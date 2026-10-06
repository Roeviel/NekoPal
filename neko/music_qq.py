"""QQ音乐：搜索 + 扫码登录 + 听歌记录。

和网易云那套完全不同 —— QQ音乐走的是**QQ互联 ptlogin** 流程：

1. ``ptqrshow`` 拿二维码图 + ``qrsig`` cookie
2. ``ptqrtoken = hash33(qrsig)`` 后轮询 ``ptqrlogin``
3. 成功时返回 ``ptuiCB('0','0','<跳转地址>','0','登录成功','<昵称>')``
4. 跟着跳转走完，把 cookie 收下来（``uin`` / ``qm_keyst`` …）

**和网易云一样，这是逆向接口**，所以每个入口失败都只返回原因、绝不抛异常。
`search` 是**免登录**的（实测可用），所以"一起听"其实不登录也能用，
登录只影响"最近在听"。
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
from typing import Any

import httpx

from . import music as _m

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_APPID = "716027609"          # QQ音乐网页版用的互联 appid
_DAID = "383"
#: **QQ音乐**的互联应用 ID。这里填 0 是不行的 —— QQ 那边不知道是哪个应用在请求，
#: 手机扫码就会报"版本不匹配"之类的错。
_PT_3RD_AID = "100497308"
_S_URL = "https://graph.qq.com/oauth2.0/login_jump"
_QR_SHOW = "https://ssl.ptlogin2.qq.com/ptqrshow"
_QR_LOGIN = "https://ssl.ptlogin2.qq.com/ptqrlogin"
_XLOGIN = "https://xui.ptlogin2.qq.com/cgi-bin/xlogin"
_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
_MUSICU = "https://u.y.qq.com/cgi-bin/musicu.fcg"

#: QQ 歌词头部的制作人员名单（"词：阿信" 这种），不是歌词，要滤掉
_CREDIT = re.compile(
    r"^(作词|作曲|编曲|词|曲|制作人|监制|吉他|贝斯|鼓|键盘|弦乐|和声|录音|混音|母带|"
    r"OP|SP|发行|出品|统筹|企划|宣发|录音室|录音棚|美术|设计|MV|导演|词曲)\s*[：:]"
)


def _client(cfg: dict[str, Any] | None = None) -> httpx.Client:
    headers = {"User-Agent": _UA, "Referer": "https://y.qq.com/"}
    ck = _m.cookie_qq(cfg) if cfg else ""
    if ck:
        headers["Cookie"] = ck
    return httpx.Client(timeout=15.0, headers=headers, follow_redirects=True)


def hash33(text: str) -> int:
    """QQ 那个 hash33（ptqrtoken 就是这么算的）。"""
    e = 0
    for ch in text:
        e += (e << 5) + ord(ch)
    return 2147483647 & e


# ---------- 搜索（**免登录**） ----------

def search(cfg: dict[str, Any], keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": _UA,
                                                 "Referer": "https://y.qq.com/"}) as c:
            r = c.get(_SEARCH, params={"w": keyword, "format": "json",
                                       "n": limit, "p": 1})
            j = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[music-qq] 搜索失败：{type(exc).__name__}")
        return []
    songs = (((j.get("data") or {}).get("song") or {}).get("list") or [])[:limit]
    out = []
    for s in songs:
        album = s.get("albummid") or ""
        out.append({
            "id": s.get("songmid") or "",
            "title": s.get("songname") or "",
            "artist": "、".join(x.get("name", "") for x in (s.get("singer") or [])),
            "album": s.get("albumname") or "",
            # QQ 的封面要按 albummid 拼
            "cover": (f"https://y.qq.com/music/photo_new/T002R300x300M000{album}.jpg"
                      if album else ""),
            "duration": int(s.get("interval") or 0),
            "provider": "qq",
        })
    return out


def detail(cfg: dict[str, Any], song_id: str) -> dict[str, Any]:
    """QQ 的搜索结果已经带封面，这里只是补一个统一入口。"""
    hits = search(cfg, song_id, limit=1)
    return hits[0] if hits else {}


#: LRC 的元数据行：[ti:] [ar:] [al:] [by:] [offset:] —— 标签是**字母**，
#: 而真正的歌词行是 [mm:ss.xx]，标签是**数字**。按这个区分最稳。
_LRC_META = re.compile(r"^\[[a-zA-Z]+:")


def lyric(cfg: dict[str, Any], song_id: str, max_chars: int = 900) -> str:
    """拿歌词。QQ 的歌词是 LRC，前面有元数据和制作人员名单，都要滤掉。"""
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": _UA,
                                                 "Referer": "https://y.qq.com/"}) as c:
            r = c.get("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                      params={"songmid": song_id, "format": "json", "nobase64": 1,
                              "g_tk": 5381})
            j = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[music-qq] 歌词失败：{type(exc).__name__}")
        return ""
    return _clean_lrc(j.get("lyric") or "", max_chars)


def _clean_lrc(raw: str, max_chars: int = 900) -> str:
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or _LRC_META.match(s):
            continue
        text = s.split("]", 1)[-1].strip() if s.startswith("[") else s
        if not text or _CREDIT.match(text):
            continue
        # 第一行内容如果是「歌名 - 歌手 (英文名)」，那也不是歌词
        if not lines and " - " in text and len(text) < 70:
            continue
        lines.append(text)
    return "\n".join(lines)[:max_chars]


# ---------- 扫码登录 ----------

def _ck_get(client: httpx.Client, name: str, default: str = "") -> str:
    """按名字取 cookie，**不抛异常**。

    httpx 的 ``client.cookies.get(name)`` 在"多个域下有同名 cookie"时会抛
    ``CookieConflict``（``get`` 只吞 ``KeyError``）。QQ 这边 qrsig / pt_login_sig
    都可能被多个域设一遍，所以直接翻 jar 更稳。
    """
    try:
        for ck in client.cookies.jar:
            if ck.name == name and ck.value:
                return ck.value
    except Exception as exc:  # noqa: BLE001
        print(f"[music-qq] 读 cookie {name} 失败：{type(exc).__name__}")
    return default


# ---------- 手动导入 Cookie（扫码走不通时的兜底） ----------

#: QQ音乐要用的 cookie 字段
_COOKIE_KEYS = ("qm_keyst", "qqmusic_key", "MUSIC_U", "uin", "p_uin", "skey",
                "p_skey", "euin", "psrf_qqunionid", "psrf_access_token_expiresat",
                "qqmusic_uin", "wxuin", "wxopenid", "wxrefresh_token")


def import_cookie(cfg: dict[str, Any], text: str) -> dict[str, Any]:
    """从用户粘进来的文本里抠出 QQ音乐 cookie。

    为什么要这个：``ptqrshow`` 给的是 **QQ互联的登录码**
    （内容长这样 ``http://txz.qq.com/p?k=...&f=716027609``），
    **只有手机QQ App 的内置扫码器**会把它认成"确认登录"；
    用微信、QQ音乐、系统相机去扫，都会当成普通网址打开，
    跳到的就是 QQ 下载页。用户手机上没有 QQ 时这条路直接走不通。
    cookie 这条路不依赖任何扫码行为。
    """
    if not text or not text.strip():
        return {"ok": False, "reason": "内容是空的"}
    raw = text.strip()
    for ch in ('"', "'", "`"):
        raw = raw.replace(ch, " ")
    pairs: dict[str, str] = {}
    for chunk in re.split(r"[;\n\r]+", raw):
        chunk = chunk.strip().strip(",").strip()
        if not chunk:
            continue
        if "=" in chunk:
            k, v = chunk.split("=", 1)
        elif re.match(r"^(qm_keyst|qqmusic_key|uin|p_uin|skey|p_skey)\s+\S+", chunk):
            k, v = re.split(r"\s+", chunk, 1)
        else:
            continue
        k, v = k.strip(), v.strip().strip('"')
        if k in _COOKIE_KEYS and v:
            pairs[k] = v
    # 关键字段：有 qm_keyst/qqmusic_key 才算真登录
    if not any(k in pairs for k in ("qm_keyst", "qqmusic_key")):
        return {"ok": False,
                "reason": "没找到 qm_keyst。要在 y.qq.com 登录后，"
                          "从 cookie 里找 qm_keyst（或 qqmusic_key）那一条。"}
    if "uin" not in pairs:
        m = re.search(r"(?:^|;\s*)uin=o?([0-9]+)", text)
        if m:
            pairs["uin"] = m.group(1)
    ck = "; ".join(f"{k}={v}" for k, v in pairs.items())
    _m._kv_set(cfg, "qq_cookie", ck)
    _m._kv_set(cfg, "qq_login_ts", time.time())
    # 验一下能不能读到东西
    songs = recent(cfg, limit=1)
    if not songs:
        print("[music-qq] 导入的 cookie 读不到听歌记录（可能没有记录，也可能无效）")
    print(f"[music] 手动导入 QQ cookie：{sorted(pairs)}")
    return {"ok": True, "nickname": f"QQ {pairs.get('uin', '')}".strip(),
            "fields": sorted(pairs), "records": len(songs)}


def qr_start(cfg: dict[str, Any]) -> dict[str, Any]:
    """拿二维码。

    **两步，顺序不能反**：

    1. 先 GET ``xui.ptlogin2.qq.com/cgi-bin/xlogin``，从 cookie 里拿 ``pt_login_sig``
       —— 轮询时要把它当 ``login_sig`` 传回去，不传 QQ 会认为会话不合法
    2. 再 GET ``ptqrshow`` 拿二维码图 + ``qrsig``

    参数也有讲究：``pt_3rd_aid`` 必须是 QQ音乐自己的 ``100497308``，
    ``t`` 是 0~1 的随机数（不是时间戳）。
    """
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": _UA}) as c:
            # ① 拿 pt_login_sig
            c.get(_XLOGIN, params={
                "appid": _APPID, "daid": _DAID, "style": "33",
                "login_text": "授权并登录", "hide_title_bar": "1", "hide_border": "1",
                "target": "self", "s_url": _S_URL, "pt_3rd_aid": _PT_3RD_AID,
                "pt_feedback_link":
                    "https://support.qq.com/products/77942?customInfo=.appid100497308",
            })
            login_sig = _ck_get(c, "pt_login_sig")
            # ② 拿二维码
            r = c.get(_QR_SHOW, params={
                "appid": _APPID, "e": "2", "l": "M", "s": "3", "d": "72", "v": "4",
                "t": str(random.random()), "daid": _DAID, "pt_3rd_aid": _PT_3RD_AID})
            sig = _ck_get(r, "qrsig") or _ck_get(c, "qrsig")
            img = r.content
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"连不上 QQ互联：{type(exc).__name__}"}
    if not sig or not img:
        return {"ok": False, "reason": "没拿到二维码"}
    mime = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
    _m._kv_set(cfg, "qq_qrsig", sig)
    _m._kv_set(cfg, "qq_login_sig", login_sig)
    print(f"[music-qq] 已生成 QQ 登录二维码（login_sig {'有' if login_sig else '缺'}）")
    return {"ok": True, "provider": "qq", "key": sig, "ttl": 240,
            "image": f"data:{mime};base64," + base64.b64encode(img).decode()}


# 注意：**结尾没有分号**。实测返回是 `ptuiCB('66','0','','0','二维码未失效。', '')`
# 加个 ``;`` 会让正则匹配不到、状态全变成"未知"。
_PTUI = re.compile(r"ptuiCB\((.+?)\)", re.S)


def qr_poll(cfg: dict[str, Any], sig: str) -> dict[str, Any]:
    """轮询扫码状态。65 过期 / 66 未扫 / 67 已扫待确认 / 0 成功。

    ``login_sig`` 必须带上（``qr_start`` 时拿到的那个），否则 QQ 会认为
    这次轮询不是同一次登录会话。
    """
    token = hash33(sig)
    login_sig = str(_m._kv_get(cfg, "qq_login_sig") or "")
    params = {
        "u1": _S_URL,
        "ptqrtoken": str(token), "ptredirect": "0", "h": "1", "t": "1", "g": "1",
        "from_ui": "1", "ptlang": "2052", "action": f"0-0-{int(time.time()*1000)}",
        "js_ver": "20102616", "js_type": "1", "login_sig": login_sig,
        "pt_uistyle": "40", "aid": _APPID, "daid": _DAID,
        "pt_3rd_aid": _PT_3RD_AID, "has_onekey": "1",
    }
    try:
        with httpx.Client(timeout=15.0, headers={
            "User-Agent": _UA, "Referer": "https://xui.ptlogin2.qq.com/",
        }) as c:
            c.cookies.set("qrsig", sig, domain=".ptlogin2.qq.com")
            r = c.get(_QR_LOGIN, params=params)
            text = r.text
            m = _PTUI.search(text)
            if not m:
                _m._trace("qq", -1, "状态未知", text[:200])
                return {"ok": True, "code": -1, "done": False, "message": "状态未知"}
            # 参数是单引号包起来的，中间可能带空格，逐个清洗
            parts = [p.strip().strip("'").strip() for p in m.group(1).split(",")]
            code = parts[0]
            if code == "0":
                # parts[2] 是跳转地址，跟着走把 cookie 收全
                ck = _follow_login(c, parts[2], text)
                if ck:
                    _m._kv_set(cfg, "qq_cookie", ck)
                    _m._kv_set(cfg, "qq_login_ts", time.time())
                    _m._kv_set(cfg, "qq_nickname", parts[5] if len(parts) > 5 else "")
                _m._trace("qq", 0, "登录成功" if ck else "登录了但没拿到 cookie",
                          f"cookie 字段：{sorted(k for k in ck.split('; ') if k)}" if ck else text[:200])
                return {"ok": True, "code": 0, "done": bool(ck),
                        "nickname": parts[5] if len(parts) > 5 else "",
                        "message": "登录成功" if ck else "登录了但没拿到 cookie"}
            msg = {"65": "二维码已过期，请刷新", "66": "等待扫码",
                   "67": "已扫码，请在手机上确认"}.get(code, f"状态 {code}")
            _m._trace("qq", code, msg,
                      "" if code in ("65", "66", "67") else text[:300])
            return {"ok": True, "code": int(code) if code.isdigit() else -1,
                    "done": False, "message": msg}
    except Exception as exc:  # noqa: BLE001
        _m._trace("qq", -1, f"查询失败：{type(exc).__name__}", str(exc)[:200])
        return {"ok": False, "code": -1, "done": False,
                "message": f"查询失败：{type(exc).__name__}"}


def _follow_login(client: httpx.Client, jump_url: str, raw: str) -> str:
    """登录成功后收 cookie。

    返回文本长这样：
    ``ptuiCB('0','0','https://ptlogin2.y.qq.com/check_sig?...','0','登录成功','昵称')``
    里面带了 ``&uin=<QQ号>&service=...``；官方流程是**再 GET 一次那个 check_sig 地址**
    （不要跟随重定向），cookie 才落得下来。
    """
    try:
        # 先把 check_sig 那个地址请求一次（不跟随跳转）
        if jump_url and jump_url.startswith("http"):
            client.get(jump_url, follow_redirects=False)
        # 再走一遍 QQ音乐首页，让 y.qq.com 域下的 cookie 落下来
        client.get("https://y.qq.com/")
        jar = {c.name: c.value for c in client.cookies.jar}
        keep = ("uin", "qm_keyst", "qqmusic_key", "skey", "p_skey", "p_uin",
                "pt2gguin", "euin", "psrf_qqunionid")
        picked = {k: v for k, v in jar.items() if k in keep}
        # uin 也可能只在返回文本里（&uin=xxxx&service=...）
        m = re.search(r"[?&]uin=([0-9a-zA-Z]+)", raw or "")
        if m:
            picked.setdefault("uin", m.group(1))
        return "; ".join(f"{k}={v}" for k, v in picked.items())
    except Exception as exc:  # noqa: BLE001
        print(f"[music-qq] 收 cookie 失败：{type(exc).__name__}")
        return ""


# ---------- 听歌记录（要登录） ----------

def recent(cfg: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    ck = _m.cookie_qq(cfg)
    if not ck:
        return []
    uin = ""
    m = re.search(r"(?:^|;\s*)uin=([^;]+)", ck)
    if m:
        uin = m.group(1).lstrip("o").lstrip("0") or m.group(1)
    body = {
        "comm": {"ct": 24, "cv": 0, "uin": uin},
        "req_0": {"module": "music.recent", "method": "Get",
                  "param": {"uin": uin, "num": max(1, min(limit, 50)), "page": 0}},
    }
    try:
        with _client(cfg) as c:
            r = c.post(_MUSICU, json=body)
            j = r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[music-qq] 记录读取失败：{type(exc).__name__}")
        return []
    node = j.get("req_0") or {}
    if node.get("code") not in (0, None):
        print(f"[music-qq] 记录被拒：code={node.get('code')}")
        return []
    data = node.get("data") or {}
    items = data.get("list") or data.get("vecSong") or []
    out = []
    for it in items[:limit]:
        song = it.get("song") or it
        album = (song.get("album") or {}).get("mid") or song.get("albummid") or ""
        out.append({
            "id": song.get("mid") or song.get("songmid") or "",
            "title": song.get("name") or song.get("title") or "",
            "artist": "、".join(x.get("name", "") for x in (song.get("singer") or [])),
            "album": (song.get("album") or {}).get("name") or "",
            "cover": (f"https://y.qq.com/music/photo_new/T002R300x300M000{album}.jpg"
                      if album else ""),
            "provider": "qq",
        })
    return out


def state(cfg: dict[str, Any]) -> dict[str, Any]:
    ck = _m.cookie_qq(cfg)
    if not ck:
        return {"logged_in": False}
    uin = ""
    m = re.search(r"(?:^|;\s*)uin=([^;]+)", ck)
    if m:
        uin = m.group(1).lstrip("o")
    return {"logged_in": True, "provider": "qq",
            "nickname": _m._kv_get(cfg, "qq_nickname") or (f"QQ {uin}" if uin else "QQ音乐用户"),
            "since": _m._kv_get(cfg, "qq_login_ts")}


def logout(cfg: dict[str, Any]) -> dict[str, Any]:
    _m._kv_set(cfg, "qq_cookie", "")
    _m._kv_set(cfg, "qq_login_ts", None)
    return {"ok": True, "logged_in": False}
