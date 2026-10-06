"""B 站扫码登录：拿 SESSDATA，从而解锁字幕。

为什么值得做：未登录时 ``x/player/v2`` 的字幕列表恒为空（实测抽样 145 个视频，
字幕 0 条），她只能靠弹幕+评论反推内容，笔记等级停在「观众视角」。
登录后能拿到 UP 主 CC 字幕，等级升到「有字幕」——这是学习质量最大的提升点。

**为什么用扫码而不是让用户去浏览器 F12 抠 Cookie**：
扫码时密码不经过这个程序，用户也不用碰开发者工具。
二维码内容只是 B 站自己的一次性登录链接。

**风险（已在界面上向用户说明）**：
- SESSDATA 等同于账号凭据，会明文存在本地 config.json 里；
- 自动化访问有触发风控的概率，所以只做只读操作，且请求频率压到最低。

流程（B 站官方 web 扫码登录）：
1. ``qrcode/generate`` 拿二维码链接 + qrcode_key
2. 轮询 ``qrcode/poll``，data.code：
   86101 未扫码 / 86090 已扫码待确认 / 0 成功 / 86038 二维码已失效
3. 成功后 Cookie 在响应的 Set-Cookie 里
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import qrcode
import qrcode.image.svg

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

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"

#: 轮询返回的 data.code -> 人话
POLL_STATUS = {
    86101: ("waiting", "等待扫码"),
    86090: ("scanned", "已扫码，请在手机上确认"),
    0: ("ok", "登录成功"),
    86038: ("expired", "二维码已失效，请重新获取"),
}

#: 需要留下的 Cookie 字段（其余如 _uuid 之类对后续请求没用）
KEEP_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3")


class BiliAuthError(RuntimeError):
    pass


def qr_generate(timeout: float = 15.0) -> dict[str, Any]:
    """申请一个登录二维码，返回 {url, qrcode_key}。"""
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(QR_GENERATE)
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BiliAuthError(f"获取二维码失败：{exc}") from exc
    if payload.get("code") != 0:
        raise BiliAuthError(f"获取二维码被拒：{payload.get('message') or payload.get('code')}")
    data = payload.get("data") or {}
    url = str(data.get("url") or "")
    key = str(data.get("qrcode_key") or "")
    if not url or not key:
        raise BiliAuthError("二维码数据不完整")
    return {"url": url, "qrcode_key": key}


def qr_svg(url: str) -> str:
    """把登录链接渲染成内联 SVG（不依赖 PIL）。"""
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # 去掉固定宽高，交给前端 CSS 缩放
    for attr in ('width="', 'height="'):
        i = svg.find(attr)
        if i != -1:
            j = svg.find('"', i + len(attr))
            svg = svg[:i] + svg[j + 1:]
    if "viewBox" not in svg:
        svg = svg.replace("<svg ", '<svg viewBox="0 0 330 330" ', 1)
    return svg


def qr_poll(qrcode_key: str, timeout: float = 15.0) -> dict[str, Any]:
    """轮询扫码结果。成功时返回 cookie 字符串。"""
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True) as c:
            resp = c.get(QR_POLL, params={"qrcode_key": qrcode_key})
            payload = resp.json()
            jar = dict(resp.cookies)
    except (httpx.HTTPError, ValueError) as exc:
        raise BiliAuthError(f"轮询失败：{exc}") from exc

    data = payload.get("data") or {}
    code = data.get("code")
    state, message = POLL_STATUS.get(code, ("unknown", str(data.get("message") or code)))

    out: dict[str, Any] = {"state": state, "message": message, "code": code}
    if state == "ok":
        cookie = _cookie_string(jar)
        if not cookie:
            out.update(state="error", message="登录成功但没拿到 Cookie，请重试")
            return out
        out["cookie"] = cookie
    return out


def _cookie_string(jar: dict[str, str]) -> str:
    parts = [f"{k}={jar[k]}" for k in KEEP_COOKIES if jar.get(k)]
    if not any(p.startswith("SESSDATA=") for p in parts):
        return ""
    return "; ".join(parts)


def nav(cookie: str, timeout: float = 15.0) -> dict[str, Any]:
    """查登录状态。cookie 为空时直接返回未登录，不发请求。"""
    if not cookie.strip():
        return {"logged_in": False, "uname": "", "mid": 0, "vip": False}
    headers = {**HEADERS, "Cookie": cookie}
    try:
        with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as c:
            payload = c.get(NAV).json()
    except (httpx.HTTPError, ValueError):
        # 网络问题不该被当成"登录失效"，如实标成未知
        return {"logged_in": False, "uname": "", "mid": 0, "unknown": True}
    data = payload.get("data") or {}
    logged = payload.get("code") == 0 and bool(data.get("isLogin"))
    return {
        "logged_in": logged,
        "uname": str(data.get("uname") or ""),
        "mid": int(data.get("mid") or 0),
        "vip": bool((data.get("vipStatus") or 0)),
        "level": int((data.get("level_info") or {}).get("current_level") or 0),
    }


if __name__ == "__main__":  # 自检：python -m neko.bili_auth
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    q = qr_generate()
    print("二维码链接:", q["url"][:80])
    print("qrcode_key:", q["qrcode_key"])
    svg = qr_svg(q["url"])
    print("SVG 长度:", len(svg), "| 含 viewBox:", "viewBox" in svg)
    print("轮询一次:", qr_poll(q["qrcode_key"]))
