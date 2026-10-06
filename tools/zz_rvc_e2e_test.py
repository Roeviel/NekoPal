"""端到端验证 RVC 变声：edge-tts -> 猫娘 DSP -> RVC，三个模型逐个真跑一遍。

为什么要有这个脚本：RVC 是**跨进程 + 跨 Python 版本**的（项目 3.14 / RVC 3.9），
中间任何一环坏了都只会"静默退回原声"，听起来像"没生效"却看不出哪里断了。
所以这里把每一层的结果都打出来：时长、采样率、音量、F0 中位数，
并且要求三个模型的输出**互不相同**、也和非变声的输出不同 —— 不满足就报 FAIL。

用法（要有耐心，第一个模型要等 worker 起来 + 载模型）：

    .venv\\Scripts\\python.exe tools\\zz_rvc_e2e_test.py
"""

from __future__ import annotations

import copy
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from neko import rvc  # noqa: E402
from neko.config import load_config  # noqa: E402
from neko.tts import NekoTTS  # noqa: E402

TEXT = "同行者，这就是我现在的音色，你觉得怎么样？"
OFF = "（不变声·对照）"


def read_mono(path: str) -> tuple[np.ndarray, int]:
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    return x, int(sr)


def f0_median(x: np.ndarray, sr: int) -> float:
    """粗略估 F0 中位数（自相关），用来判断"音色到底变没变"。

    只做**相对比较**用：同一句话在三套参数下的 F0 差异是真实的；
    绝对值不必当真（这里不区分清浊音，也没做任何平滑）。
    """
    win, hop = int(sr * 0.04), int(sr * 0.02)
    lo, hi = int(sr / 400), int(sr / 70)          # 70~400Hz 的搜索范围
    vals: list[float] = []
    for start in range(0, max(1, len(x) - win), hop):
        frame = x[start:start + win]
        if frame.size < win or float(np.sqrt(np.mean(frame ** 2))) < 0.01:
            continue
        frame = frame - frame.mean()
        ac = np.correlate(frame, frame, mode="full")[win - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if seg.size == 0:
            continue
        lag = int(np.argmax(seg)) + lo
        if ac[lag] / ac[0] > 0.3:
            vals.append(sr / lag)
    return float(np.median(vals)) if vals else 0.0


def describe(path: str) -> dict:
    x, sr = read_mono(path)
    return {
        "sec": round(len(x) / sr, 2),
        "sr": sr,
        "rms": round(float(np.sqrt(np.mean(x ** 2))), 4),
        "f0": round(f0_median(x, sr), 1),
        "kb": round(Path(path).stat().st_size / 1024),
    }


def main() -> int:
    cfg = load_config()
    print(f"RVC 整合包：{rvc.rvc_dir(cfg)}")
    print(f"启用状态：{rvc.enabled(cfg)}")
    voices = rvc.voices(cfg)
    print(f"配置里的音色（{len(voices)} 个）：")
    for v in voices:
        print(f"  - {v['name']:<10} {'OK ' if v['ok'] else '缺失'} {v['model']}")
    if not rvc.enabled(cfg):
        print("\nFAIL：RVC 没启用（或整合包路径不对）")
        return 1
    if not voices or not all(v["ok"] for v in voices):
        print("\nFAIL：有模型文件不存在")
        return 1

    paths: dict[str, str] = {}
    results: dict[str, dict] = {}

    # ① 对照：同一句话，关掉 RVC
    off_cfg = copy.deepcopy(cfg)
    off_cfg["voice"]["rvc"]["enabled"] = False
    print(f"\n① 合成对照（不变声）…")
    t0 = time.time()
    paths[OFF] = NekoTTS(off_cfg).synth_to_cache(TEXT)
    results[OFF] = describe(paths[OFF])
    print(f"   耗时 {time.time()-t0:5.1f}s  {results[OFF]}")

    # ② 三个模型逐个真跑
    for i, v in enumerate(voices, start=1):
        trial = copy.deepcopy(cfg)
        trial["voice"]["rvc"]["voice"] = v["name"]
        trial["voice"]["rvc"]["enabled"] = True
        tts = NekoTTS(trial)
        print(f"\n② 变声 ->「{v['name']}」（{v['model']}）")
        print(f"     tts 实际在用的音色 = {tts.rvc_voice_name!r}  "
              f"{'OK' if tts.rvc_voice_name == v['name'] else 'FAIL：没走 RVC'}")
        t1 = time.time()
        paths[v["name"]] = tts.synth_to_cache(TEXT)
        el = time.time() - t1
        results[v["name"]] = describe(paths[v["name"]])
        print(f"     耗时 {el:.1f}s  {results[v['name']]}")
        for line in rvc.log()[-2:]:
            print(f"     rvc: {line}")

    print("\n③ 汇总")
    print(f"     {'音色':<18}{'时长':>7}{'sr':>7}{'音量':>9}{'F0':>8}{'KB':>7}")
    for name, d in results.items():
        print(f"     {name:<18}{d['sec']:>7}{d['sr']:>7}{d['rms']:>9}{d['f0']:>8}{d['kb']:>7}")

    # ④ 判定：四份输出的字节必须两两不同（相同就说明变声没生效 / 音频被复用）
    blobs = {name: Path(p).read_bytes() for name, p in paths.items()}
    names = list(blobs)
    ok = True
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if blobs[a] == blobs[b]:
                print(f"\nFAIL：「{a}」和「{b}」输出完全相同 —— 变声没生效")
                ok = False
    if ok:
        print("\nPASS：三模型 + 对照共四份输出两两都不同，RVC 变声确实生效")
    print("（对照听感请自己放一遍："
          + "，".join(str(Path(p).name) for p in paths.values()) + "）")

    rvc.shutdown()
    print("已关闭 RVC worker")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
