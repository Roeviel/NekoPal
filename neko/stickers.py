"""网络表情包：搜索 + 下载缓存。

为什么要自己下载缓存，而不是直接让前端引用外链：
1. 很多图床禁止外链（Referer 校验），直接用 <img src=外链> 会碎图；
2. 前端页面是本地 http://127.0.0.1，跨域加载第三方图片在某些情况下会被拦；
3. 缓存下来之后，表情包不会因为原图被删而失效。

搜索源说明（实测结论，很重要）：
- Giphy / Tenor：**这台机器连不上**（ConnectTimeout），境外 API 在当前网络不可用；
- 搜狗图片 API：返回 {"status":1,"info":"forbid"}，被反爬挡住；
- 百度图片：返回 "Forbid spider access"；
- **Bing 图片**（cn.bing.com/images/async）：可达，能稳定解出图片地址 —— 所以用它。
  这是网页抓取，不是官方 API，Bing 改版就可能失效；所以失败时一律优雅降级，
  不能让表情包把聊天搞挂。
"""

from __future__ import annotations

import hashlib
import html as html_mod
import ipaddress
import re
import socket
import zlib
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Referer": "https://cn.bing.com/"}
BING_ASYNC = "https://cn.bing.com/images/async"

_MURL_RE = re.compile(r"murl&quot;:&quot;(.*?)&quot;")
_MURL_RE2 = re.compile(r'"murl":"(.*?)"')
_MAX_BYTES = 5 * 1024 * 1024
_ALLOWED_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF", b"BM")

#: 默认检索式前缀。为什么需要它 —— 实测（截图对比过）：
#:   「猫猫震惊」          -> 全是**真猫照片**（相机拍的猫）
#:   「猫猫震惊 表情包」    -> 还是真猫照片，只是加了配字
#:   「猫娘 震惊 表情包 二次元」-> 动漫猫娘，但多是全身立绘
#:   「动漫猫娘 表情包 可爱 q版」-> **Q 版猫娘表情包，正是要的那种**
#:   「猫娘 表情 可爱 动漫 免抠」-> 插画裁切图 + 九宫格合集
#: 所以必须显式加上"动漫 + q版 + 表情包"，否则搜出来的东西和猫娘形象不搭。
DEFAULT_SEARCH_PREFIX = "动漫猫娘 表情包 可爱 q版"

#: 这些词出现在关键词里会把结果带向真猫照片或成人内容，去掉（前缀已经交代了猫娘）
_NOISE_WORDS = ("猫猫", "猫咪", "小猫", "猫娘", "猫", "写真", "照片", "真人")

#: 这些域名基本是图库/新闻/壁纸站，出来的多半是照片或大图，不是表情包。
#: 直接**排除**（只降权是不够的：当搜索结果清一色是这些站时，降权等于没降 ——
#: 实测线上跑就返回过一排 sinaimg/nximg 的照片）。
_LOW_VALUE_DOMAINS = (
    "699pic.com", "sinaimg.cn", "itc.cn", "nximg.cn", "zcool.com.cn",
    "vcg.com", "gettyimages", "shutterstock", "zhimg.com",
    "bing.com", "zhhainiao.com", "ntimg.cn",
)

#: 这些站是表情包/图片社区，命中优先
_GOOD_DOMAINS = ("qiubiaoqing.com", "duitang.com", "dtstatic.com", "huaban.com", "bilibili.com")


def build_query(keyword: str, cfg: dict[str, Any] | None = None) -> str:
    """把她的关键词变成一条能搜到猫娘表情包的检索式。

    ``stickers.search_prefix`` 可覆盖；设成空字符串则完全用原关键词（想自己控制时用）。
    """
    conf = (cfg or {}).get("stickers") or {}
    prefix = conf.get("search_prefix")
    if prefix is None:
        prefix = DEFAULT_SEARCH_PREFIX
    prefix = str(prefix).strip()

    kw = str(keyword or "").strip()
    for noise in _NOISE_WORDS:
        kw = kw.replace(noise, " ")
    kw = re.sub(r"\s+", " ", kw).strip(" ，,、")
    # 关键词被清空（比如她只搜了"猫猫"）就只用前缀
    return f"{prefix} {kw}".strip() if kw else prefix


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


#: 最近发过的表情包 URL。她不该连着发同一张 —— 真人不会。
#: 只存进程内存：重启后清空无所谓，不值得为它落库。
_recent: "deque[str]" = deque(maxlen=24)


def _remember(urls: list[str]) -> None:
    for u in urls:
        _recent.append(u)



def _safe_url(url: str) -> bool:
    """只允许 http/https 的公网地址：防 SSRF（别让人用表情包去访问内网）。"""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    host = p.hostname
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return False
    except (OSError, ValueError):
        # 解析不了就先放行，真正的下载会失败并被吞掉
        pass
    return True


def search(
    keyword: str,
    limit: int = 12,
    timeout: float = 15.0,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按关键词搜表情包。失败返回 []，绝不抛异常（表情包不能拖垮聊天）。

    ``keyword`` 是**她给的原词**（如「猫猫震惊」）；实际发出去的检索式由
    :func:`build_query` 补上"动漫猫娘 表情包 可爱 q版"这类限定词 ——
    不补的话会搜到相机拍的真猫照片。结果还会按域名和尺寸权重排序。
    """
    query = build_query(keyword, cfg)
    if not query:
        return []
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as c:
            # 多抓一些：下面要按关键词错开取窗口，池子太小每个关键词就都是同一批图
            resp = c.get(BING_ASYNC, params={
                "q": query, "count": max(limit * 6, 40), "first": 0,
                "mkt": "zh-CN", "adlt": "off",
            })
            if resp.status_code != 200:
                return []
            text = resp.text
    except httpx.HTTPError:
        return []

    urls = _MURL_RE.findall(text) or _MURL_RE2.findall(text)
    good: list[tuple[float, str]] = []
    backup: list[tuple[float, str]] = []
    seen: set[str] = set()
    for raw in urls:
        u = html_mod.unescape(raw)
        if not u.startswith("http") or u in seen or not _safe_url(u):
            continue
        seen.add(u)
        host = _domain(u)
        score = 0.0
        if any(g in host for g in _GOOD_DOMAINS):
            score += 1.0                      # 表情包站，优先
        if u.lower().endswith(".gif"):
            score += 0.2                      # 动图通常是表情包
        item = (score, u)
        if any(b in host for b in _LOW_VALUE_DOMAINS):
            backup.append(item)               # 图库/新闻站，非必要不用
        else:
            good.append(item)

    # 优先用"干净"的那批；万一被过滤得不够（Bing 某次全返回图库站），
    # 就退回用全部结果 —— 有图总比没图强，但正常情况下轮不到这条路径。
    pool_src = good if len(good) >= limit else (good + backup)
    pool_src.sort(key=lambda x: -x[0])
    pool = [u for _, u in pool_src]

    # 跳过最近发过的，免得她连着发同一张
    if _recent:
        fresh = [u for u in pool if u not in _recent]
        if len(fresh) >= limit:
            pool = fresh

    # 按关键词哈希错开取窗口。
    # 为什么需要：前缀（"动漫猫娘 表情包 可爱 q版"）主导了检索，Bing 对
    # "震惊/无语/开心"这几个尾词的区分度很低，同一批热门图会排在最前面 ——
    # 不错开的话，她不管什么情绪都发同一张图。
    if len(pool) > limit:
        start = zlib.crc32(str(keyword).encode("utf-8")) % (len(pool) - limit + 1)
        picked = pool[start:start + limit]
        if len(picked) < limit:               # 窗口贴到尾了，从头补
            picked += [u for u in pool if u not in picked][: limit - len(picked)]
    else:
        picked = pool[:limit]

    _remember(picked)
    return [{"url": u, "title": keyword} for u in picked]


def cache_path(cache_dir: str | Path, url: str) -> Path:
    ext = ".jpg"
    low = url.lower().split("?")[0]
    for e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        if low.endswith(e):
            ext = ".png" if e == ".png" else e
            break
    return Path(cache_dir) / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ext)


def fetch_image(url: str, cache_dir: str | Path, timeout: float = 20.0) -> Path | None:
    """下载并缓存图片，返回本地路径。已缓存直接返回。失败返回 None。"""
    if not _safe_url(url):
        return None
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache_path(cache, url)
    if target.is_file() and target.stat().st_size > 0:
        return target
    # Referer 要跟着来源变：B 站 CDN 有防盗链，带 cn.bing.com 的 Referer 会被拒
    # （实测 B 站官方表情图就是这样全部下载失败的，表现是"[表情:smug]"被整段删掉）。
    host = _domain(url)
    headers = dict(HEADERS)
    if host.endswith("hdslb.com") or host.endswith("bilibili.com"):
        headers["Referer"] = "https://www.bilibili.com/"
    try:
        with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as c:
            with c.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                ctype = (resp.headers.get("content-type") or "").lower()
                if ctype and not ctype.startswith("image/"):
                    return None
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MAX_BYTES:
                        return None
        if not buf or not any(bytes(buf).startswith(m) for m in _ALLOWED_MAGIC):
            return None
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(bytes(buf))
        tmp.replace(target)
        return target
    except (httpx.HTTPError, OSError):
        return None


# ---------------- 用户自己导入的表情包 ----------------
#
# 早期版本内置了 12 个程序生成的表情包，用户明确要求删掉，改成完全由用户导入。
# 这样她发的表情包是你自己挑的，而不是程序画给你的。

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def user_dir(cfg: dict[str, Any]) -> Path:
    """用户表情包目录（默认 data/stickers）。"""
    raw = (cfg.get("stickers") or {}).get("user_dir") or "data/stickers"
    d = Path(raw)
    if not d.is_absolute():
        d = Path(cfg.get("_root") or ".") / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_stem(name: str) -> str:
    """文件名 -> 可当标记 key 的名字。保留中文，去掉路径分隔符等危险字符。"""
    stem = Path(str(name or "")).stem or "sticker"
    stem = _UNSAFE_RE.sub("", stem).strip().strip(".")
    return stem[:40] or "sticker"


def _entry(f: Path) -> dict[str, Any]:
    from urllib.parse import quote

    return {
        "key": f.stem,
        "label": f.stem,
        "name": f.name,
        "url": f"/api/stickers/local/{quote(f.name)}",
    }


def list_user(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """列出用户导入的表情包，按文件名排序。"""
    try:
        d = user_dir(cfg)
    except OSError:
        return []
    out = []
    for f in sorted(d.iterdir(), key=lambda p: p.name):
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTS:
            out.append(_entry(f))
    return out


def save_user(cfg: dict[str, Any], raw: bytes, ext: str, name: str) -> dict[str, Any]:
    """保存一张导入的表情包（重名自动加 _2 / _3）。"""
    d = user_dir(cfg)
    stem = safe_stem(name)
    target = d / f"{stem}{ext}"
    i = 2
    while target.exists():
        target = d / f"{stem}_{i}{ext}"
        i += 1
    target.write_bytes(raw)
    return _entry(target)


def find_user_path(cfg: dict[str, Any], name: str) -> Path | None:
    """按文件名取路径，并确保没有跳出目录（防目录穿越）。"""
    try:
        d = user_dir(cfg).resolve()
    except OSError:
        return None
    target = (d / Path(name).name).resolve()
    if d not in target.parents or not target.is_file():
        return None
    return target


def delete_user(cfg: dict[str, Any], name: str) -> bool:
    p = find_user_path(cfg, name)
    if not p:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":  # 自检：python -m neko.stickers 猫咪
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    kw = sys.argv[1] if len(sys.argv) > 1 else "猫咪表情包"
    hits = search(kw, limit=5)
    print(f"搜索「{kw}」-> {len(hits)} 条")
    for h in hits:
        print("   ", h["url"][:100])
    if hits:
        p = fetch_image(hits[0]["url"], Path("data/stickers_cache"))
        print("下载第一条 ->", p)
