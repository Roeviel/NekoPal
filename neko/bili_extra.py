"""B站补充内容源：弹幕 与 评论区（**全部免登录**）。

为什么需要这个模块
------------------
实测结论（BV1Sz4y1U77N，未登录）：

    x/player/v2 与 x/player/wbi/v2 都返回 code: 0，
    但 data.subtitle.subtitles == []，data.subtitle.ai_subtitles 直接是 null。
    → B 站不对未登录游客返回 AI 字幕列表。

这等于说：只靠字幕，免登录时"学视频"就退化成"只读标题"，教学价值几乎为零。
但另外两个源完全开放，而且内容很实：

    弹幕     /x/v1/dm/list.so?oid={cid}          → 2502 条，
             例如「int为整形，即数字，float为浮点型，即带小数点，str为字符串，bool为布尔值」
    评论区   /x/v2/reply/main?type=1&oid={aid}   → code 0，19 条，
             例如「一、Python的基础知识，包括变量声明、数据类型、注释、输出语句…」

注意：`x/v2/reply/wbi/main` 返回 **-403 访问权限不足**，必须用非 wbi 的 `reply/main`。

诚实边界
--------
弹幕和评论是**观众说的话，不是视频的原话**。所以上层必须在提示词里如实标注来源，
让猫娘说"观众在弹幕里提到…"而不是"视频里讲了…"。这里只负责取干净数据，
不负责措辞——但模块文档把这条约束写清楚。
"""

from __future__ import annotations

import html
import math
import re
from collections import Counter
from typing import Any

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DANMAKU_URLS = (
    "https://api.bilibili.com/x/v1/dm/list.so?oid={cid}",
    "https://comment.bilibili.com/{cid}.xml",
)
REPLY_URL = "https://api.bilibili.com/x/v2/reply/main"

_D_RE = re.compile(r"<d\s+p=\"[^\"]*\"\s*>(.*?)</d>", re.DOTALL)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_MEANINGLESS = (
    "前排", "哈哈", "233", "awsl", "考古", "打卡", "签到", "来了", "我来了",
    "好耶", "厉害", "太强", "牛逼", "牛", "6", "泪目", "破防", "哈哈哈哈哈",
)


def _clean(text: str) -> str:
    """清洗一条弹幕：反转义、去标签、压缩空白。"""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _informative(text: str, min_chars: int) -> bool:
    """判断一条弹幕是否"有信息量"。"""
    if len(text) < min_chars:
        return False
    # 纯符号 / 纯表情 / 纯数字直接丢
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", text):
        return False

    # 去掉包裹的括号和标点后再看内核（"[OHHHHH...]" 这种刷屏要能识别出来）
    core = text.strip(" \t\r\n[]【】()（）{}<>《》「」\"'`~～!！?？.。,，、:：;；")
    if not core:
        return False
    # 重复字符刷屏（"666666"、"哈哈哈哈哈哈"）
    if re.fullmatch(r"(.)\1{2,}", core):
        return False
    if core.strip("。！？!?~～ ") in _MEANINGLESS:
        return False
    # 字符多样性过低 = 复读刷屏（长文本才判定，短文本容易误伤）
    compact = re.sub(r"\s+", "", core)
    if len(compact) >= 12 and len(set(compact)) / len(compact) < 0.22:
        return False
    return True


def _score(text: str, dup: int) -> float:
    """信息量打分：长且含中文的优先，被刷屏的降权。"""
    has_cjk = bool(_CJK_RE.search(text))
    base = min(len(text), 70)
    if has_cjk:
        base += 12
    # 含数字/英文术语的往往是知识点（如 "str为字符串"）
    if re.search(r"[A-Za-z]{2,}", text) and has_cjk:
        base += 6
    return base - 4.0 * math.log1p(max(dup - 1, 0))


def danmaku(cid: int, *, limit: int = 300, min_chars: int = 5, timeout: float = 15.0) -> list[str]:
    """取公开弹幕（无需登录），按信息量排序返回。

    失败（网络/风控/空）一律返回 []，不抛异常——上层要靠它做降级，
    不能因为一个内容源挂掉就让整条学习流程失败。
    """
    if not cid:
        return []

    body: bytes | None = None
    with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        for template in DANMAKU_URLS:
            url = template.format(cid=cid)
            try:
                resp = client.get(url)
                if resp.status_code == 200 and b"<d " in resp.content:
                    body = resp.content
                    break
            except httpx.HTTPError:
                continue
    if not body:
        return []

    raw_texts: list[str] = []
    for match in _D_RE.findall(body.decode("utf-8", errors="replace")):
        cleaned = _clean(match)
        if cleaned:
            raw_texts.append(cleaned)
    if not raw_texts:
        return []

    counts = Counter(raw_texts)
    seen: set[str] = set()
    scored: list[tuple[float, str]] = []
    for text in raw_texts:
        if text in seen:
            continue
        seen.add(text)
        if not _informative(text, min_chars):
            continue
        scored.append((_score(text, counts[text]), text))

    scored.sort(key=lambda item: -item[0])
    return [text for _, text in scored[:limit]]


def comments(
    aid: int, *, limit: int = 20, min_like: int = 0, timeout: float = 15.0
) -> list[dict[str, Any]]:
    """取热门评论（无需登录）。返回 [{"message","like","reply"}]，按点赞降序。"""
    if not aid:
        return []

    params = {"type": 1, "oid": aid, "mode": 3, "ps": max(20, min(limit * 2, 49))}
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(REPLY_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return []

    if payload.get("code") != 0:
        return []

    replies = ((payload.get("data") or {}).get("replies") or [])
    out: list[dict[str, Any]] = []
    for item in replies:
        if not isinstance(item, dict):
            continue
        message = _clean(((item.get("content") or {}).get("message") or ""))
        if not message:
            continue
        like = int(item.get("like") or 0)
        if like < min_like:
            continue
        out.append({"message": message, "like": like, "reply": int(item.get("rcount") or 0)})

    out.sort(key=lambda x: -x["like"])
    return out[:limit]


def build_audience_text(
    title: str,
    danmaku_list: list[str],
    comment_list: list[dict[str, Any]],
    *,
    max_chars: int = 9000,
    danmaku_n: int = 120,
    comment_n: int = 12,
) -> str:
    """把弹幕和评论拼成给模型看的"观众视角"材料。

    刻意保留"这是观众说的"这一层语义，避免把观众的话当成视频原话。
    """
    chunks = [f"【视频标题】{title}", "【以下是观众在弹幕和评论区说的话，**不是视频原话**】"]
    if danmaku_list:
        chunks.append("— 弹幕（已按信息量筛选，长的通常是讲解或纠错）—")
        chunks.extend(danmaku_list[:danmaku_n])
    if comment_list:
        chunks.append("— 热门评论 —")
        for c in comment_list[:comment_n]:
            like = c.get("like") or 0
            prefix = f"[{like}赞] " if like else ""
            chunks.append(f"{prefix}{c.get('message', '')}")
    text = "\n".join(chunks)
    return text[:max_chars]


if __name__ == "__main__":  # 自检：python -m neko.bili_extra
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from neko.bilibili import BiliClient
    from neko.config import load_config

    BV = sys.argv[1] if len(sys.argv) > 1 else "BV1Sz4y1U77N"
    with BiliClient(load_config()) as bili:
        info = bili.video(BV)
        dm = danmaku(info["cid"])
        cm = comments(info["aid"])
    print(f"视频：{info['title']}")
    print(f"弹幕：{len(dm)} 条（清洗+排序后）")
    for t in dm[:5]:
        print("   ·", t)
    print(f"评论：{len(cm)} 条")
    for c in cm[:3]:
        print(f"   · [{c['like']}赞] {c['message'][:70]}")
