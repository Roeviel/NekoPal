"""语音模块自测探针（voice_probe）。

用法::

    .venv\\Scripts\\python.exe tools\\voice_probe.py
    .venv\\Scripts\\python.exe tools\\voice_probe.py "喵～主人回来啦，今天想学点什么呀？"
    .venv\\Scripts\\python.exe tools\\voice_probe.py --offline            # 只测 DSP，不联网
    .venv\\Scripts\\python.exe tools\\voice_probe.py --input 某段.wav      # 给已有音频上猫娘音色

做的事：
1. 打印环境诊断（edge-tts 版本、soundfile/libsndfile 的 MP3 支持、available()）
2. 打印文本预处理结果（markdown / emoji / 动作描写 / 截断）
3. **离线** DSP 验证：拿 ``--input``（默认用 VoiceChanger 自带的录音.wav）走
   ``apply_neko_style``，写出 wav 并打印采样率/时长/峰值
4. 打印最终 EQ 的幅频响应，验证「更甜更轻」的塑形方向（复用 biquad._coeffs）
5. **在线** 端到端：edge-tts 合成 → 猫娘音色 → wav，并验证缓存命中
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path

# 允许 `python tools/voice_probe.py` 直接跑
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，打印 emoji 会 UnicodeEncodeError —— 强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

import numpy as np  # noqa: E402

from neko.tts import NekoTTS, preprocess_text  # noqa: E402
from neko.voicestyle import (  # noqa: E402
    apply_neko_style,
    find_voicechanger_root,
    load_wav,
    neko_style_params,
    save_wav,
    _VC_CACHE,
    _load_voicechanger,
)

DEFAULT_TEXT = "喵～主人回来啦，今天想学点什么呀？"

PREPROCESS_SAMPLES = [
    ("动作描写", "喵～（蹭蹭主人）*摇尾巴* 今天想学点什么呀？"),
    ("markdown", "## 标题\n**重点**内容 [链接](http://x.com) `code` ~~删除~~\n- 列表项"),
    ("emoji", "好耶 🐱✨🎉 太棒啦 😻"),
    ("代码块", "先看代码：\n```python\nprint('hi')\n```\n然后继续喵"),
    ("超长截断", "喵～" + "主人今天想学点什么呀？" * 40 + "尾巴都摇累了" + "喵" * 50),
]

#: 离线 DSP 验证的输入素材。优先用 `--input` 指定；
#: 这里只放项目内的相对路径（开源版不写死私人路径），找不到就现场生成测试音。
INPUT_CANDIDATES = [
    ROOT / "data" / "voice_probe" / "input.wav",
]


def _newest_cache_wav() -> Path | None:
    """没有指定素材时，退而用语音缓存里最新的一段 wav（本地一般都有）。"""
    cache = ROOT / "data" / "voice_cache"
    if not cache.is_dir():
        return None
    files = sorted(cache.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def _report(path: Path, label: str = "输出") -> None:
    samples, sr = load_wav(path)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    duration = samples.size / float(sr or 1)
    print(f"  {label:<8}= {path.resolve()}")
    print(f"  采样率  = {sr} Hz")
    print(f"  时长    = {duration:.3f} s")
    print(f"  峰值    = {peak:.4f}  ({'float32 单声道' if samples.ndim == 1 else samples.shape})")
    print(f"  大小    = {path.stat().st_size} bytes")


def _eq_response_db(params: dict, sr: int, freqs) -> np.ndarray:
    """级联 EQ 的幅频响应（dB）—— 直接复用 VoiceChanger 的双二阶系数函数。"""
    chain_mod, _cfg, _pkg = _load_voicechanger()
    # biquad 是 chain 的兄弟模块（同一个 dsp 包）
    biquad = importlib.import_module(chain_mod.__package__ + ".biquad")
    freqs = np.asarray(freqs, dtype=np.float64)
    total = np.ones_like(freqs)
    for band in params.get("eq", []) or []:
        b0, b1, b2, a1, a2 = biquad._coeffs(
            band.get("type", "peak"), sr, band.get("freq", 1000.0),
            band.get("gain_db", 0.0), band.get("q", 0.707),
        )
        z = np.exp(-1j * 2.0 * np.pi * freqs / sr)
        h = (b0 + b1 * z + b2 * z ** 2) / (1.0 + a1 * z + a2 * z ** 2)
        total = total * np.abs(h)
    return 20.0 * np.log10(np.maximum(total, 1e-9))


def main() -> int:
    parser = argparse.ArgumentParser(description="NekoPal 语音模块自测")
    parser.add_argument("text", nargs="?", default=DEFAULT_TEXT, help="要合成的文本")
    parser.add_argument("--offline", action="store_true", help="跳过联网的 edge-tts 端到端测试")
    parser.add_argument("--input", type=Path, default=None, help="离线 DSP 用的输入音频")
    parser.add_argument("--out-dir", type=Path, default=None, help="输出 wav 目录")
    args = parser.parse_args()

    out_dir = args.out_dir or (ROOT / "data" / "voice_probe")
    out_dir.mkdir(parents=True, exist_ok=True)

    _banner("1. 环境诊断")
    tts = NekoTTS()
    for key, value in tts.diagnose().items():
        print(f"  {key:<16}= {value}")
    print(f"  {'vc_root':<16}= {find_voicechanger_root()}")
    print(f"  {'ffmpeg':<16}= {__import__('shutil').which('ffmpeg') or '（未安装，soundfile 走 libsndfile 原生 mp3）'}")

    _banner("2. 文本预处理")
    for name, raw in PREPROCESS_SAMPLES:
        clean = preprocess_text(raw, tts.max_chars)
        print(f"  [{name}] {raw[:38]!r}")
        print(f"    -> {clean[:60]!r}  (len={len(clean)})")

    _banner("3. 离线 DSP 验证（不联网，直接吃 wav）")
    src = args.input
    if src is None:
        src = next((p for p in INPUT_CANDIDATES if p.is_file()), None) or _newest_cache_wav()
    if src is None or not Path(src).is_file():
        # 没有现成素材就合成一段 440Hz+880Hz 的测试音
        print("  （没找到现成输入音频，改用程序生成的测试音）")
        sr = 24000
        t = np.arange(int(sr * 2.0), dtype=np.float32) / sr
        env = np.minimum(1.0, np.minimum(t * 8, (2.0 - t) * 8)).clip(0, 1)
        wave = (0.3 * np.sin(2 * np.pi * 220 * t) + 0.15 * np.sin(2 * np.pi * 440 * t)) * env
        src = out_dir / "probe_input.wav"
        save_wav(src, wave.astype(np.float32), sr)

    t0 = time.perf_counter()
    samples, sr = load_wav(src)
    styled = apply_neko_style(samples, sr, **tts.dsp_params)
    dst = out_dir / "probe_offline_neko.wav"
    save_wav(dst, styled, sr)
    dt = time.perf_counter() - t0
    print(f"  输入    = {Path(src).resolve()}")
    print(f"  输入信息= {sr} Hz, {samples.size / sr:.3f} s, 峰值 {np.max(np.abs(samples)):.4f}")
    print(f"  音色参数= {tts.dsp_params}")
    print(f"  处理耗时= {dt:.3f} s  ({dt / max(samples.size / sr, 1e-9):.2f}x 实时)")
    _report(dst, "输出")

    _banner("4. 最终 EQ 频率响应（验证「更甜更轻」的塑形方向）")
    params = neko_style_params(sr, **tts.dsp_params)
    print(f"  预设底子  = {params.get('name')} (VoiceChanger「少御音」)")
    print(f"  EQ 级联   = {len(params.get('eq', []))} 段")
    check_freqs = [60, 120, 250, 500, 1000, 2400, 3200, 4500, 5500, 7000, 9000, 11000]
    resp = _eq_response_db(params, sr, check_freqs)
    print("  频点(Hz) : " + "".join(f"{f:>7}" for f in check_freqs))
    print("  增益(dB) : " + "".join(f"{g:>7.1f}" for g in resp))
    for band in params.get("eq", []):
        print(f"    - {band.get('type'):<10} {band.get('freq'):>7.0f} Hz  "
              f"{band.get('gain_db', 0.0):+.1f} dB  q={band.get('q', 0.707)}")

    if args.offline:
        print("\n[--offline] 跳过 edge-tts 端到端测试")
        return 0

    _banner("5. 在线端到端：edge-tts → 猫娘音色 → wav")
    print(f"  输入文本= {args.text!r}")
    clean = preprocess_text(args.text, tts.max_chars)
    print(f"  预处理后= {clean!r}")
    if not clean:
        print("  [失败] 预处理后为空")
        return 1
    if not tts.available():
        print(f"  [阻塞] edge-tts 不可用：{tts._last_probe_error}")
        return 2

    t0 = time.perf_counter()
    out = tts.synth(args.text, str(out_dir / "probe_online_neko.wav"))
    dt = time.perf_counter() - t0
    print(f"  合成耗时= {dt:.2f} s")
    _report(Path(out), "输出")

    # 缓存：第二次应几乎瞬时
    t0 = time.perf_counter()
    first = tts.synth_to_cache(args.text)
    t1 = time.perf_counter()
    second = tts.synth_to_cache(args.text)
    t2 = time.perf_counter()
    _report(Path(second), "缓存")
    print(f"  缓存命中= {first == second}  (首次 {t1 - t0:.2f}s / 二次 {t2 - t1:.4f}s)")
    print(f"  VoiceChanger 根目录 = {_VC_CACHE.get('root')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
