"""B 站官方表情包：有标签、有人审核，比全网乱搜靠谱得多。

## 为什么改成这个

原来她发表情包走的是「Bing 图片搜索」，问题是**搜到的图和当下情绪没有任何
语义关联** —— 程序只会按关键词哈希在候选池里错开取，图片内容是什么它并不知道。
用户就收到过一张"××眼（动漫里表示死了/昏过去）"的表情，配在她刚问完一个
正经问题的后面，非常突兀。

B 站官方表情（`x/emote/user/panel/web`）完全不同：

- **人工审核过**，不会出现不合时宜的内容；
- **每个都有文字标签**（难过 / 生气 / 委屈 / 惊吓 / 思考 / 疑问 / 微笑…），
  标签天然能对上情绪，等于"选表情"变成了有依据的映射而不是碰运气；
- 走 B 站自己的 CDN（`i0.hdslb.com`），稳定、可外链、不用下载缓存。

实测（登录后）能拿到 4 个包共 **411 个**表情：小黄脸 225、热词系列 84、
tv_小电视 50、颜文字 52。

**注意**：这个接口**必须登录**。未登录时返回 `code: -101`，一个都拿不到。
所以没登录时会自动退回网络搜图那条路。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

PANEL_URL = "https://api.bilibili.com/x/emote/user/panel/web"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: 情绪 -> 优先匹配的标签关键词（按顺序取第一个存在的）。
#: 键名和 neko/prosody.py 的情绪名保持一致，一套词汇两处复用。
EMOTION_LABELS: dict[str, list[str]] = {
    "happy":    ["呲牙", "微笑", "喜极而泣", "tv_微笑"],
    "laugh":    ["笑哭", "喜极而泣", "tv_坏笑", "大笑"],
    "love":     ["星星眼", "tv_微笑", "微笑", "热词系列_世萌双冠"],
    "smug":     ["tv_坏笑", "滑稽", "tv_斜眼笑", "肥肠自信", "热词系列_我故意的"],
    "ok":       ["OK", "打call", "点赞", "热词系列_三连"],
    "think":    ["tv_思考", "思考", "热词系列_知识增加"],
    "surprise": ["tv_惊吓", "啊?", "惊吓", "热词系列_啊?"],
    "angry":    ["tv_生气", "tv_发怒", "发怒"],
    "sad":      ["tv_难过", "难过", "委屈", "tv_委屈"],
    "cry":      ["大哭", "tv_难过"],
    "sleepy":   ["tv_呆", "呆"],
    "question": ["tv_疑问", "疑问", "热词系列_啊?"],
    "neutral":  ["微笑", "tv_微笑"],
}

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "emotes": [], "cookie": ""}
_CACHE_TTL = 1800.0     # 半小时；表情包不会天天变


def _clean(label: str) -> str:
    return str(label or "").strip().strip("[]【】")


def fetch(cfg: dict[str, Any], timeout: float = 15.0) -> list[dict[str, str]]:
    """拉取官方表情列表 [{label, url}]。未登录或失败返回 []。"""
    cookie = str(((cfg or {}).get("bilibili") or {}).get("cookie") or "").strip()
    if not cookie:
        return []
    now = time.time()
    with _cache_lock:
        if (_cache["emotes"] and _cache["cookie"] == cookie
                and now - float(_cache["ts"]) < _CACHE_TTL):
            return list(_cache["emotes"])

    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Cookie": cookie}
    try:
        with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(PANEL_URL, params={"business": "reply"})
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return []
    if payload.get("code") != 0:
        return []

    out: list[dict[str, str]] = []
    for pkg in (payload.get("data") or {}).get("packages") or []:
        for e in pkg.get("emote") or []:
            url = str(e.get("url") or e.get("gif_url") or "").strip()
            label = _clean(e.get("text") or e.get("name"))
            if url and label:
                out.append({"label": label, "url": url})
    if out:
        with _cache_lock:
            _cache.update(ts=now, emotes=out, cookie=cookie)
        print(f"[emotes] 拿到 {len(out)} 个官方表情")
    return out


def pick(emotion: str, cfg: dict[str, Any]) -> dict[str, str] | None:
    """按情绪挑一个官方表情。挑不到返回 None（上层会优雅降级）。

    注意：**情绪名不在表里就直接返回 None**，不要退回"微笑"。否则她写错一个键
    （比如 ``[表情:happyy]``）会被静默换成一张笑脸，问题就被藏起来了。
    """
    if emotion not in EMOTION_LABELS:
        return None
    emotes = fetch(cfg)
    if not emotes:
        return None
    by_label = {e["label"]: e for e in emotes}

    # 1) 精确命中优先
    for want in EMOTION_LABELS[emotion] + EMOTION_LABELS["neutral"]:
        if want in by_label:
            return by_label[want]
    # 2) 退一步做子串匹配
    for want in EMOTION_LABELS[emotion]:
        for e in emotes:
            if want and want in e["label"]:
                return e
    return None


def labels(cfg: dict[str, Any], limit: int = 0) -> list[str]:
    """当前可用的表情标签（给提示词用）。"""
    names = [e["label"] for e in fetch(cfg)]
    return names[:limit] if limit else names


if __name__ == "__main__":  # 自检：python -m neko.emotes
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .config import load_config

    cfg = load_config()
    emotes = fetch(cfg)
    print("官方表情总数:", len(emotes))
    if not emotes:
        print("（没拿到 —— 多半是没登录）")
        raise SystemExit(1)
    print()
    print(f"  {'情绪':<10}{'标签':<24}地址")
    print("  " + "-" * 74)
    for emo in EMOTION_LABELS:
        got = pick(emo, cfg)
        if got:
            print(f"  {emo:<10}{got['label']:<24}{got['url'][:44]}")
        else:
            print(f"  {emo:<10}(挑不到)")
