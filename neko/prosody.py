"""语气（prosody）：让同一句话在开心/低落/生气时听起来不一样。

## 为什么需要这个

edge-tts 的每条回复都用同一套固定参数（rate/pitch/volume）去合成，
所以"太好了！我就知道你能行！"和"唉……算了，不想说了"听起来一模一样 ——
用户的原话是「声音没有语气的变化」。

## 为什么只能这么做

实测过的三条路（结论都写在代码里，别重复踩）：

1. **`mstts:express-as` 表现力风格 —— 不可用。**
   传 SSML 进去，edge-tts 会把它**转义成纯文本**，服务会逐字朗读
   `<speak version="1.0" ...>` 这堆标签。证据：同一句话纯文本 0.3 秒，
   传 SSML 变 4.7 秒（长 15 倍），且所有风格时长几乎一样。
2. **换更会演的引擎**（GPT-SoVITS / CosyVoice 之类）——需要 GPU 和几个 GB 下载，
   本机不现实，作为以后的升级项。
3. **`rate` / `pitch` / `volume` —— 真实可用**，实测：
   rate ±20% 让时长 4.13s 变 3.46s / 5.16s；
   pitch ±30Hz 让 F0 从 221Hz 变 240Hz / 181Hz；
   "激动"组合 (3.70s, 245Hz) 与"低落"组合 (4.80s, 195Hz) 差 50Hz、1.1 秒 —— 耳朵能听出来。
   所以本模块就在这三个参数上做文章。

## 情绪信号从哪来

优先用她回复里的 `[表情:xxx]` 标记 —— 那是**模型自己给的情绪标签**，
比猜标点可靠得多，而且已经有现成的 12 个类别。
没有标记时再退回到标点/关键词的启发式。
"""

from __future__ import annotations

import re
from typing import Any

#: 表情标记 -> 语气参数。数值是**相对基准配置的增量**，
#: 基准通常已经是 rate=+0% / volume=+0% / pitch=0，所以直接当绝对值也差不多。
EMOTION_PROSODY: dict[str, dict[str, Any]] = {
    "happy":    {"rate": "+14%", "pitch": "+26Hz", "volume": "+8%"},
    "laugh":    {"rate": "+18%", "pitch": "+30Hz", "volume": "+10%"},
    "love":     {"rate": "+6%",  "pitch": "+16Hz", "volume": "+4%"},
    "surprise": {"rate": "+12%", "pitch": "+24Hz", "volume": "+6%"},
    "angry":    {"rate": "+10%", "pitch": "+14Hz", "volume": "+12%"},
    "smug":     {"rate": "-2%",  "pitch": "+8Hz",  "volume": "+0%"},
    "ok":       {"rate": "+4%",  "pitch": "+6Hz",  "volume": "+0%"},
    "question": {"rate": "+2%",  "pitch": "+10Hz", "volume": "+0%"},
    "think":    {"rate": "-8%",  "pitch": "-4Hz",  "volume": "-2%"},
    "sad":      {"rate": "-16%", "pitch": "-18Hz", "volume": "-10%"},
    "cry":      {"rate": "-20%", "pitch": "-22Hz", "volume": "-12%"},
    "sleepy":   {"rate": "-22%", "pitch": "-14Hz", "volume": "-14%"},
    "neutral":  {"rate": "+0%",  "pitch": "+0Hz",  "volume": "+0%"},
}

_MARK_RE = re.compile(r"\[表情:([A-Za-z_]+)\]")

#: 没有表情标记时，用标点和关键词退而求其次
_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(哈哈|笑死|太好|真棒|厉害|恭喜|太好了|漂亮)"), "happy"),
    (re.compile(r"(唉|叹气|算了|好累|难受|烦|难过|没意思|不想说)"), "sad"),
    (re.compile(r"(生气|过分|讨厌|凭什么|可恶)"), "angry"),
    (re.compile(r"(居然|竟然|没想到|真的假的|不会吧)"), "surprise"),
    (re.compile(r"(困|睡|晚安|累了)"), "sleepy"),
    (re.compile(r"(你确定|是吗|真的吗|怎么|为什么|什么)"), "question"),
]


def detect_emotion(text: str) -> str:
    """从回复文本里判断情绪。优先看 `[表情:x]` 标记，其次看标点/关键词。"""
    body = str(text or "")
    mark = _MARK_RE.search(body)
    if mark:
        key = mark.group(1)
        if key in EMOTION_PROSODY:
            return key
    for pattern, emotion in _HINTS:
        if pattern.search(body):
            return emotion
    if "！" in body or "!" in body:
        return "happy"
    if "？" in body or "?" in body:
        return "question"
    if "……" in body or "…" in body:
        return "sad"
    return "neutral"


def prosody_for(text: str, emotion: str | None = None) -> dict[str, str]:
    """整条回复的语气参数。emotion 显式给出时优先用它。"""
    key = emotion if (emotion and emotion in EMOTION_PROSODY) else detect_emotion(text)
    return dict(EMOTION_PROSODY[key])


def strip_markers(text: str) -> str:
    """去掉 `[表情:x]` / `[图片:...]` / `[学习:...]` 这些标记，只剩下要念出来的话。

    标记是给程序看的，念出来就出戏了。
    """
    out = re.sub(r"\[(?:表情|图片|学习):[^\]]+\]", "", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _sentence_prosody(sentence: str, index: int, base: dict[str, str]) -> dict[str, str]:
    """在整条回复的语气基础上，按句子的收尾标点再微调。

    这是让"一句话内部也有起伏"的关键：疑问句往上扬、感叹句加快、
    以省略号收尾的往下沉。同时给相邻句子一点交替偏移，避免每句都一个调。
    """
    rate = _pct(base.get("rate", "+0%"))
    pitch = _hz(base.get("pitch", "+0Hz"))
    volume = _pct(base.get("volume", "+0%"))

    tail = sentence.rstrip()
    if tail.endswith(("？", "?")):
        pitch += 12
        rate -= 4
    elif tail.endswith(("！", "!")):
        rate += 8
        pitch += 10
    elif tail.endswith(("……", "…", "。")):
        rate -= 6 if tail.endswith(("……", "…")) else 0
        pitch -= 8 if tail.endswith(("……", "…")) else 0
    elif tail.endswith(("，", ",")):
        pitch += 4          # 逗号处轻轻提一下，像换气

    # 交替偏移：连续几句同一个调子最像机器人
    pitch += 6 if index % 2 == 0 else -4
    # 太长的句子稍微放慢，否则一口气念不完
    if len(sentence) > 40:
        rate -= 6

    return {"rate": _fmt_pct(rate), "pitch": _fmt_hz(pitch), "volume": _fmt_pct(volume)}


def split_sentences(text: str) -> list[str]:
    """按中文标点断句，保留标点。太短的碎句会和前一句合并。"""
    text = str(text or "").strip()
    if not text:
        return []
    raw = re.split(r"(?<=[。！？!?；;…])", text)
    out: list[str] = []
    for piece in raw:
        piece = piece.strip()
        if not piece:
            continue
        if out and len(piece) < 4:
            out[-1] += piece          # "嗯。" 这种跟前面合并，免得单独合成很怪
        else:
            out.append(piece)
    return out


def plan(text: str, *, max_sentences: int = 6, emotion: str | None = None) -> list[dict[str, str]]:
    """把一条回复拆成 [(句子, 该句的语气参数)]。

    句子太多（> max_sentences）就退化成整条一起合成：一来避免十几次 TTS 调用，
    二来碎句子各自合成会明显听出拼接感。

    ``emotion`` 由调用方（Brain.voice）从**带标记的原文**里判定后传进来 ——
    因为传给 TTS 的文本已经把 `[表情:x]` 剥掉了，不显式传的话就丢了
    最可靠的那个情绪信号。
    """
    body = strip_markers(text)
    sentences = split_sentences(body)
    if len(sentences) <= 1 or len(sentences) > max_sentences:
        return [{"text": body, **prosody_for(text, emotion)}]
    base = prosody_for(text, emotion)
    return [{"text": s, **_sentence_prosody(s, i, base)} for i, s in enumerate(sentences)]


# ---------- 小工具 ----------

def _pct(value: str) -> int:
    m = re.match(r"([+-]?\d+)", str(value or "0"))
    return int(m.group(1)) if m else 0


def _hz(value: str) -> int:
    m = re.match(r"([+-]?\d+)", str(value or "0"))
    return int(m.group(1)) if m else 0


def _fmt_pct(v: int) -> str:
    return f"{v:+d}%"


def _fmt_hz(v: int) -> str:
    return f"{v:+d}Hz"
