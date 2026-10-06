"""网易云音乐：扫码登录 + 听歌记录 + 一起听（记录与分享）。

为什么用**扫码登录**而不是账号密码：
用户不用把密码交给这个程序，cookie 也只存在本地，随时可以在手机端退出登录。

为什么自己实现 weapi 加密而不装 NeteaseCloudMusicApi：
那是个 Node 服务，为了几个只读接口在 Python 应用里再拖一个 Node 进程不划算。
weapi 的算法（AES-128-CBC + RSA）是公开且多年稳定的，见 ``_weapi``。

诚实提醒：这是**逆向接口**，网易改签名或风控策略就可能失效。
所以每个入口失败都只返回错误信息、绝不抛到界面上，功能坏了不影响聊天。
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .config import resolve_path

# ---------- weapi 加密 ----------

_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629"
    "ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
_PUBKEY_E = "010001"
_NONCE = "0CoJUm6Qyw8W8jud"
_IV = b"0102030405060708"
#: **UA 不能随便写。** 网易云会看 UA 判断客户端；用普通 Chrome UA 时，
#: 扫码后服务端会回「请切换其他登录方式或升级新版本再试」。
#: 这个值是参考实现里 weapi→pc 用的那个。
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")
_BASE = "https://music.163.com"
_lock = threading.Lock()

#: 搜索的节流与缓存。**不是优化，是必需** ——
#: 实测连查几次就被网易云以 ``405 操作过于频繁`` 挡掉，那时挑歌会直接失败。
_search_lock = threading.Lock()
_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SEARCH_TTL = 600.0        # 同一关键词 10 分钟内复用
_SEARCH_MIN_GAP = 1.2      # 两次真实请求至少隔这么久
_last_search = 0.0

#: 客户端声明的运行环境。**没有这一整套，网易云会把请求当成未知设备**，
#: 表现就是扫了码却被拒（"请切换其他登录方式或升级新版本再试"）。
_OS_PC = {
    "os": "pc",
    "appver": "3.1.17.204416",
    "osver": "Microsoft-Windows-10-Professional-build-19045-64bit",
    "channel": "netease",
}


def _rand_hex(n: int) -> str:
    return os.urandom(n // 2).hex()


def _device(cfg: dict[str, Any]) -> dict[str, str]:
    """设备标识。**首次生成后固定下来**（真机也不会每次换设备号）。"""
    d = _kv_get(cfg, "music_device")
    if not isinstance(d, dict) or not d.get("_ntes_nuid"):
        ms = int(time.time() * 1000)
        nuid = _rand_hex(32)
        d = {
            "_ntes_nuid": nuid,
            "_ntes_nnid": f"{nuid},{ms}",
            "WNMCID": f"{_rand_hex(6)}.{ms}.01.0",
            "WEVNSM": "1.0.0",
            "deviceId": _rand_hex(32).upper(),
            **{k: v for k, v in _OS_PC.items() if k != "os"},
        }
        _kv_set(cfg, "music_device", d)
        print("[music] 已生成并固定设备标识")
    return d


def _aes(text: str, key: str) -> str:
    from Crypto.Cipher import AES

    raw = text.encode()
    pad = 16 - len(raw) % 16
    enc = AES.new(key.encode(), AES.MODE_CBC, _IV).encrypt(raw + bytes([pad]) * pad)
    return base64.b64encode(enc).decode()


def _weapi(payload: dict[str, Any]) -> dict[str, str]:
    """把明文参数加密成 weapi 需要的 params + encSecKey。"""
    secret = base64.b64encode(os.urandom(12)).decode()[:16]
    params = _aes(_aes(json.dumps(payload, ensure_ascii=False), _NONCE), secret)
    enc = pow(int.from_bytes(secret[::-1].encode(), "big"), int(_PUBKEY_E, 16),
              int(_MODULUS, 16))
    return {"params": params, "encSecKey": format(enc, "x").zfill(256)}


# ---------- 配置 / 连接 ----------

def conf(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return ((cfg or {}).get("music") or {})


def provider(cfg: dict[str, Any] | None) -> str:
    """用哪家的接口取"最近在听"。``netease``（默认）或 ``qq``。

    注意：**搜歌和一起听两家都免登录可用**，所以 provider 只影响"最近在听"
    和登录态。用户登了哪家就用哪家。
    """
    p = str(conf(cfg).get("provider") or "").lower()
    if p in ("qq", "qqmusic"):
        return "qq"
    if p in ("netease", "163", "wangyi"):
        return "netease"
    # 没显式配置就看谁登录了
    if cookie_qq(cfg) and not cookie(cfg):
        return "qq"
    return "netease"


def _qq():
    from . import music_qq

    return music_qq


def enabled(cfg: dict[str, Any] | None) -> bool:
    return bool(conf(cfg).get("enabled", True))


def _db(cfg: dict[str, Any]) -> Path:
    p = Path((cfg.get("memory") or {}).get("db_path") or "data/neko.db")
    return p if p.is_absolute() else resolve_path(p)


def _conn(cfg: dict[str, Any]):
    conn = sqlite3.connect(_db(cfg), timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


_DDL = """
CREATE TABLE IF NOT EXISTS music_sessions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    song_id   TEXT,
    title     TEXT,
    artist    TEXT,
    cover     TEXT,
    comment   TEXT,
    source    TEXT
);
CREATE INDEX IF NOT EXISTS idx_music_ts ON music_sessions(ts);
"""


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    # kv 表由 memory 建；这里只保证存在
    conn.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")


def _kv_get(cfg: dict[str, Any], key: str) -> Any:
    try:
        with _conn(cfg) as c:
            _setup(c)
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row and row["value"] else None
    except (sqlite3.Error, ValueError):
        return None


def _kv_set(cfg: dict[str, Any], key: str, value: Any) -> None:
    try:
        with _conn(cfg) as c, c:
            _setup(c)
            c.execute("INSERT INTO kv(key,value) VALUES(?,?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))
    except sqlite3.Error as exc:
        print(f"[music] 保存失败：{exc}")


# ---------- 登录 ----------

def cookie(cfg: dict[str, Any]) -> str:
    return str(_kv_get(cfg, "music_cookie") or "")


def cookie_qq(cfg: dict[str, Any] | None) -> str:
    """QQ音乐的 cookie（另一个键，两家可以同时登着，互不干扰）。"""
    return str(_kv_get(cfg, "qq_cookie") or "") if cfg else ""


def _client(cfg: dict[str, Any]) -> httpx.Client:
    """带齐设备 cookie + 登录 cookie 的客户端。

    ``is_login`` 为真时是登录相关请求（``/login`` 路径），
    参考实现里那种请求**不加 NMTID**、并且可以覆盖设备字段。
    """
    ck = cookie(cfg)
    jar = dict(_device(cfg))
    jar.update({"__remember_me": "true", "ntes_kaola_ad": "1", "os": _OS_PC["os"]})
    extra = {}
    for part in (ck or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            extra[k.strip()] = v.strip()
    jar.update(extra)
    headers = {
        "User-Agent": _UA,
        "Referer": "https://music.163.com/",
        "Origin": "https://music.163.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
    }
    return httpx.Client(timeout=15.0, headers=headers, follow_redirects=True)


def _device_client(cfg: dict[str, Any]) -> httpx.Client:
    """登录专用：不带 NMTID（参考实现里 ``uri.indexOf('login') !== -1`` 的分支）。"""
    return _client(cfg)


def _post(cfg: dict[str, Any], path: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _client(cfg) as c:
        r = c.post(f"{_BASE}{path}?csrf_token=", data=_weapi(payload))
        r.raise_for_status()
        return r.json()


#: 二维码类型。**必须是 3**。
#: 用 1 也能拿到 unikey、轮询也返回 801，看不出问题 —— 但手机 App 一扫就报
#: **"版本不匹配"**：type=1 是旧客户端的二维码格式。
#: 参考实现（NeteaseCloudMusicApi 的 module/login_qr_key.js）用的就是 type: 3。
_QR_TYPE = 3


def qr_start(cfg: dict[str, Any]) -> dict[str, Any]:
    """取一个登录二维码，返回 unikey + 可直接显示的 SVG。"""
    if not enabled(cfg):
        return {"ok": False, "reason": "音乐功能已在配置里关闭"}
    try:
        j = _post(cfg, "/weapi/login/qrcode/unikey", {"type": _QR_TYPE})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"连不上网易云：{type(exc).__name__}"}
    if j.get("code") != 200 or not j.get("unikey"):
        return {"ok": False, "reason": f"拿二维码失败（code={j.get('code')}）"}
    key = j["unikey"]
    url = f"https://music.163.com/login?codekey={key}"
    # 二维码用 **SVG** 而不是 PNG：PNG 要 PIL，而这个环境里没装
    # （为了一个二维码去拖 Pillow 不值得）。SVG 是纯文本，前端直接塞进 DOM 就行。
    svg = ""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage

        q = qrcode.QRCode(border=2, box_size=6)
        q.add_data(url)
        q.make(fit=True)
        raw = q.make_image(image_factory=SvgPathImage).to_string()
        svg = raw.decode() if isinstance(raw, bytes) else raw
    except Exception as exc:  # noqa: BLE001
        print(f"[music] 生成二维码失败：{type(exc).__name__}: {exc}")
    print("[music] 已生成登录二维码，用手机网易云 App 扫码")
    return {"ok": True, "key": key, "url": url, "svg": svg,
            "ttl": int(conf(cfg).get("qr_ttl_sec", 240))}


#: 扫码过程的状态轨迹（给排查用）。只记**状态变化**，不打每一轮，免得刷屏。
_QR_TRACE: dict[str, list[dict[str, Any]]] = {}
_QR_LAST: dict[str, Any] = {}


def qr_trace(provider: str = "netease") -> list[dict[str, Any]]:
    return list(_QR_TRACE.get(provider, []))


def _trace(provider: str, code: Any, message: str, extra: str = "") -> None:
    if _QR_LAST.get(provider) == (code, message):
        return                      # 没变化就不记
    _QR_LAST[provider] = (code, message)
    row = {"ts": time.strftime("%H:%M:%S"), "code": code, "message": message}
    if extra:
        row["detail"] = extra
    _QR_TRACE.setdefault(provider, []).append(row)
    del _QR_TRACE[provider][:-40]   # 只留最近 40 条
    print(f"[music] 扫码状态 {provider}: {code} {message}" + (f" | {extra}" if extra else ""))


def _jar_cookies(client: httpx.Client) -> dict[str, str]:
    """把响应里设的 cookie 收成字典。

    **不能用 ``client.cookies.items()``。** 它遇到"多个域下有同名 cookie"会抛
    ``httpx.CookieConflict``（实测扫码那一刻服务器会同时下发
    ``MUSIC_R_T``/``MUSIC_R_U``，就有多个同名），异常一抛整个轮询就失败，
    二维码还被消耗掉，下一次直接变成"已过期" —— 表现就是
    "扫了码但登录失败"，而真正的错误信息被埋在一句 CookieConflict 里。
    直接遍历 jar 并按名字去重才是稳的。
    """
    out: dict[str, str] = {}
    try:
        for ck in client.cookies.jar:
            out.setdefault(ck.name, ck.value)
    except Exception as exc:  # noqa: BLE001
        print(f"[music] 读取 cookie 失败：{type(exc).__name__}: {exc}")
    return out


def qr_poll(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    """查一次扫码状态。800 过期 / 801 待扫 / 802 待确认 / 803 成功。"""
    try:
        with _client(cfg) as c:
            r = c.post(f"{_BASE}/weapi/login/qrcode/client/login?csrf_token=",
                       data=_weapi({"key": key, "type": _QR_TYPE}))
            try:
                j = r.json()
            except ValueError:
                _trace("netease", -1, "返回不是 JSON", r.text[:200])
                return {"ok": False, "code": -1, "done": False, "message": "服务端返回异常"}
            code = j.get("code")
            if code == 803:
                got = _jar_cookies(c)
                ck = "; ".join(f"{k}={v}" for k, v in got.items())
                _kv_set(cfg, "music_cookie", ck)
                _kv_set(cfg, "music_login_ts", time.time())
                _trace("netease", code, "登录成功", f"拿到 cookie 字段：{sorted(got)}")
                who = me(cfg)
                return {"ok": True, "code": 803, "done": True,
                        "nickname": who.get("nickname") or "",
                        "cookies": sorted(got)}
            msg = {800: "二维码已过期，请刷新", 801: "等待扫码",
                   802: "已扫码，请在手机上确认",
                   -460: "被风控拦下了，稍等一会儿再试"}.get(code, j.get("message") or "")
            _trace("netease", code, msg,
                   "" if code in (800, 801, 802) else json.dumps(j, ensure_ascii=False)[:300])
            return {"ok": True, "code": code, "done": False, "message": msg, "raw": j}
    except httpx.CookieConflict as exc:
        # 单独兜住：这个异常说明"服务器已经认了这次扫码"（下发了 MUSIC_R_*），
        # 却倒在收 cookie 这一步。绝不能让它变成一句"查询失败"。
        _trace("netease", -1, f"收 cookie 时冲突：{exc}", "扫码其实已经生效")
        return {"ok": False, "code": -1, "done": False,
                "message": "扫码成功但收 cookie 出错，请再扫一次"}
    except Exception as exc:  # noqa: BLE001
        _trace("netease", -1, f"查询失败：{type(exc).__name__}", str(exc)[:200])
        return {"ok": False, "code": 0, "done": False,
                "message": f"查询失败：{type(exc).__name__}"}


def me(cfg: dict[str, Any]) -> dict[str, Any]:
    """当前登录账号。**没登录必须返回空字典。**

    踩过的坑：cookie 无效时网易云返回 ``code:301`` 且 ``profile`` 是空的，
    但以前这里仍然返回 ``{"user_id": None, "nickname": None}`` —— 非空字典
    恒为真，于是**假 cookie 也被判成"登录成功"**，整个登录状态不可信。
    """
    if not cookie(cfg):
        return {}
    try:
        j = _post(cfg, "/weapi/w/nuser/account/get", {})
        prof = j.get("profile") or {}
        uid = prof.get("userId")
        if not uid:
            # code 301 = 需要登录；任何情况下没有 userId 就是没登录
            print(f"[music] 账号查询未登录：code={j.get('code')}")
            return {}
        return {"user_id": uid, "nickname": prof.get("nickname"),
                "avatar": prof.get("avatarUrl")}
    except Exception:  # noqa: BLE001
        return {}


def state(cfg: dict[str, Any]) -> dict[str, Any]:
    """两家的登录状态都返回，界面可以两个按钮并排显示。"""
    if not enabled(cfg):
        return {"enabled": False, "logged_in": False, "provider": "netease"}
    ne: dict[str, Any] = {"logged_in": False}
    if cookie(cfg):
        who = me(cfg)
        if who:
            ne = {"logged_in": True, **who, "since": _kv_get(cfg, "music_login_ts")}
        else:
            _kv_set(cfg, "music_cookie", "")       # cookie 失效
            ne = {"logged_in": False, "reason": "网易云登录已失效，请重新扫码"}
    try:
        qq = _qq().state(cfg)
    except Exception as exc:  # noqa: BLE001
        qq = {"logged_in": False, "reason": f"{type(exc).__name__}"}
    cur = provider(cfg)
    active = ne if cur == "netease" else qq
    return {"enabled": True, "provider": cur,
            "logged_in": bool(active.get("logged_in")),
            "nickname": active.get("nickname") or "",
            "since": active.get("since"),
            "reason": active.get("reason") or "",
            "netease": {"logged_in": bool(ne.get("logged_in")),
                        "nickname": ne.get("nickname") or ""},
            "qq": {"logged_in": bool(qq.get("logged_in")),
                   "nickname": qq.get("nickname") or ""}}


# ---------- 手动导入 Cookie（扫码走不通时的兜底） ----------

#: 需要保留的网易云 cookie 字段（其它字段是广告/统计用的，留不留都行）
_COOKIE_KEYS = ("MUSIC_U", "MUSIC_A", "__csrf", "__remember_me", "NMTID",
                "_ntes_nuid", "_ntes_nnid", "WNMCID", "WEVNSM", "osver",
                "deviceId", "os", "appver", "channel")


def import_cookie(cfg: dict[str, Any], text: str) -> dict[str, Any]:
    """从用户粘进来的文本里抠出 cookie。

    为什么要这个：扫码是**逆向接口**，网易云随时可能让第三方生成的二维码
    在自家 App 里失效（手机上直接报"版本不匹配"，服务器根本收不到扫码事件）。
    这时最可靠的办法就是让用户从浏览器里把 cookie 抄过来 —— 这条路不依赖
    任何签名算法，只要能拿到 ``MUSIC_U`` 就能用。

    所以这里的解析做得**很宽容**：整段 Cookie 头、devtools 里复制的多行、
    甚至带 ``document.cookie`` 引号的，都能认。
    """
    if not text or not text.strip():
        return {"ok": False, "reason": "内容是空的"}
    raw = text.strip()
    # 常见粘贴形态：`MUSIC_U=xxx; Max-Age=...; Path=/` 或多行 `name  value`
    for ch in ('"', "'", "`"):
        raw = raw.replace(ch, " ")
    pairs: dict[str, str] = {}
    # 按 ; 和换行切，**但不要按 tab 切** —— devtools 里复制出来的是
    # `MUSIC_U<Tab>值`，按 tab 切会把名字和值拆成两段，谁都认不出来。
    for chunk in re.split(r"[;\n\r]+", raw):
        chunk = chunk.strip().strip(",").strip()
        if not chunk:
            continue
        if "=" in chunk:
            k, v = chunk.split("=", 1)
        elif re.match(r"^(MUSIC_U|MUSIC_A|__csrf|NMTID)\s+\S+", chunk):
            k, v = re.split(r"\s+", chunk, 1)          # devtools 表格里是空格/tab 分隔
        else:
            continue
        k, v = k.strip(), v.strip().strip('"')
        if k in _COOKIE_KEYS and v:
            pairs[k] = v
    if "MUSIC_U" not in pairs:
        return {"ok": False,
                "reason": "没找到 MUSIC_U。要的是网易云登录后的那条 cookie，"
                          "不是别的网站的。"}
    ck = "; ".join(f"{k}={v}" for k, v in pairs.items())
    _kv_set(cfg, "music_cookie", ck)
    _kv_set(cfg, "music_login_ts", time.time())
    who = me(cfg)
    if not who:
        _kv_set(cfg, "music_cookie", "")
        return {"ok": False, "reason": "这条 cookie 用不了（可能已经过期了），"
                                       "重新登一次网易云网页版再抄一次"}
    print(f"[music] 手动导入 cookie 成功：{sorted(pairs)}")
    return {"ok": True, "nickname": who.get("nickname") or "", "fields": sorted(pairs)}


def logout(cfg: dict[str, Any], prov: str = "") -> dict[str, Any]:
    p = (prov or provider(cfg)).lower()
    if p == "qq":
        return _qq().logout(cfg)
    _kv_set(cfg, "music_cookie", "")
    _kv_set(cfg, "music_login_ts", None)
    return {"ok": True, "logged_in": False, "provider": "netease"}


# ---------- 听歌记录 ----------

def recent(cfg: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """最近在听什么（用当前 provider，需要登录）。"""
    if provider(cfg) == "qq":
        return _qq().recent(cfg, limit=limit)
    return _recent_netease(cfg, limit=limit)


def _recent_netease(cfg: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """网易云的最近在听。返回 [{id,title,artist,cover,album}]。"""
    if not cookie(cfg):
        return []
    try:
        j = _post(cfg, "/weapi/v1/play/record", {"uid": (me(cfg) or {}).get("user_id"),
                                                 "type": 0, "limit": 100, "offset": 0})
    except Exception as exc:  # noqa: BLE001
        print(f"[music] 读取听歌记录失败：{type(exc).__name__}")
        return []
    out: list[dict[str, Any]] = []
    for item in (j.get("allData") or [])[:limit]:
        s = item.get("song") or {}
        out.append(_song(s, play=item.get("playCount"),
                         when=item.get("lastPlayed") or item.get("score")))
    return out


def _song(s: dict[str, Any], **extra: Any) -> dict[str, Any]:
    artists = "、".join(a.get("name", "") for a in (s.get("ar") or s.get("artists") or []))
    album = s.get("al") or s.get("album") or {}
    return {
        "id": str(s.get("id") or ""),
        "title": s.get("name") or "",
        "artist": artists,
        "album": album.get("name") or "",
        "cover": album.get("picUrl") or "",
        "duration": int((s.get("dt") or s.get("duration") or 0) // 1000),
        **extra,
    }


def search(cfg: dict[str, Any], keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    """搜歌（免登录）。按 provider 走，返回的每首歌都带 ``provider`` 字段。"""
    if provider(cfg) == "qq":
        return _qq().search(cfg, keyword, limit=limit)
    return _search_netease(cfg, keyword, limit=limit)


def _search_netease(cfg: dict[str, Any], keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    """网易云搜歌。**不需要登录**（但登录后结果更全）。

    用 ``/weapi/search/get``：实测 ``/weapi/cloudsearch/get/web`` 会返回
    ``50000005`` 拿不到结果，而这个是通的。

    **必须节流 + 缓存。** 实测连续查几次就返回 ``code=405``（操作过于频繁），
    那时挑歌会直接失败。所以同一关键词 10 分钟内直接复用缓存，
    并且两次真实请求之间至少隔 1.2 秒。
    """
    key = f"{keyword.strip().lower()}|{limit}"
    now = time.time()
    with _search_lock:
        hit = _search_cache.get(key)
        if hit and now - hit[0] < _SEARCH_TTL:
            return hit[1]

    # 节流：离上次请求太近就等一会儿（在锁外 sleep，别卡住别的线程）
    global _last_search
    with _search_lock:
        wait = _SEARCH_MIN_GAP - (now - _last_search)
        if wait > 0:
            _last_search = now + wait
        else:
            _last_search = now
    if wait > 0:
        time.sleep(min(wait, _SEARCH_MIN_GAP))

    for attempt in range(2):
        try:
            j = _post(cfg, "/weapi/search/get",
                      {"s": keyword, "type": 1, "limit": limit, "offset": 0})
        except Exception as exc:  # noqa: BLE001
            print(f"[music] 搜索失败：{type(exc).__name__}")
            return []
        if j.get("code") == 200:
            songs = ((j.get("result") or {}).get("songs") or [])[:limit]
            out = []
            for s in songs:
                d = _song(s)
                d["provider"] = "netease"
                out.append(d)
            with _search_lock:
                _search_cache[key] = (time.time(), out)
            return out
        code = j.get("code")
        if code == 405 and attempt == 0:
            print("[music] 搜索被限流（405），等 2 秒重试一次")
            time.sleep(2.0)
            continue
        print(f"[music] 搜索被拒：code={code}")
        return []
    return []


def detail(cfg: dict[str, Any], song_id: str, prov: str = "") -> dict[str, Any]:
    if (prov or provider(cfg)) == "qq":
        return _qq().detail(cfg, song_id)
    try:
        j = _post(cfg, "/weapi/v3/song/detail", {"c": json.dumps([{"id": str(song_id)}])})
        songs = j.get("songs") or []
        return _song(songs[0]) if songs else {}
    except Exception:  # noqa: BLE001
        return {}


def lyric(cfg: dict[str, Any], song_id: str, max_chars: int = 900,
          prov: str = "") -> str:
    """拿歌词（去掉时间轴）。给她用来"听懂这首歌在唱什么"。"""
    if (prov or provider(cfg)) == "qq":
        return _qq().lyric(cfg, song_id, max_chars=max_chars)
    return _lyric_netease(cfg, song_id, max_chars=max_chars)


def _lyric_netease(cfg: dict[str, Any], song_id: str, max_chars: int = 900) -> str:
    try:
        j = _post(cfg, "/weapi/song/lyric", {"id": str(song_id), "lv": -1, "kv": -1, "tv": -1})
    except Exception:  # noqa: BLE001
        return ""
    raw = ((j.get("lrc") or {}).get("lyric") or "")
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        # [ti:] [ar:] [al:] [by:] [offset:] 是元数据不是歌词（标签是字母）
        if not s or re.match(r"^\[[a-zA-Z]+:", s):
            continue
        text = s.split("]", 1)[-1].strip() if s.startswith("[") else s
        if text and not text.startswith(("作词", "作曲", "编曲")):
            lines.append(text)
    return "\n".join(lines)[:max_chars]


# ---------- 听歌倾向 ----------

#: 明显是翻唱/劣化版本的标题特征。网易云搜索里这些经常排到第一，
#: 直接取 hits[0] 就会推给用户一首"深情版/钢琴版/DJ版"的翻唱 ——
#: 用户反馈"她歌品太差"就是这么来的（搜"晴天 周杰伦"推了翻唱版）。
_BAD_TITLE = ("翻唱", "cover", "原唱", "dj", "remix", "beat", "trap", "伴奏",
              "铃声", "片段", "慢摇", "钢琴版", "吉他版", "古筝版", "纯音乐",
              "深情版", "女声版", "男声版", "抖音", "串烧", "清唱", "试听",
              "改编", "口琴", "八音盒", "karaoke", "instrumental")
#: 现场版/不同编曲：不算劣化，但同分时优先录音室版
_MILD_TITLE = ("live", "现场", "演唱会", "不插电", "acoustic")


def taste(cfg: dict[str, Any]) -> dict[str, Any]:
    t = dict(conf(cfg).get("taste") or {})
    return {"likes": list(t.get("likes") or []),
            "dislikes": list(t.get("dislikes") or []),
            "prefer_history": bool(t.get("prefer_history", True))}


def set_taste(cfg: dict[str, Any], **patch: Any) -> dict[str, Any]:
    from .config import load_config, save_config

    conf_now = load_config()
    m = conf_now.setdefault("music", {})
    t = dict(m.get("taste") or {})
    for k in ("likes", "dislikes", "prefer_history"):
        if k in patch and patch[k] is not None:
            t[k] = patch[k]
    t["likes"] = [str(x).strip() for x in (t.get("likes") or []) if str(x).strip()][:60]
    t["dislikes"] = [str(x).strip() for x in (t.get("dislikes") or []) if str(x).strip()][:60]
    m["taste"] = t
    save_config(conf_now)
    if cfg is not None:                      # 让当前进程立刻用上新值
        cfg.setdefault("music", {})["taste"] = t
    return t


def _hit(text: str, words: list[str]) -> bool:
    low = (text or "").lower()
    return any(w and w.lower() in low for w in words)


def score_song(song: dict[str, Any], query: str = "",
               tastev: dict[str, Any] | None = None) -> float:
    """给候选歌打分。**分越高越该推。**

    打分而不是取第一条，是因为网易云的搜索排序对"翻唱/改编"很友好 ——
    搜原唱经常第一条是别人的翻唱版。

    核心两条规则（都是被实际结果教出来的）：
    - **标题命中比歌手命中重要得多。** 只按歌手匹配会推出《默》来
      （因为它的歌手里有"周杰伦"），而用户搜的明明是《晴天》。
    - **查询里有两个词、却一个都不在标题里 → 强扣分。** 那基本不是他要的那首。
    """
    t = tastev or {}
    title = song.get("title") or ""
    album = song.get("album") or ""
    artist = song.get("artist") or ""
    q = (query or "").strip()
    score = 0.0

    qwords = [w for w in re.split(r"[\s,，、/·]+", q) if w]
    title_hits = [w for w in qwords if w in title]
    artist_hits = [w for w in qwords if w in artist]
    score += 18.0 * len(title_hits)
    score += 14.0 * len(artist_hits)
    if len(qwords) >= 2:
        if not title_hits:
            score -= 20.0                 # 搜的两个词一个都不在标题里，多半不是这首
        elif not artist_hits and q.strip():
            score -= 10.0                 # 标题对上了但歌手对不上 -> 可能是翻唱
    if title.strip() == q:
        score += 6.0

    # 翻唱/劣化版本扣分（权重很高，宁可换一首）
    if _hit(title, list(_BAD_TITLE)) or _hit(album, list(_BAD_TITLE)):
        score -= 30.0
    if _hit(title, list(_MILD_TITLE)):
        score -= 6.0                      # 现场版不算差，但优先录音室

    # 用户明确说喜欢的歌手 -> 加权
    for w in t.get("likes") or []:
        if w and (w in artist or w in title):
            score += 15.0

    # 播放次数（来自"最近在听"）—— 数量本身就是口味信号
    try:
        score += min(float(song.get("play") or 0), 200) / 20.0
    except (TypeError, ValueError):
        pass
    return score


def preferred(cfg: dict[str, Any], query: str, limit: int = 10) -> dict[str, Any]:
    """按倾向挑一首，返回 ``{song, candidates:[...]}``。挑不到就返回空 song。"""
    t = taste(cfg)
    cands = search(cfg, query, limit=limit) if query.strip() else recent(cfg, limit=limit)
    # 先剔掉明确不想要的
    cands = [s for s in cands
             if not (_hit(s.get("title", ""), t["dislikes"])
                     or _hit(s.get("artist", ""), t["dislikes"]))]
    if not cands:
        return {"song": {}, "candidates": []}

    ranked = sorted(cands, key=lambda s: score_song(s, query, t), reverse=True)

    # 最近一起听过的往后放，别每次都是同一首
    try:
        seen = {str(x.get("song_id")) for x in sessions(cfg, limit=20)}
    except Exception:  # noqa: BLE001
        seen = set()
    ranked.sort(key=lambda s: 1 if str(s.get("id")) in seen else 0)

    best = ranked[0]
    if not best.get("cover"):
        full = detail(cfg, best["id"], prov=best.get("provider", ""))
        if full:
            best.update({k: v for k, v in full.items() if v})
    best.setdefault("provider", provider(cfg))
    return {"song": best, "candidates": ranked[:limit],
            "why": _why(best, query, t)}


def _why(song: dict[str, Any], query: str, t: dict[str, Any]) -> str:
    """给一句"为什么挑它"，一起听时可以说出口，也方便排查。"""
    bits = []
    if t.get("likes") and _hit(song.get("artist", "") + song.get("title", ""), t["likes"]):
        bits.append("你喜欢的")
    if song.get("play"):
        bits.append(f"你听过 {song['play']} 次")
    if query and query.strip() and any(w and w in (song.get("artist") or "")
                                       for w in re.split(r"[\s,，、/]+", query)):
        bits.append("原唱")
    if _hit(song.get("title", ""), list(_BAD_TITLE)):
        bits.append("（这条是改编版）")
    return "、".join(bits)


def feedback(cfg: dict[str, Any], song: dict[str, Any], like: bool) -> dict[str, Any]:
    """用户对一首歌表态 -> 更新倾向（学他喜欢/讨厌哪个歌手）。"""
    t = taste(cfg)
    artist = (song.get("artist") or "").split("、")[0].strip()
    if not artist:
        return {"ok": False, "reason": "这首歌没有歌手信息"}
    likes = list(t["likes"])
    dislikes = list(t["dislikes"])
    if like:
        dislikes = [x for x in dislikes if x != artist]
        if artist not in likes:
            likes.append(artist)
    else:
        likes = [x for x in likes if x != artist]
        if artist not in dislikes:
            dislikes.append(artist)
    out = set_taste(cfg, likes=likes, dislikes=dislikes)
    print(f"[music] 听歌倾向更新：{'喜欢' if like else '不喜欢'} {artist}")
    return {"ok": True, "action": "like" if like else "dislike",
            "artist": artist, **out}


# ---------- 可播放地址 ----------

def song_url(cfg: dict[str, Any], song: dict[str, Any]) -> dict[str, Any]:
    """能不能直接播。返回 ``{url, web, playable}``。

    **未登录也要试。** 之前这里看到一次 ``fee=1`` 返回空就下了"未登录都拿不到"
    的结论，还在 ``_url_netease`` 里加了"没 cookie 就直接返回空"的短路 ——
    结果实测 8 首里 8 首都有直链（VIP 歌给试听片段，免费歌给完整地址）。
    那个短路把本来能播的歌全挡掉了，卡片只能退化成跳网页。
    """
    prov = song.get("provider") or provider(cfg)
    sid = str(song.get("id") or "")
    web = (f"https://y.qq.com/n/ryqq/songDetail/{sid}" if prov == "qq"
           else f"https://music.163.com/#/song?id={sid}")
    url = _url_qq(cfg, sid) if prov == "qq" else _url_netease(cfg, sid)
    return {"url": url, "web": web, "playable": bool(url)}


def _url_netease(cfg: dict[str, Any], song_id: str) -> str:
    try:
        j = _post(cfg, "/weapi/song/enhance/player/url/v1",
                  {"ids": f"[{song_id}]", "level": "standard", "encodeType": "aac"})
        d = (j.get("data") or [{}])[0]
        return str(d.get("url") or "")
    except Exception:  # noqa: BLE001
        return ""


def _url_qq(cfg: dict[str, Any], song_mid: str) -> str:
    ck = cookie_qq(cfg)
    m = re.search(r"(?:^|;\s*)uin=([^;]+)", ck)
    uin = (m.group(1).lstrip("o") if m else "") or "0"
    # 未登录也试一遍：loginflag=0 + platform=20（网页版）有时能拿到免费歌的 purl
    for loginflag in ((1 if ck else 0), 0):
        body = {"req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                          "param": {"guid": "10000", "songmid": [song_mid], "songtype": [0],
                                    "uin": uin, "loginflag": loginflag, "platform": "20"}}}
        try:
            with _qq()._client(cfg) as c:
                j = c.post(_qq()._MUSICU, json=body).json()
            data = ((j.get("req_0") or {}).get("data") or {})
            infos = data.get("midurlinfo") or []
            purl = (infos[0].get("purl") if infos else "") or ""
            if purl:
                sip = (data.get("sip") or [""])[0]
                return (sip + purl) if purl.startswith("/") else purl
        except Exception:  # noqa: BLE001
            continue
    return ""


def card(cfg: dict[str, Any], song: dict[str, Any], comment: str = "") -> dict[str, Any]:
    """组装一张可以在聊天里渲染的音乐卡片。"""
    u = song_url(cfg, song)
    return {
        "kind": "music",
        "provider": song.get("provider") or provider(cfg),
        "id": str(song.get("id") or ""),
        "title": song.get("title") or "",
        "artist": song.get("artist") or "",
        "album": song.get("album") or "",
        "cover": song.get("cover") or "",
        "url": u["url"],
        "web": u["web"],
        "playable": u["playable"],
        "comment": comment,
    }


def card_marker(payload: dict[str, Any]) -> str:
    """把卡片编码成一个标记，塞进回复文本里。

    用 base64 而不是裸 JSON：JSON 里有 ``]`` ``|`` 这类字符，
    放在 ``[标记:...]`` 里迟早被切坏。前端解出来直接渲染。
    """
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "[音乐卡:" + base64.b64encode(raw.encode()).decode() + "]"


def parse_card_marker(token: str) -> dict[str, Any] | None:
    try:
        data = json.loads(base64.b64decode(token.encode()).decode())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ---------- 一起听：记录与分享 ----------

def record_session(cfg: dict[str, Any], song: dict[str, Any], comment: str,
                   source: str = "chat", dedupe_sec: float = 300.0) -> int:
    """记一条"一起听过"。

    **短时间内的同歌同来源会并成一条。** 实测用户会连点「一起听」——
    第一次要调模型生成邀请语（约十秒），等不及就再点一次，于是同一个歌
    出现两条记录。同一首歌在 5 分钟内重复记，就更新那一条的评论而不是再插一条。
    （不同来源不合并：她"分享"一次、你"一起听"一次，是两件事。）
    """
    try:
        with _conn(cfg) as c, c:
            _setup(c)
            if dedupe_sec > 0 and song.get("id"):
                row = c.execute(
                    "SELECT id, comment FROM music_sessions"
                    " WHERE song_id=? AND source=? AND ts>=? ORDER BY id DESC LIMIT 1",
                    (str(song.get("id")), source, time.time() - dedupe_sec)).fetchone()
                if row:
                    # 新评论更长就换上（比如本来记的是空/旧话）
                    if comment and len(comment) > len(row["comment"] or ""):
                        c.execute("UPDATE music_sessions SET comment=?, ts=? WHERE id=?",
                                  (comment, time.time(), row["id"]))
                    print(f"[music] 同一首已在 {int(dedupe_sec)}s 内记过，合并到 #{row['id']}")
                    return int(row["id"])
            cur = c.execute(
                "INSERT INTO music_sessions(ts,song_id,title,artist,cover,comment,source)"
                " VALUES(?,?,?,?,?,?,?)",
                (time.time(), song.get("id", ""), song.get("title", ""),
                 song.get("artist", ""), song.get("cover", ""), comment, source))
            return int(cur.lastrowid or 0)
    except sqlite3.Error as exc:
        print(f"[music] 记录失败：{exc}")
        return 0


# ---------- "要我讲讲吗" 的待讲状态 ----------

def set_pending(cfg: dict[str, Any], song: dict[str, Any], lyric: str = "") -> None:
    """记下"刚一起听了这首，还没讲"。

    用户要求：**别一上来就长篇大论**，先问一句要不要讲，等他点头再展开。
    所以这里存下歌和歌词，等他回话。
    """
    _kv_set(cfg, "pending_music", {"song": song, "lyric": lyric, "ts": time.time()})


def get_pending(cfg: dict[str, Any], ttl: float = 900.0) -> dict[str, Any] | None:
    """取待讲的歌。超过 ``ttl``（默认 15 分钟）就当过期，不再追问。"""
    d = _kv_get(cfg, "pending_music")
    if not isinstance(d, dict) or not d.get("song"):
        return None
    if time.time() - float(d.get("ts") or 0) > ttl:
        return None
    return d


def clear_pending(cfg: dict[str, Any]) -> None:
    _kv_set(cfg, "pending_music", None)


def sessions(cfg: dict[str, Any], limit: int = 30) -> list[dict[str, Any]]:
    try:
        with _conn(cfg) as c:
            _setup(c)
            rows = c.execute("SELECT * FROM music_sessions ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def delete_session(cfg: dict[str, Any], sid: int) -> int:
    try:
        with _conn(cfg) as c, c:
            _setup(c)
            n = c.execute("SELECT COUNT(*) FROM music_sessions WHERE id=?", (int(sid),)).fetchone()[0]
            c.execute("DELETE FROM music_sessions WHERE id=?", (int(sid),))
        return int(n)
    except sqlite3.Error:
        return 0


def pick(cfg: dict[str, Any], keyword: str = "") -> dict[str, Any]:
    """挑一首歌（走倾向打分，不再无脑取搜索第一条）。"""
    return preferred(cfg, keyword)["song"] or _fallback_pick(cfg, keyword)


def _fallback_pick(cfg: dict[str, Any], keyword: str = "") -> dict[str, Any]:
    """打分路径全军覆没时的兜底（比如搜索接口被风控）。"""
    if keyword.strip():
        hits = search(cfg, keyword.strip(), limit=5)
        song = hits[0] if hits else {}
    else:
        rec = recent(cfg, limit=10)
        song = rec[0] if rec else {}
    if song and not song.get("cover"):
        full = detail(cfg, song["id"], prov=song.get("provider", ""))
        if full:
            song.update({k: v for k, v in full.items() if v})
    song.setdefault("provider", provider(cfg))
    return song


def stats(cfg: dict[str, Any]) -> dict[str, Any]:
    rows = sessions(cfg, limit=1000)
    artists: dict[str, int] = {}
    for r in rows:
        if r.get("artist"):
            artists[r["artist"].split("、")[0]] = artists.get(r["artist"].split("、")[0], 0) + 1
    top = sorted(artists.items(), key=lambda kv: -kv[1])[:5]
    return {"sessions": len(rows), "top_artists": top,
            "last": rows[0] if rows else None}


if __name__ == "__main__":  # 自检：python -m neko.music
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .config import load_config

    cfg = load_config()
    print("音乐功能:", "已启用" if enabled(cfg) else "已关闭")
    print("登录状态:", state(cfg))
    print("搜歌测试:", [s["title"] for s in search(cfg, "周杰伦", limit=3)])
    print("一起听记录:", stats(cfg))
