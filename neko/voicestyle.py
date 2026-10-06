"""猫娘音色（Neko Voice Style）。

本模块**直接复用**同机已有的纯 CPU 变声项目 ``VoiceChanger``
（``<VoiceChanger 仓库>/voice_changer``）里的 DSP 处理链，
把任意单人声（本项目的上游是 edge-tts 的合成语音）离线转成「猫娘音色」，
不重写任何 DSP 算法。

VoiceChanger 是**外部可选依赖**，不在本仓库里。定位顺序见
``find_voicechanger_root()``：环境变量 ``NEKO_VOICECHANGER_ROOT`` →
本项目同级目录下的 ``VoiceChanger``。找不到时语音链路会跳过 DSP（只出 edge-tts 原声），
不会让聊天失败。

复用内容
--------
- ``voice_changer.presets.get_preset("少御音")``
  「少御音」预设是最接近猫娘的底子（年轻清甜、略御姐）：它的 EQ /
  压缩 / 限幅参数被整体沿用。
- ``voice_changer.config.EngineConfig`` + ``resolve_preset_params()``
  预设与用户覆盖项的合并逻辑（等价于 VoiceChanger GUI 滑块的合并方式）。
- ``voice_changer.dsp.chain.DSPChain``
  完整离线处理链：变调 → 共振峰 → 均衡 → 压缩 → 混响 → 限幅 → 输出增益。
  其中包含（均为 numpy 实现、无 scipy/librosa/GPU 依赖）：
  ``PitchShifter``（自研相位声码器变调）、``FormantShifter``（倒频谱包络搬移）、
  ``ParametricEQ``/``BiquadFilter``（RBJ 双二阶）、``Compressor``、``Reverb``
  （Schroeder 梳状+全通）、``Limiter``。

离线调用方式（关键）
--------------------
``DSPChain`` 虽为实时流式设计，但 ``process(x)`` 对**整段**数组调用时天然成立：
``PitchShifter.process`` 内部会前置一段零值 ``overlap`` 做预热、跨块交叉淡化，
单次调用时 ``_tail_out is None`` 分支使输出长度与输入**严格相等**；
``FormantShifter``、各 biquad、压缩器、混响、限幅器都只为整段维护一份状态。
因此「整段一次性喂进 ``DSPChain.process``」即可得到离线结果，无需分块循环。
本模块把 ``chunk`` 设为不小于 ``2*overlap``，保证 PitchShifter 的
``overlap`` 不被 ``chunk // 2`` 压小。

采样率策略
----------
**不重采样**：``apply_neko_style`` 保持输入采样率不变（返回数组与输入同采样率），
``samplerate`` 参数只用于让 DSP 链按正确的时间尺度计算双二阶系数、混响延时
与压缩器时间常数。edge-tts 的输出是 ``audio-24khz-48kbitrate-mono-mp3``，
即 **24000 Hz**，所以整条语音链路的实际采样率就是 24000 Hz；若上游给了别的
采样率（例如 48000），也原样保留。

输出样本统一为 **float32、单声道**，并在最后做一次峰值归一化/限幅，
目标峰值 ``PEAK_TARGET = 0.95``，避免削波。

性能提示
--------
``ParametricEQ`` / ``Compressor`` / ``Limiter`` / ``Reverb`` 是逐样本的
Python 循环，实测约 0.3~0.5 倍实时速度：3 秒语音约 0.1 秒，60 秒语音约
20 秒。语音缓存（``NekoTTS.synth_to_cache``）正是为了摊掉这个开销。
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 应用音色后的目标峰值（避免削波，同时保证响度足够）
PEAK_TARGET: float = 0.95
#: 归一化时允许的最大向上提升量（dB）。20 dB 足以把正常语音拉到目标峰值，
#: 同时挡住对「几乎静音」片段的病态放大。
MAX_MAKEUP_DB: float = 20.0
#: 静音门限（-50 dBFS）：低于此峰值视为静音，不做向上归一化
SILENCE_FLOOR: float = 10.0 ** (-50.0 / 20.0)
#: 猫娘默认使用的底子预设（来自 VoiceChanger）
BASE_PRESET_NAME: str = "少御音"

#: 在「少御音」预设之上追加的「更甜更轻」EQ 调味（猫娘化）。
#: 频点按 24 kHz 采样率选取，Nyquist = 12 kHz。
NEKO_EQ_EXTRA: tuple[dict[str, Any], ...] = (
    # 切掉 90 Hz 以下的隆隆声/喷麦，让声音更"轻"
    {"type": "highpass", "freq": 90.0, "gain_db": 0.0, "q": 0.707},
    # 再削一点胸腔厚度，减少"御姐"感，向少女/猫娘靠
    {"type": "lowshelf", "freq": 250.0, "gain_db": -2.5, "q": 0.7},
    # 3.2 kHz 附近的甜感/清晰度（F3 区域）
    {"type": "peak", "freq": 3200.0, "gain_db": 1.6, "q": 1.1},
    # 9 kHz 以上补空气感（"轻"）。注意预设 5.5 kHz 那段的 shelf 尾巴会一直压到
    # 11 kHz，所以这里必须给到 +4 dB，实测 11 kHz 才净得 +2.5 dB。
    {"type": "highshelf", "freq": 9000.0, "gain_db": 4.0, "q": 0.6},
)

#: 「少御音」预设是为「男声 +6 半音」调的去金属配置：5.5 kHz 之上压了 -8 dB。
#: 但 edge-tts 的女声底子本身干净，实测原样沿用会把频谱质心从 2502 Hz 拉到
#: 1919 Hz（-23%，听感变"闷"），与猫娘要的"更甜更轻"**方向相反**。
#: 收敛到 -1.5 dB 后：质心 2429 Hz（原声的 97%，不再发闷），而 3~8 kHz 谱平坦度
#: 0.566 仍显著低于原声的 0.615，说明相位声码器的高频毛刺没有因此被放出来
#: （多出来的高频是变调本身带上来的谐波，不是噪声）。
#: 仅对 4~8 kHz 的 highshelf 生效，不影响 9 kHz 的空气感 shelf。
PRESET_HIGHSHELF_SOFTEN_MIN_FREQ: float = 4000.0
PRESET_HIGHSHELF_SOFTEN_MAX_FREQ: float = 8000.0
PRESET_HIGHSHELF_SOFTEN_DB: float = -1.5

#: 环境变量：显式指定 VoiceChanger 仓库根目录
VC_ROOT_ENV = "NEKO_VOICECHANGER_ROOT"

#: 内部别名。用独立顶层名加载 VoiceChanger，避免与环境中任何同名
#: ``voice_changer`` / ``dsp`` / ``config`` 模块冲突。
_VC_ALIAS = "_neko_voicechanger_dsp"
_VC_LOCK = threading.Lock()
_VC_CACHE: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# VoiceChanger 定位与加载
# ---------------------------------------------------------------------------


def find_voicechanger_root() -> Path:
    """定位 VoiceChanger 项目根目录。可用 ``NEKO_VOICECHANGER_ROOT`` 覆盖。"""
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    env = os.environ.get(VC_ROOT_ENV)
    if env:
        candidates.append(Path(env))
    # NekoPal/neko/voicestyle.py -> NekoPal -> partner（同级目录）
    candidates.append(here.parent.parent.parent / "VoiceChanger")
    for cand in candidates:
        if (cand / "voice_changer" / "dsp" / "chain.py").is_file():
            return cand
    raise FileNotFoundError(
        "找不到 VoiceChanger 项目（需要 <root>/voice_changer/dsp/chain.py）。"
        f"已尝试：{[str(c) for c in candidates]}；"
        f"可设置环境变量 {VC_ROOT_ENV} 指定路径。"
    )


def _load_voicechanger(root: Path | None = None):
    """把 VoiceChanger 作为独立别名包加载，返回 ``(dsp_chain_mod, preset_params_mod)``。

    采用 ``importlib`` 以 ``_neko_voicechanger_dsp`` 之名注册包，
    使其内部的相对导入（``from ..config import BiquadCoeffs``）在别名空间内解析，
    彻底避开模块名冲突与 ``sys.path`` 污染。只执行轻量的
    ``voice_changer/__init__.py``（不含 sounddevice 等重依赖）。
    """
    with _VC_LOCK:
        if "loaded" in _VC_CACHE:
            return _VC_CACHE["loaded"]

        root = Path(root) if root else find_voicechanger_root()
        pkg_dir = root / "voice_changer"
        if not (pkg_dir / "dsp" / "chain.py").is_file():
            raise FileNotFoundError(f"VoiceChanger 包不完整：{pkg_dir}")

        if _VC_ALIAS in sys.modules:
            # 已加载过（可能是本进程早先调用）
            mod = sys.modules[_VC_ALIAS]
            loaded = (
                importlib.import_module(_VC_ALIAS + ".dsp.chain"),
                importlib.import_module(_VC_ALIAS + ".config"),
                mod,
            )
            _VC_CACHE["loaded"] = loaded
            return loaded

        spec = importlib.util.spec_from_file_location(
            _VC_ALIAS,
            pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:  # pragma: no cover - 理论不可达
            raise ImportError(f"无法为 {pkg_dir} 构造 import spec")

        package = importlib.util.module_from_spec(spec)
        sys.modules[_VC_ALIAS] = package
        try:
            spec.loader.exec_module(package)
            chain_mod = importlib.import_module(_VC_ALIAS + ".dsp.chain")
            config_mod = importlib.import_module(_VC_ALIAS + ".config")
        except Exception:
            # 失败时清理，避免留下半初始化模块污染后续 import
            for name in [n for n in sys.modules if n == _VC_ALIAS or n.startswith(_VC_ALIAS + ".")]:
                sys.modules.pop(name, None)
            raise

        _VC_CACHE["root"] = root
        _VC_CACHE["loaded"] = (chain_mod, config_mod, package)
        return _VC_CACHE["loaded"]


# ---------------------------------------------------------------------------
# 音色参数
# ---------------------------------------------------------------------------


def neko_style_params(
    samplerate: int,
    *,
    pitch_semitones: float = 5.0,
    formant_shift: float = 0.18,
    reverb: float = 0.12,
    gain_db: float = 0.0,
) -> dict[str, Any]:
    """基于「少御音」预设生成猫娘 DSP 参数 dict（可直接喂给 ``DSPChain``）。

    - 底子：``presets.get_preset("少御音")``（EQ / 压缩 / 限幅整体沿用）
    - 覆盖：``pitch_semitones`` / ``formant_shift`` / ``reverb`` / ``output_gain_db``
    - 加料：``NEKO_EQ_EXTRA``（更甜更轻的高频空气感 + 低频减重）
    """
    _chain_mod, config_mod, _pkg = _load_voicechanger()

    params: dict[str, Any] | None = None
    try:
        engine_cfg = config_mod.EngineConfig(
            samplerate=int(samplerate),
            preset_name=BASE_PRESET_NAME,
            pitch_semitones=float(pitch_semitones),
            formant_shift=float(formant_shift),
            reverb_wet=float(reverb),
            output_gain_db=float(gain_db),
        )
        params = config_mod.resolve_preset_params(engine_cfg)
    except Exception:
        params = None

    if params is None:
        # 退路：手工复刻 resolve_preset_params 的合并语义（保持行为一致）
        preset_mod = importlib.import_module(_VC_ALIAS + ".presets")
        base = preset_mod.get_preset(BASE_PRESET_NAME)
        params = dict(base)
        params["eq"] = [dict(b) for b in base.get("eq", []) or []]
        params["compressor"] = dict(base.get("compressor", {}))
        params["reverb"] = dict(base.get("reverb", {}))
        params["limiter"] = dict(base.get("limiter", {}))
        params["pitch_semitones"] = float(pitch_semitones)
        params["formant_shift"] = float(formant_shift)
        params["reverb"]["wet"] = float(reverb)
        params["output_gain_db"] = float(gain_db)

    # 猫娘调味：追加 EQ（线性滤波器串联，顺序不影响总响应）
    eq = [dict(b) for b in NEKO_EQ_EXTRA] + [dict(b) for b in params.get("eq", []) or []]
    # 收敛预设里过强的去金属高频压暗（见 PRESET_HIGHSHELF_SOFTEN_* 注释）
    for band in eq:
        if (
            band.get("type") == "highshelf"
            and PRESET_HIGHSHELF_SOFTEN_MIN_FREQ
            <= _as_float(band.get("freq"), 0.0)
            < PRESET_HIGHSHELF_SOFTEN_MAX_FREQ
            and _as_float(band.get("gain_db"), 0.0) < PRESET_HIGHSHELF_SOFTEN_DB
        ):
            band["gain_db"] = PRESET_HIGHSHELF_SOFTEN_DB
    params["eq"] = eq
    params["name"] = "猫娘音色"
    return params


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def _to_mono_float32(samples, samplerate: int | None = None) -> np.ndarray:
    """任意形状/类型的样本 → (n,) float32 单声道。多声道按算术平均下混。"""
    arr = np.asarray(samples)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)

    if arr.dtype == np.float32:
        out = arr
    elif np.issubdtype(arr.dtype, np.floating):
        out = arr.astype(np.float32)
    elif np.issubdtype(arr.dtype, np.integer):
        # 按整型满量程归一化到 [-1, 1)
        info = np.iinfo(arr.dtype)
        scale = float(max(abs(info.min), info.max))
        out = (arr.astype(np.float32) / scale).astype(np.float32)
    else:
        out = arr.astype(np.float32)

    if out.ndim == 2:
        # soundfile 约定 (frames, channels)
        out = out.mean(axis=1) if out.shape[1] <= out.shape[0] else out.mean(axis=0)
    elif out.ndim > 2:
        out = out.reshape(out.shape[0], -1).mean(axis=1)

    return np.ascontiguousarray(out, dtype=np.float32)


def load_wav(path) -> tuple["np.ndarray", int]:
    """返回 (float32 单声道 samples, samplerate)。

    ``path`` 可以是文件路径，也可以是任意 file-like 对象（如 ``io.BytesIO``）。
    只要底层 libsndfile 支持就能读：wav / flac / ogg / **mp3** ...
    （``soundfile`` 0.13+ 捆绑的 libsndfile >= 1.1 自带 MP3 解码，无需 ffmpeg）。
    """
    import soundfile as sf

    source = path if hasattr(path, "read") else str(path)
    data, samplerate = sf.read(source, dtype="float32", always_2d=True)
    return _to_mono_float32(data), int(samplerate)


def save_wav(path, samples, samplerate: int) -> None:
    """写 16-bit PCM 单声道 wav（微信/播放器兼容性最好）。自动建父目录。"""
    import soundfile as sf

    target = Path(path)
    if target.parent and str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    mono = _to_mono_float32(samples)
    # 写盘前做最终削波保护
    mono = np.clip(mono, -1.0, 1.0).astype(np.float32)
    sf.write(str(target), mono, int(samplerate), subtype="PCM_16", format="WAV")


def _ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def decode_audio_bytes(data: bytes) -> tuple["np.ndarray", int]:
    """解码内存中的音频字节（edge-tts 给的是 mp3）→ (float32 单声道, 采样率)。

    多级回退，全部失败才抛 ``RuntimeError``：
    1. ``soundfile`` / libsndfile（libsndfile >= 1.1 支持 MP3，**首选**，无外部依赖）
    2. ``miniaudio``（若已安装）
    3. 外部 ``ffmpeg`` 可执行文件（若在 PATH 中）
    4. ``pydub``（依赖 ffmpeg）
    """
    if not data:
        raise ValueError("decode_audio_bytes: 空数据")

    errors: list[str] = []

    # 1) soundfile（libsndfile 原生 mp3）
    try:
        return load_wav(io.BytesIO(data))
    except Exception as exc:
        errors.append(f"soundfile: {exc!r}")

    # 2) miniaudio
    try:
        import miniaudio  # type: ignore

        decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32)
        arr = np.asarray(decoded.samples, dtype=np.float32)
        if getattr(decoded, "nchannels", 1) > 1:
            arr = arr.reshape(-1, decoded.nchannels)
        return _to_mono_float32(arr), int(decoded.sample_rate)
    except Exception as exc:
        errors.append(f"miniaudio: {exc!r}")

    # 3) 外部 ffmpeg
    exe = _ffmpeg_path()
    if exe:
        tmp_dir = tempfile.mkdtemp(prefix="neko_ffmpeg_")
        try:
            src = Path(tmp_dir) / "in.audio"
            dst = Path(tmp_dir) / "out.wav"
            src.write_bytes(data)
            proc = subprocess.run(
                [exe, "-v", "error", "-y", "-i", str(src),
                 "-ac", "1", "-ar", "24000", "-f", "wav", str(dst)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and dst.is_file():
                return load_wav(dst)
            errors.append(f"ffmpeg: rc={proc.returncode} {proc.stderr[:200]!r}")
        except Exception as exc:
            errors.append(f"ffmpeg: {exc!r}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 4) pydub
    try:
        from pydub import AudioSegment  # type: ignore

        seg = AudioSegment.from_file(io.BytesIO(data)).set_channels(1).set_sample_width(2)
        arr = np.frombuffer(seg.raw_data, dtype="<i2").astype(np.float32) / 32768.0
        return arr, int(seg.frame_rate)
    except Exception as exc:
        errors.append(f"pydub: {exc!r}")

    raise RuntimeError(
        "无法解码音频字节（edge-tts 输出为 mp3）。已尝试的解码器都失败：\n  - "
        + "\n  - ".join(errors)
        + "\n请升级 soundfile（>=0.13 捆绑的 libsndfile>=1.1 自带 MP3 解码），"
        "或安装 ffmpeg 到 PATH，或安装 miniaudio。"
    )


# ---------------------------------------------------------------------------
# 音色处理
# ---------------------------------------------------------------------------


def _normalize_peak(
    samples: np.ndarray,
    target: float = PEAK_TARGET,
    max_makeup_db: float = MAX_MAKEUP_DB,
    silence_floor: float = SILENCE_FLOOR,
) -> np.ndarray:
    """峰值归一化/限幅：目标峰值约 ``target``。

    - 峰值高于 ``silence_floor``（-50 dBFS）的正常语音：拉到 ``target``
    - 向上提升量上限 ``max_makeup_db``（20 dB），防止放大静音底噪
    - 峰值低于门限：原样返回（不放大）
    """
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < silence_floor:
        return samples.astype(np.float32, copy=True)

    ceiling = 10.0 ** (max_makeup_db / 20.0)
    gain = min(target / peak, ceiling)
    out = samples * gain
    # 兜底硬钳位，保证绝不削波
    out = np.clip(out, -target, target)
    return np.ascontiguousarray(out, dtype=np.float32)


def apply_neko_style(
    samples,
    samplerate: int,
    *,
    pitch_semitones: float = 5.0,
    formant_shift: float = 0.18,
    reverb: float = 0.12,
    gain_db: float = 0.0,
) -> "np.ndarray":
    """就地或返回新的 float32 单声道数组，采样率不变。

    复用 VoiceChanger 的 ``DSPChain`` 一次性处理整段音频
    （变调 → 共振峰 → EQ → 压缩 → 混响 → 限幅 → 增益），
    最后做峰值归一化，目标峰值 ``0.95``。

    参数
    ----
    pitch_semitones : 变调半音数。>0 升高。5.0 ≈ 升高纯五度附近，猫娘的"高而甜"。
    formant_shift   : 共振峰额外上移比例（0 关闭）。0.18 让声道共鸣更"小只"。
                      注意变调本身已会带动共振峰，此项是在其之上再推一把。
    reverb          : 混响干湿比 0~1，0.12 给一点空气感，过大会"罐头"。
    gain_db         : DSP 链输出增益（dB），峰值归一化在它之后执行。
    """
    mono = _to_mono_float32(samples)
    if mono.size == 0:
        return mono

    chain_mod, _config_mod, _pkg = _load_voicechanger()
    params = neko_style_params(
        int(samplerate),
        pitch_semitones=pitch_semitones,
        formant_shift=formant_shift,
        reverb=reverb,
        gain_db=gain_db,
    )

    # chunk 仅用于夹住 PitchShifter 的 overlap(=512)，取足够大即可
    chunk = max(4096, int(mono.size))
    chain = chain_mod.DSPChain(
        samplerate=int(samplerate),
        chunk=chunk,
        params=params,
        overlap=512,
        n_fft=1024,
        hop_length=256,
    )
    styled = chain.process(mono)
    styled = np.asarray(styled, dtype=np.float32).reshape(-1)

    # DSP 链的 Limiter 已把峰值压到 -1 dBFS 以内，这里统一到目标峰值
    styled = _normalize_peak(styled)
    return styled.astype(np.float32, copy=False)


def style_wav_file(
    src: str,
    dst: str,
    *,
    pitch_semitones: float = 5.0,
    formant_shift: float = 0.18,
    reverb: float = 0.12,
    gain_db: float = 0.0,
) -> tuple[str, int, float, float]:
    """「能直接吃 wav（或 mp3）」的离线入口：文件 → 文件。

    返回 ``(绝对输出路径, 采样率, 时长秒, 峰值)``。
    """
    samples, samplerate = load_wav(src)
    styled = apply_neko_style(
        samples,
        samplerate,
        pitch_semitones=pitch_semitones,
        formant_shift=formant_shift,
        reverb=reverb,
        gain_db=gain_db,
    )
    save_wav(dst, styled, samplerate)
    peak = float(np.max(np.abs(styled))) if styled.size else 0.0
    duration = styled.size / float(samplerate or 1)
    return str(Path(dst).resolve()), samplerate, duration, peak


if __name__ == "__main__":  # 手工试听：python -m neko.voicestyle in.wav out.wav
    import argparse

    parser = argparse.ArgumentParser(description="给一段 wav/mp3 上猫娘音色（离线）")
    parser.add_argument("src", help="输入音频路径（wav/flac/ogg/mp3）")
    parser.add_argument("dst", nargs="?", default="neko_style_out.wav", help="输出 wav 路径")
    parser.add_argument("--pitch", type=float, default=5.0, help="变调半音数")
    parser.add_argument("--formant", type=float, default=0.18, help="共振峰偏移")
    parser.add_argument("--reverb", type=float, default=0.12, help="混响干湿比")
    parser.add_argument("--gain-db", type=float, default=0.0, help="输出增益 dB")
    args = parser.parse_args()

    path, sr, dur, peak = style_wav_file(
        args.src, args.dst,
        pitch_semitones=args.pitch, formant_shift=args.formant,
        reverb=args.reverb, gain_db=args.gain_db,
    )
    print(f"VoiceChanger 根目录 : {_VC_CACHE.get('root')}")
    print(f"输入               : {args.src}")
    print(f"输出               : {path}")
    print(f"采样率             : {sr} Hz")
    print(f"时长               : {dur:.3f} s")
    print(f"峰值               : {peak:.4f}")
