"""语音识别（ASR）：没有字幕时，直接听视频的声音。

## 为什么加这一级

学习内容的降级链原本是：字幕 → 弹幕评论 → 简介 → 标题。
登录后字幕覆盖率实测 18/20，但**剩下那 2 个只能靠观众评论反推**，
而这恰恰是最需要"视频原话"的场景。所以补一级：**直接识别视频音频**。

## 实测数据（本机 16 核，int8 量化）

| 模型 | 120 秒音频转写耗时 | 速度比 | 20 分钟视频 |
|---|---|---|---|
| base | 4.5s | **26.5x 实时** | **0.8 分钟** |
| small | 12.4s | 9.7x 实时 | 2.1 分钟 |

base 快得多、准确度差距不大，所以默认 base。

## 依赖与坑

1. **音频是 M4A/AAC，soundfile 解不了**（它支持 mp3/ogg/flac/wav）。
   所以用 **PyAV**（自带 ffmpeg 库，不需要单独装 ffmpeg）。
2. **huggingface.co 在本机不可达**（ConnectTimeout），必须走 `hf-mirror.com`。
3. **huggingface_hub 默认把缓存写 `~/.cache/huggingface`，本机被拒**（os error 5），
   还会因为 xet 日志再报一次。所以要把 `HF_HOME` 指到项目内，并关掉 xet。
   这三个环境变量必须在 `import faster_whisper` **之前**设置好。
4. 模型只加载一次并常驻（加载 ~1s，但重复加载没必要）。

任何一步失败都返回空字符串，让上层继续往下降级 —— 听不清不能把学习搞挂。
"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

#: 只在没设过的时候写入，别覆盖用户自己的配置
def _prepare_env(model_dir: Path) -> None:
    hf_home = model_dir / "hf_home"
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(hf_home))
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


_model_lock = threading.Lock()
_models: dict[str, Any] = {}

#: 默认从哪一级开始降级到 ASR —— 只对没字幕的视频做，有字幕就别浪费算力
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def deps_ok() -> tuple[bool, str]:
    """检查依赖。返回 (是否可用, 原因)。"""
    try:
        import av  # noqa: F401
    except ImportError:
        return False, "没装 PyAV（pip install av）"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "没装 faster-whisper（pip install faster-whisper）"
    return True, ""


def _get_model(size: str, model_dir: Path):
    with _model_lock:
        if size not in _models:
            from faster_whisper import WhisperModel

            t0 = time.time()
            _models[size] = WhisperModel(
                size, device="cpu", compute_type="int8", download_root=str(model_dir)
            )
            print(f"[asr] 模型 {size} 就绪（{time.time() - t0:.1f}s）")
        return _models[size]


def _audio_url(bili: Any, bvid: str, cid: int) -> str:
    """向 playurl 要音频流地址（DASH 的第一条音轨）。"""
    payload = bili._get_json(  # noqa: SLF001 —— 复用它的签名/请求头逻辑
        "https://api.bilibili.com/x/player/playurl",
        params={"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1},
        allow_codes=(0,),
        with_cookie=True,
    )
    dash = (payload.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        raise RuntimeError("这个视频没有可用的音频轨")
    url = str(audios[0].get("baseUrl") or audios[0].get("base_url") or "")
    if not url:
        raise RuntimeError("音频轨没有地址")
    return url


def _decode_16k(blob: bytes, seconds: int):
    """解成 whisper 要的 16kHz 单声道 float32。"""
    import av
    import numpy as np

    with av.open(io.BytesIO(blob)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=16000)
        chunks = []
        total = 0
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                arr = out.to_ndarray().reshape(-1)
                chunks.append(arr)
                total += arr.size
            if total >= 16000 * seconds:
                break
    if not chunks:
        return None
    audio = np.concatenate(chunks).astype("float32") / 32768.0
    return audio[: 16000 * seconds]


def transcribe(
    bili: Any,
    bvid: str,
    cid: int,
    cfg: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """听这个视频的前 N 秒，返回 (文本, 说明)。失败返回 ("", 原因)。"""
    conf = ((cfg or {}).get("asr") or {})
    if not conf.get("enabled", True):
        return "", "语音识别已在配置里关闭"
    ok, why = deps_ok()
    if not ok:
        return "", why

    model_dir = Path(conf.get("model_dir") or (ROOT / "data" / "asr_models"))
    _prepare_env(model_dir)

    size = str(conf.get("model") or "base")
    max_seconds = max(30, int(conf.get("max_seconds", 300) or 300))
    language = str(conf.get("language") or "zh")
    prompt = str(conf.get("initial_prompt") or "以下是普通话的课程讲解内容，可能有专业术语。")

    # 从 CFG 里拿 cookie 供下载音频用（playurl 的地址需要登录态）
    cookie = str(((cfg or {}).get("bilibili") or {}).get("cookie") or "").strip()

    t0 = time.time()
    try:
        import httpx

        url = _audio_url(bili, bvid, cid)
        headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
        if cookie:
            headers["Cookie"] = cookie
        # 80kbps ≈ 10KB/s，按 max_seconds 多要点余量；用 Range 只取前面一段
        want = int(max_seconds * 1024 * 12)
        with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as c:
            resp = c.get(url, headers={"Range": f"bytes=0-{want}"})
        if resp.status_code not in (200, 206):
            return "", f"音频下载失败 HTTP {resp.status_code}"
        blob = resp.content
    except Exception as exc:  # noqa: BLE001
        return "", f"取音频失败：{type(exc).__name__}: {exc}"

    try:
        audio = _decode_16k(blob, max_seconds)
    except Exception as exc:  # noqa: BLE001
        return "", f"解码音频失败：{type(exc).__name__}: {exc}"
    if audio is None or audio.size < 16000:
        return "", "音频太短，没有可用内容"

    try:
        model = _get_model(size, model_dir)
        segments, _info = model.transcribe(
            audio, language=language, beam_size=1, vad_filter=True, initial_prompt=prompt
        )
        text = "".join(seg.text for seg in segments).strip()
    except Exception as exc:  # noqa: BLE001
        return "", f"语音识别失败：{type(exc).__name__}: {exc}"

    if len(text) < 40:
        return "", "识别出来的内容太少"
    dur = audio.size / 16000
    note = (f"语音识别（{size} 模型，识别前 {int(dur // 60)} 分 {int(dur % 60)} 秒，"
            f"耗时 {time.time() - t0:.0f} 秒）")
    return text, note


if __name__ == "__main__":  # 自检：python -m neko.asr BV号
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .bilibili import BiliClient
    from .config import load_config

    cfg = load_config()
    ok, why = deps_ok()
    print("依赖:", "可用" if ok else why)
    if not ok:
        raise SystemExit(1)
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    with BiliClient(cfg) as b:
        if not target:
            target = b.search("高等数学", limit=1)[0]["bvid"]
        info = b.video(target)
        print("视频:", info.get("title", "")[:40])
        text, note = transcribe(b, target, int(info.get("cid") or 0), cfg)
        print("说明:", note)
        print("正文:", text[:200] if text else "(空)")
