"""NekoPal 语音合成（edge-tts + VoiceChanger 猫娘音色）。

链路：文本预处理 → edge-tts 合成(mp3) → 解码 → ``apply_neko_style`` → wav。

mp3 解码
--------
``edge_tts.Communicate`` 只能输出 ``audio-24khz-48kbitrate-mono-mp3``，
而 ``soundfile`` 长期以来不支持 mp3。**本机的解法**：``soundfile`` 0.13+
捆绑的 libsndfile >= 1.1 已内置 MP3 解码（libmpg123），因此
``sf.read(io.BytesIO(mp3_bytes))`` 直接可用，**不需要 ffmpeg**。
``neko.voicestyle.decode_audio_bytes`` 仍保留了 miniaudio / ffmpeg /
pydub 多级回退，以应对老版本 soundfile。

采样率
------
edge-tts 输出为 24000 Hz，本模块**不重采样**，端到端保持 24000 Hz；
输出 wav 为 16-bit PCM 单声道（float32 单声道在 DSP 内部流转）。

配置项（``cfg["voice"]``）
-------------------------
``enabled`` / ``azure_voice`` / ``rate`` / ``volume`` / ``pitch_semitones`` /
``formant_shift`` / ``reverb`` / ``gain_db`` / ``cache_dir`` / ``max_chars``。
其中 ``enabled=False`` 时仍会合成，但**跳过猫娘音色**（输出 edge-tts 原声），
``available()`` 只反映 edge-tts 可导入性与网络，不受 ``enabled`` 影响。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

if __package__:
    from .config import load_config, resolve_path
    from .voicestyle import apply_neko_style, decode_audio_bytes, load_wav, save_wav
else:  # 支持 python neko/tts.py 直接运行
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from neko.config import load_config, resolve_path
    from neko.voicestyle import apply_neko_style, decode_audio_bytes, load_wav, save_wav

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: edge-tts 的 WebSocket 端点主机（用于 ``available()`` 的轻量网络探测）
EDGE_TTS_HOST = "speech.platform.bing.com"
EDGE_TTS_PORT = 443
#: 网络探测超时（秒）——必须短，``available()`` 可能被 UI 同步调用
NET_PROBE_TIMEOUT = 1.5
#: ``available()`` 结果缓存时长（秒），避免频繁探测
AVAIL_TTL = 60.0

#: 缓存键里的音色版本号：调整 DSP 参数或链结构后 +1，即可整体失效旧缓存
#: （v4：接上 RVC 变声；v5：修掉"变声失败也把原声写进缓存"导致的永久失效，
#   旧缓存里已经混进了没变声的文件，必须整体作废）
STYLE_VERSION = "5"

#: 默认配置（config.py 缺失该段时兜底）
VOICE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "azure_voice": "zh-CN-XiaoyiNeural",
    "rate": "+8%",
    "volume": "+0%",
    "pitch_semitones": 5.0,
    "formant_shift": 0.18,
    "reverb": 0.12,
    "gain_db": 0.0,
    "cache_dir": "data/voice_cache",
    "max_chars": 400,
}

#: 中文/英文句末标点（截断优先在此断句）
SENTENCE_END_CHARS = "。！？～…!?；;."

_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_NL_RE = re.compile(r"\n{2,}")
_CJK_GAP_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")

# 代码块 / 行内代码
_CODE_FENCE_RE = re.compile(r"(```|~~~).*?\1", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
# 图片（整段删）与链接（留文字）
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# emoji / 符号图标 / 变体选择符 / ZWJ
_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"   # 麻将/扑克/表情/交通/补充符号
    "\U00002600-\U000027bf"   # 杂项符号 + 装饰符号
    "\U0001f1e6-\U0001f1ff"   # 区域指示符（国旗）
    "\u2190-\u21ff"           # 箭头
    "\u2300-\u23ff"           # 技术符号
    "\u2b00-\u2bff"           # 杂项符号与箭头
    "\u3030\u303d\u3297\u3299\u00a9\u00ae\u2122\u2139"
    "\ufe0e\ufe0f\u200d\u20e3"
    "]+"
)
# 加粗（**x**/__x__）保留文字；斜体/星号包裹（*摇尾巴*）视为动作描写整段删除
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_ACTION_STAR_RE = re.compile(r"\*[^*\n]{1,40}\*|_[^_\n]{1,40}_")
# 动作描写：短括号（全/半角）整段删除
_ACTION_PAREN_RE = re.compile(r"[（(][^（）()\n]{0,40}[）)]")
_PAREN_LEFTOVER_RE = re.compile(r"[（）()]")
# 残余 markdown 标记
_MD_MARK_RE = re.compile(
    r"(\*+|~{2,}|`|^\s*#{1,6}\s*|\s#{1,6}\s+|^\s*[-*+•]\s+|^\s*\d+[.)]\s+|^\s*>\s+|\|)",
    re.M,
)


# ---------------------------------------------------------------------------
# 文本预处理
# ---------------------------------------------------------------------------


def preprocess_text(text: str, max_chars: int | None = 400) -> str:
    """把 LLM 输出洗成「适合朗读」的纯文本。

    依次去掉：markdown 代码块/行内代码、图片与链接语法、emoji、
    动作描写（``（蹭蹭）``、``*摇尾巴*``）、残余 markdown 标记，
    再压缩空白；超过 ``max_chars`` 时在句末标点（。！？～等）处截断。

    ``max_chars`` 为 None 或 <= 0 表示不截断。结果可能为空字符串。
    """
    if not text:
        return ""

    s = str(text).replace("\r\n", "\n").replace("\r", "\n")

    s = _CODE_FENCE_RE.sub(" ", s)          # 代码块整段丢弃
    s = _INLINE_CODE_RE.sub(r"\1", s)       # 行内代码保留文字
    s = _IMAGE_RE.sub(" ", s)
    s = _LINK_RE.sub(r"\1", s)
    s = _EMOJI_RE.sub(" ", s)
    s = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", s)  # 加粗保留文字
    s = _ACTION_STAR_RE.sub(" ", s)         # 星号/下划线包裹的动作描写整段删除
    s = _ACTION_PAREN_RE.sub(" ", s)
    s = _PAREN_LEFTOVER_RE.sub(" ", s)      # 超长括号只去括号,保留文字
    s = _MD_MARK_RE.sub(" ", s)

    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = _NL_RE.sub("\n", s)
    s = _WS_RE.sub(" ", s)
    s = s.replace("\n", " ")
    s = _CJK_GAP_RE.sub("", s)
    s = s.strip(" \t\n，,、")               # 去掉开头/结尾的碎标点

    if max_chars and max_chars > 0:
        s = truncate_text(s, int(max_chars))
    return s.strip()


def truncate_text(text: str, max_chars: int) -> str:
    """超长时在句末标点处截断；找不到合适断点则硬截。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    window = text[:max_chars]
    cut = max((window.rfind(ch) for ch in SENTENCE_END_CHARS), default=-1)
    if cut >= max(1, int(max_chars * 0.5)):
        return window[: cut + 1]
    return window


# ---------------------------------------------------------------------------
# NekoTTS
# ---------------------------------------------------------------------------


class NekoTTS:
    """edge-tts 合成 + 猫娘音色（复用 VoiceChanger DSP）+ 硬盘缓存。

    ``synth`` / ``synth_async`` / ``synth_to_cache`` 都返回**最终 wav 的绝对路径**
    （已上猫娘音色，可直接播放或作为微信语音发送）。
    """

    def __init__(self, cfg: dict | None = None) -> None:
        if cfg is None:
            cfg = load_config()
        self.cfg = cfg

        raw: dict[str, Any] = dict(VOICE_DEFAULTS)
        raw.update(cfg.get("voice") or {})

        self.enabled = bool(raw.get("enabled", True))
        self.azure_voice = str(raw.get("azure_voice") or VOICE_DEFAULTS["azure_voice"])
        self.rate = str(raw.get("rate") or "+0%")
        self.volume = str(raw.get("volume") or "+0%")
        self.pitch_semitones = _as_float(raw.get("pitch_semitones"), 5.0)
        self.formant_shift = _as_float(raw.get("formant_shift"), 0.18)
        self.reverb = _as_float(raw.get("reverb"), 0.12)
        self.gain_db = _as_float(raw.get("gain_db"), 0.0)
        self.max_chars = int(_as_float(raw.get("max_chars"), 400))
        self.cache_dir = Path(resolve_path(str(raw.get("cache_dir") or "data/voice_cache")))

        # RVC 变声（把 edge-tts 的底声换成真人音色）。整段 dict 原样留着，
        # 具体解析交给 neko/rvc.py —— 这里只关心"开没开"和"用哪个音色"。
        self.rvc: dict[str, Any] = dict(raw.get("rvc") or {})

        self._edge_tts = None
        self._edge_tts_error: str | None = None
        self._avail_lock = threading.Lock()
        self._avail_cache: tuple[float, bool] | None = None
        self._last_probe_error: str | None = None

    # -- 配置辅助 ---------------------------------------------------------

    @property
    def style_enabled(self) -> bool:
        """是否对 edge-tts 原声应用猫娘音色（= ``voice.enabled``）。"""
        return self.enabled

    @property
    def dsp_params(self) -> dict[str, float]:
        return {
            "pitch_semitones": self.pitch_semitones,
            "formant_shift": self.formant_shift,
            "reverb": self.reverb,
            "gain_db": self.gain_db,
        }

    def _style_kwargs(self) -> dict[str, float]:
        """``enabled=False`` 时给出中性参数（不变调、不加混响）。"""
        if not self.enabled:
            return {"pitch_semitones": 0.0, "formant_shift": 0.0, "reverb": 0.0, "gain_db": 0.0}
        return self.dsp_params

    # -- RVC 变声 ---------------------------------------------------------

    @property
    def rvc_voice_name(self) -> str:
        """当前生效的 RVC 音色名；没开、配置坏了或模型不在就是空串。"""
        if not self.rvc.get("enabled"):
            return ""
        from . import rvc as rvc_mod

        if not rvc_mod.enabled(self.cfg):
            return ""
        v = rvc_mod.current(self.cfg)
        return str(v["name"]) if v else ""

    def _apply_rvc(self, target: Path) -> bool:
        """把刚写好的 wav 再过一遍 RVC，**就地替换**成变声后的音频。

        为什么失败要静默保留原音：变声是"锦上添花"，它坏了顶多声音没变；
        但如果因此抛异常，整句话就没声音了 —— 这个取舍在 neko/rvc.py 里定死了，
        这里只负责执行。

        为什么写临时文件再 ``os.replace``：转换到一半失败会留下半个 wav，
        直接覆盖目标文件就再也回不去了（用户听到的是爆音或半句话）。
        """
        name = self.rvc_voice_name
        if not name:
            return False
        from . import rvc as rvc_mod

        voice = rvc_mod.current(self.cfg)
        if voice is None:
            return False
        # 临时文件必须**保留 .wav 后缀**：RVC worker 里用 soundfile 写文件，
        # 它是按扩展名判断容器格式的，写成 .rvc 会直接报"未知格式"。
        tmp = target.with_name(f".{target.stem}.rvc{target.suffix}")
        try:
            res = rvc_mod.convert(self.cfg, str(target), str(tmp), voice)
            if res.get("ok") and tmp.is_file() and tmp.stat().st_size > 44:
                os.replace(tmp, target)
                print(f"[tts] RVC「{name}」完成：推理 {res.get('infer_sec')}s / "
                      f"载入 {res.get('load_sec')}s")
                return True
            print(f"[tts] RVC 没生效，保留原始音色：{res.get('reason')}")
            return False
        except Exception as exc:  # noqa: BLE001 —— 绝不因为变声失败而没声音
            print(f"[tts] RVC 异常，保留原始音色：{type(exc).__name__}: {exc}")
            return False
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # -- 可用性 -----------------------------------------------------------

    def _import_edge_tts(self):
        if self._edge_tts is not None:
            return self._edge_tts
        try:
            import edge_tts  # type: ignore

            self._edge_tts = edge_tts
            self._edge_tts_error = None
            return edge_tts
        except Exception as exc:  # pragma: no cover - 取决于环境
            self._edge_tts_error = f"{type(exc).__name__}: {exc}"
            raise

    def available(self) -> bool:
        """edge-tts 能否导入 + 网络是否可用。**不抛异常**。结果缓存 60 秒。"""
        now = time.monotonic()
        with self._avail_lock:
            if self._avail_cache is not None and now - self._avail_cache[0] < AVAIL_TTL:
                return self._avail_cache[1]

        ok = False
        err: str | None = None
        try:
            self._import_edge_tts()
        except Exception as exc:
            err = f"edge-tts 不可用：{self._edge_tts_error or exc}"
        else:
            try:
                with socket.create_connection((EDGE_TTS_HOST, EDGE_TTS_PORT), timeout=NET_PROBE_TIMEOUT):
                    ok = True
            except Exception as exc:
                err = f"无法连接 {EDGE_TTS_HOST}:{EDGE_TTS_PORT}（{type(exc).__name__}: {exc}）"

        self._last_probe_error = err
        with self._avail_lock:
            self._avail_cache = (now, ok)
        return ok

    def diagnose(self) -> dict[str, Any]:
        """诊断信息（给 ``tools/voice_probe.py`` 用）。"""
        info: dict[str, Any] = {
            "voice_enabled": self.enabled,
            "azure_voice": self.azure_voice,
            "rate": self.rate,
            "volume": self.volume,
            "dsp_params": self.dsp_params,
            "max_chars": self.max_chars,
            "cache_dir": str(self.cache_dir),
            "edge_tts_import": None,
            "mp3_decoder": None,
            "available": False,
            "error": None,
        }
        try:
            mod = self._import_edge_tts()
            info["edge_tts_import"] = getattr(mod, "__version__", "unknown")
        except Exception as exc:
            info["error"] = f"edge-tts 导入失败：{exc}"

        try:
            import soundfile as sf

            info["mp3_decoder"] = (
                f"soundfile {sf.__version__} / libsndfile {sf.__libsndfile_version__}"
                f" (MP3={'MP3' in sf.available_formats()})"
            )
        except Exception as exc:
            info["mp3_decoder"] = f"soundfile 不可用：{exc}"

        info["available"] = self.available()
        info["error"] = info["error"] or self._last_probe_error
        return info

    # -- 合成 -------------------------------------------------------------

    async def _edge_synth(
        self,
        text: str,
        *,
        rate: str | None = None,
        volume: str | None = None,
        pitch: str | None = None,
    ) -> bytes:
        """调用 edge-tts 拿 mp3 字节。rate/volume/pitch 可逐句覆盖（见 neko/prosody.py）。

        注意：这几个参数是**真实生效**的（实测 rate ±20% 让时长 4.13s→3.46/5.16s，
        pitch ±30Hz 让 F0 221Hz→240/181Hz）。而 `mstts:express-as` 表现力风格
        **不可用** —— edge-tts 会把 SSML 转义成纯文本，服务就去朗读 XML 标签了。
        """
        edge_tts = self._import_edge_tts()
        kwargs: dict[str, Any] = {
            "text": text,
            "voice": self.azure_voice,
            "rate": rate if rate is not None else self.rate,
            "volume": volume if volume is not None else self.volume,
        }
        if pitch is not None:
            kwargs["pitch"] = pitch
        try:
            communicate = edge_tts.Communicate(**kwargs)
        except TypeError:
            # 兼容更老/更新的签名差异（老版本没有 pitch）
            try:
                communicate = edge_tts.Communicate(
                    text=text, voice=self.azure_voice,
                    rate=kwargs["rate"], volume=kwargs["volume"],
                )
            except TypeError:
                communicate = edge_tts.Communicate(text, self.azure_voice)

        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.extend(chunk["data"])
        if not buf:
            raise RuntimeError(f"edge-tts 未返回音频（voice={self.azure_voice}, 文本长度={len(text)}）")
        return bytes(buf)

    def _output_path(self, out_path: str | None) -> Path:
        if out_path:
            return Path(out_path)
        # 不放系统 TEMP（本机 TEMP 指向项目上层目录），统一收到项目 data/ 下
        tmp_dir = Path(resolve_path("data/voice_tmp"))
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="neko_tts_", suffix=".wav", dir=str(tmp_dir))
        os.close(fd)
        return Path(name)

    def _write_wav(self, target: Path, samples: "np.ndarray", samplerate: int) -> None:
        """原子写 wav：临时名带 pid+线程 id，避免多线程同时合成同一段文本时互相截断。"""
        if str(target.parent):
            target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.{os.getpid()}.{threading.get_ident()}.part"
        try:
            save_wav(tmp, samples, samplerate)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    async def _synth_clean_async(
        self,
        clean_text: str,
        out_path: str | None,
        prosody: dict[str, str] | None = None,
    ) -> str:
        mp3 = await self._edge_synth(clean_text, **(prosody or {}))
        samples, samplerate = decode_audio_bytes(mp3)
        if samples.size == 0:
            raise RuntimeError("解码后的音频为空")
        styled = apply_neko_style(samples, samplerate, **self._style_kwargs())

        target = self._output_path(out_path)
        self._write_wav(target, styled, samplerate)
        return str(target.resolve())

    async def _synth_final_async(
        self, text: str, out_path: str | None, emotion: str | None = None
    ) -> str:
        """合成入口：edge-tts + 猫娘 DSP + 语气，最后**再过一遍 RVC 变声**。

        RVC 单独包一层的原因：里面的 ``_synth_final_raw_async`` 有 3 个 return
        （整句 / 单块拼接 / 多块拼接），把变声挂在每一处很容易漏；
        统一在这层收口，三条路径都必然经过。
        """
        path, _ = await self._synth_and_convert(text, out_path, emotion=emotion)
        return path

    async def _synth_and_convert(
        self, text: str, out_path: str | None, emotion: str | None = None
    ) -> tuple[str, bool]:
        """同上，但把"变声到底生效没有"一起返回。

        为什么不能用一个实例变量记：同一条回复可能被多个线程同时合成，
        变量会被别人覆盖，于是"没变声的文件"就可能被当成变声成功写进缓存。
        """
        path = await self._synth_final_raw_async(text, out_path, emotion=emotion)
        return path, self._apply_rvc(Path(path))

    async def _synth_final_raw_async(
        self, text: str, out_path: str | None, emotion: str | None = None
    ) -> str:
        """带语气的合成入口（不含 RVC）。

        一条回复会被拆成若干句，**每句用不同的 rate/pitch/volume 单独合成再拼起来**，
        这样句子内部和句子之间都有起伏（疑问上扬、感叹加快、省略号下沉），
        而不是整条一个平调子。只有一句（或碎句太多）时退回整条合成。

        ``emotion`` 来自 `[表情:x]` 标记，由 Brain.voice 从原文判定后传进来。

        代价是句子多时会有多次 TTS 调用，所以 neko/prosody.py 里限了上限。
        """
        from . import prosody as pr

        pieces = pr.plan(text, emotion=emotion)
        if len(pieces) <= 1:
            clean = preprocess_text(text, self.max_chars)
            if not clean:
                raise ValueError("文本预处理后为空，没有可合成的内容")
            p = pieces[0] if pieces else {}
            kw = {k: p[k] for k in ("rate", "pitch", "volume") if p.get(k)}
            return await self._synth_clean_async(clean, out_path, prosody=kw or None)

        chunks: list[np.ndarray] = []
        samplerate = 0
        for piece in pieces:
            clean = preprocess_text(piece.get("text", ""), self.max_chars)
            if not clean:
                continue
            kw = {k: piece[k] for k in ("rate", "pitch", "volume") if piece.get(k)}
            try:
                mp3 = await self._edge_synth(clean, **kw)
                samples, sr = decode_audio_bytes(mp3)
            except Exception as exc:  # noqa: BLE001 —— 单句失败不该让整条没有声音
                print(f"[tts] 逐句合成时这句失败，跳过：{exc}")
                continue
            if samples.size == 0:
                continue
            styled = apply_neko_style(samples, sr, **self._style_kwargs())
            if not samplerate:
                samplerate = sr
            elif sr != samplerate:
                continue                      # 采样率不一致的极端情况，宁缺勿乱
            chunks.append(styled)

        if not chunks:
            raise RuntimeError("逐句合成后没有可用音频")
        if len(chunks) == 1:
            target = self._output_path(out_path)
            self._write_wav(target, chunks[0], samplerate)
            return str(target.resolve())

        # 句间留 130ms 停顿：不留的话拼起来像连读，反而更不像人
        gap = np.zeros(int(samplerate * 0.13), dtype=chunks[0].dtype)
        joined = chunks[0]
        for c in chunks[1:]:
            joined = np.concatenate([joined, gap, c])

        target = self._output_path(out_path)
        self._write_wav(target, joined, samplerate)
        return str(target.resolve())

    def _run_async(self, coro):
        """同步执行协程；若当前已处于事件循环中，则改用工作线程。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()

    async def synth_async(
        self, text: str, out_path: str | None = None, emotion: str | None = None
    ) -> str:
        """异步合成：返回最终 wav 的绝对路径（已上猫娘音色 + 语气）。"""
        return await self._synth_final_async(text, out_path, emotion=emotion)

    def synth(
        self, text: str, out_path: str | None = None, emotion: str | None = None
    ) -> str:
        """同步合成：返回最终 wav 的绝对路径（已上猫娘音色 + 语气）。

        ``out_path=None`` 时写到系统临时目录下的 ``neko_tts_*.wav``。
        无论 ``out_path`` 后缀是什么，写出的都是 WAV(PCM_16) 内容。
        """
        return self._run_async(self._synth_final_async(text, out_path, emotion=emotion))

    # -- 缓存 -------------------------------------------------------------

    def cache_key(self, clean_text: str, emotion: str | None = None) -> str:
        """按「文本 + 音色参数 + 情绪 + RVC 音色」生成 cache key（sha1 十六进制）。

        情绪必须算进去：同一句话在不同情绪下合成出的音频是不同的
        （rate/pitch/volume 都不一样），不加进 key 会拿到错的缓存。
        RVC 音色同理 —— 变声后的音频是最终产物，换音色必须重新生成。
        """
        payload = "\x1f".join(
            [
                STYLE_VERSION,
                clean_text,
                self.azure_voice,
                self.rate,
                self.volume,
                f"{self.pitch_semitones:g}",
                f"{self.formant_shift:g}",
                f"{self.reverb:g}",
                f"{self.gain_db:g}",
                "1" if self.enabled else "0",
                f"emo={emotion or '-'}",
                # RVC 音色也要进 key：换了音色必须重新合成，
                # 否则"用这个"之后放出来的还是上一个音色的缓存。
                f"rvc={self.rvc_voice_name or '-'}",
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def synth_to_cache(self, text: str, emotion: str | None = None) -> str:
        """按「文本 + 音色参数 + 情绪 + RVC 音色」hash 命名，命中缓存直接返回路径，不重复合成。

        **RVC 打开时不能直接往缓存路径里写**：万一本句变声失败（模型没下好、
        worker 卡住…），落在缓存键上的就是"没变声的原声"，
        而这个键是"已经变声"的键 —— 之后每次都会命中它，变声就永久失效了
        （实测踩过：修好 RVC 之后声音还是没变，因为缓存骗了它）。
        所以顺序是：先合成到临时文件 → 变声成功才原子搬进缓存；
        失败就返回临时文件（能听，但不占坑，下次还会重试变声）。
        """
        clean = preprocess_text(text, self.max_chars)
        if not clean:
            raise ValueError("文本预处理后为空，没有可合成的内容")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{self.cache_key(clean, emotion)}.wav"
        try:
            if path.is_file() and path.stat().st_size > 44:  # 44 = wav 头长度
                return str(path.resolve())
        except OSError:
            pass

        if not self.rvc_voice_name:
            result = self._run_async(self._synth_final_async(clean, str(path), emotion=emotion))
            self._prune_cache()
            return result

        tmp = path.with_name(f".{path.stem}.pending{path.suffix}")
        produced, converted = self._run_async(
            self._synth_and_convert(clean, str(tmp), emotion=emotion))
        if converted:
            os.replace(tmp, path)              # 变声成功 → 正式进缓存
            self._prune_cache()
            return str(path.resolve())
        # 变声没生效：挪到一个普通临时名返回（能播，但不占"已变声"的缓存键）
        keep = self._output_path(None)
        try:
            os.replace(tmp, keep)
            return str(keep.resolve())
        except OSError:
            return produced

    def _prune_cache(self, keep: int = 400) -> None:
        """只清理本模块自己的缓存文件（``<40位sha1>.wav``），保留最新 ``keep`` 个。"""
        try:
            files = [
                p
                for p in self.cache_dir.glob("*.wav")
                if len(p.stem) == 40 and all(c in "0123456789abcdef" for c in p.stem)
            ]
            if len(files) <= keep:
                return
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in files[keep:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# 自测入口（python -m neko.tts / python neko/tts.py）
# ---------------------------------------------------------------------------

SELFTEST_TEXT = "喵～主人回来啦，今天想学点什么呀？"


def _selftest(text: str = SELFTEST_TEXT) -> int:
    import sys

    tts = NekoTTS()
    print("=== NekoTTS 自测 ===")
    for key, value in tts.diagnose().items():
        print(f"  {key:16}= {value}")
    print(f"  输入文本        = {text!r}")
    print(f"  预处理后        = {preprocess_text(text, tts.max_chars)!r}")

    if not tts.available():
        print("\n[阻塞] edge-tts 不可用（无法导入或无网络），无法完成端到端自测。")
        print("       voicestyle 的 DSP 部分仍可用：python -m neko.voicestyle in.wav out.wav")
        return 2

    out = tts.synth(text)
    samples, samplerate = load_wav(out)
    duration = len(samples) / float(samplerate or 1)
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    print("\n  输出路径        = " + out)
    print(f"  采样率          = {samplerate} Hz")
    print(f"  时长            = {duration:.3f} s")
    print(f"  峰值            = {peak:.4f}")
    print(f"  文件存在        = {Path(out).is_file()}  ({Path(out).stat().st_size} bytes)")

    # 缓存命中验证：第二次应直接命中、不再联网
    t0 = time.perf_counter()
    first = tts.synth_to_cache(text)
    t1 = time.perf_counter()
    second = tts.synth_to_cache(text)
    t2 = time.perf_counter()
    print(f"  缓存路径        = {second}")
    print(f"  缓存命中        = {first == second and Path(second).is_file()}"
          f"  (首次 {t1 - t0:.2f}s / 二次 {t2 - t1:.4f}s)")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_selftest(sys.argv[1] if len(sys.argv) > 1 else SELFTEST_TEXT))
