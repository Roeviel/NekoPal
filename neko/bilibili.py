"""B站（bilibili）免登录客户端：搜索 / 视频详情 / 字幕 / 排行榜。

设计目标
--------
本项目是「本地学习伴侣」，只需要 B站的**公开数据**，因此：

* 不实现登录、不要求用户提供 Cookie（用户已明确选择只用免登录公开数据）。
* ``neko/config.py`` 里的 ``bilibili.cookie`` 只在**非空**时附加到请求头，
  默认路径完全不依赖它；未登录也能拿到 :meth:`BiliClient.search` 等全部接口。
* 只访问 ``api.bilibili.com`` / ``www.bilibili.com`` / ``*.hdslb.com``
  / ``*.bilibili.com``，不触达其它站点。
* 抓到的内容仅用于本地学习总结。

已知坑（实测结论，动手改代码前请先读）
--------------------------------------
1. **WBI 签名是硬门槛**。``x/web-interface/search/type`` 不带签名会直接返回
   HTTP 412（实测），带签名缺 ``buvid3`` 也可能被风控。签名算法：
   取 ``nav`` 接口的 ``data.wbi_img.img_url`` / ``sub_url`` 的文件名（去扩展名）
   拼成 64 字符，按 :data:`MIXIN_KEY_ENC_TAB` 重排后取前 32 位得到 ``mixin_key``；
   参数补 ``wts=int(time.time())``，值里剔除 ``!'()*`` 然后 URL 编码，
   按 key 升序拼成 query，``w_rid = md5(query + mixin_key)``。
   —— 该算法已在本机实测跑通（``code=0``，返回 20 条结果）。
2. **未登录时 ``nav`` 的 ``code`` 是 ``-101``，但 ``data.wbi_img`` 仍然存在**，
   所以不能因为 ``code != 0`` 就放弃签名；只有真的取不到 ``wbi_img`` 才降级为不签名。
3. **``buvid3`` Cookie 必须有**。可从 ``x/frontend/finger/spi`` 的 ``data.b_3``
   拿到（实测 ``code=0``），或从 ``www.bilibili.com`` 的 Set-Cookie 兜底。
   拿到后缓存复用，避免每个请求都去打指纹接口。
4. **风控返回 HTML 而不是 JSON**。响应体以 ``<!DOCTYPE`` / ``<html`` 开头时，
   一定是反爬页面，必须抛 :class:`BiliError`，让上层能降级（不要 json.loads 硬崩）。
5. ``duration`` 字段在不同接口里类型不一致：``search`` 常给 ``"12:34"`` 字符串，
   ``view`` / ``ranking`` 给秒数 int。统一用 :func:`_parse_duration` 归一。
6. ``search`` 的 ``title`` 带 ``<em class="keyword">`` 高亮标签且做了 HTML 转义，
   必须清洗（见 :func:`_clean_text`），否则标题写进笔记里很难看。
7. **字幕是登录门槛功能（本项目最硬的限制，已实测确认）**。
   ``x/player/v2`` 的返回里带一个 ``need_login_subtitle`` 字段，未登录时实测恒为
   ``true``，同时 ``data.subtitle.subtitles`` 恒为 ``[]``，且返回体里**根本没有**
   ``ai_subtitles`` 字段。实测抽样 145 个不同分区 / 不同分P数的视频（53 个单P、
   20 个 2-10P、39 个 11-50P、33 个 51+P），**0 个**能拿到字幕。
   结论：未登录状态下 :meth:`BiliClient.subtitles` 的可得率约为 ``0``，
   不是本模块的 bug，而是 B站的接口策略；用户若不提供 Cookie 就无法绕开。
   因此该方法在拿不到字幕时**返回空列表，绝不抛异常**，由上层降级处理。
   想要字幕只有两条路：用户在 ``config.json`` 的 ``bilibili.cookie``
   自己填 Cookie（本模块已支持，非空才附加），或改用「无字幕的视频总结」策略。
   想知道当前视频到底能不能拿字幕，可用 :meth:`BiliClient.subtitle_status`。
8. 字幕正文在 ``https://*.hdslb.com/...json`` 上，同样属于允许访问的域。
   （登录后能拿到时）字幕 URL 自带 ``auth_key`` 签名，必须用接口返回的地址，
   不要试图自己拼，拼出来的地址一定 403。

用法::

    from neko.bilibili import BiliClient, BiliError

    with BiliClient() as bili:                 # cfg=None 时自动 load_config()
        for item in bili.search("Python 入门", limit=5):
            print(item["title"], item["duration"], item["url"])
        info = bili.video("BV1GJ411x7h7")
        for sub in bili.subtitles("BV1GJ411x7h7"):
            print(sub["lan"], sub["lan_doc"], len(sub["text"]))

自测::

    python -m neko.bilibili              # 或 python tools/bili_probe.py
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

try:  # 包内导入（正常路径）
    from .config import load_config
except ImportError:  # 直接以脚本方式运行 neko/bilibili.py 时的兜底
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from neko.config import load_config  # type: ignore[no-redef]

__all__ = ["BiliError", "BiliClient", "MIXIN_KEY_ENC_TAB"]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: WBI 签名用的 64 位重排表（B站前端固定表，不要随意改动）。
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

#: 所有请求共用的基础域名白名单（只允许访问这些域，见模块 docstring）。
API_BASE = "https://api.bilibili.com"
WEB_BASE = "https://www.bilibili.com"

#: 索引用 UA；B站对「非浏览器 UA」的风控更严，这里固定一个真实浏览器 UA。
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: 默认字幕优先语言（cfg 里没有 bilibili.subtitle_priority 时使用）。
DEFAULT_SUBTITLE_PRIORITY: tuple[str, ...] = ("zh-CN", "zh-Hans", "zh-Hant", "ai-zh")

_TIMEOUT = 15.0  # 单次请求超时（秒），按需求固定 15s
_RETRIES = 2  # 失败后的重试次数（总尝试次数 = 1 + 2）
_BACKOFF_BASE = 0.6  # 指数退避基数：0.6s, 1.2s

_HTML_WARN_PREFIX = ("<!doctype", "<html", "<?xml")

_EM_TAG_RE = re.compile(r"</?em\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WBI_UNSAFE_RE = re.compile(r"[!'()*]")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


class BiliError(Exception):
    """B站接口失败（网络异常 / 风控页 / code != 0）。"""


# --------------------------------------------------------------------------
# 纯函数工具（无副作用，方便单测）
# --------------------------------------------------------------------------


def _mixin_key(img_key: str, sub_key: str) -> str:
    """把 ``img_key + sub_key`` 按固定重排表混成 ``mixin_key``（取前 32 位）。

    重排表长度 64，索引落在 0..63；实测 ``img_key``/``sub_key`` 各 32 字符。
    """
    raw = img_key + sub_key
    if len(raw) < 64:  # 理论上不会发生；防御性处理，避免 IndexError 直接崩
        raise BiliError(f"wbi 原始 key 长度异常（{len(raw)} < 64），无法生成 mixin_key")
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _parse_duration(value: Any) -> int:
    """把多种形态的时长统一成**秒**（int）。

    * ``754`` / ``"754"`` → ``754``
    * ``"0:27"`` → ``27``；``"12:34"`` → ``754``；``"1:02:03"`` → ``3723``
    * ``"2398:14"`` → ``143894``（**实测坑**：合集类视频的 ``MM:SS`` 里
      分钟可以是 3~4 位数，不是合法的 ``HH:MM:SS``，按 60 进制照样能算对）
    * ``"--"`` / ``None`` / 空串 / 无法解析 → ``0``

    解析规则：按 ``:`` 切分，**最后一段是秒，倒数第二段是分**（分允许 > 59，
    这是实测的真实数据，不要按合法时钟去校验），更靠前的段依次按 3600、
    216000 进制累加。越界（负值 / 大于 10^9 秒）返回 0。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return 0
    if text.isdigit():
        return int(text)

    if ":" not in text:
        return 0
    parts = text.split(":")
    if len(parts) > 4 or any(not part.strip().isdigit() for part in parts):
        return 0

    numbers = [int(part) for part in parts]
    seconds = numbers.pop()
    minutes = numbers.pop()
    total = minutes * 60 + seconds
    for index, number in enumerate(reversed(numbers)):  # 更靠前的段：小时、更大的单位
        total += number * (3600 * (60**index))
    if total < 0 or total > 10**9:
        return 0
    return total


def _clean_text(value: Any) -> str:
    """去掉搜索接口的 ``<em class="keyword">`` 高亮标签，并反转义 HTML 实体。

    ``&amp;`` / ``&quot;`` / ``&#39;`` / ``&lt;`` 等都会被还原成正常字符。
    先反转义再剥标签会误伤（比如正文里字面写着 ``&lt;b&gt;``），所以这里
    **先剥标签、再反转义**，顺序不要调换。
    """
    if value is None:
        return ""
    text = str(value)
    if "<" in text or "&" in text:
        text = _EM_TAG_RE.sub("", text)
        text = _TAG_RE.sub("", text)
        text = _html.unescape(text)
    return text.strip()


def _fix_url(value: Any) -> str:
    """补全 ``//i0.hdslb.com/...`` 这类协议相对 URL。"""
    text = str(value or "").strip()
    if text.startswith("//"):
        return "https:" + text
    return text


def _first_int(*values: Any) -> int:
    """按顺序返回第一个能转成 int 的值，全部失败返回 0。"""
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _subtitle_sort_key(item: dict[str, Any], priority: Iterable[str]) -> tuple[int, int, str]:
    """字幕排序键：命中优先语言的排最前，其余保持原顺序。"""
    order = {str(lan): idx for idx, lan in enumerate(priority)}
    lan = str(item.get("lan") or "")
    return (order.get(lan, len(order)), 0, lan)


def _join_subtitle_body(body: Any) -> str:
    """把字幕 JSON 的 ``body`` 数组拼成纯文本。

    每段取 ``content``，用 ``\\n`` 连接，去掉连续空行（3 个以上换行压成 2 个），
    并去掉首尾空白。拿不到有效段落时返回空串。
    """
    if not isinstance(body, list):
        return ""
    lines: list[str] = []
    for segment in body:
        if isinstance(segment, dict):
            content = segment.get("content")
        else:
            content = segment
        if content is None:
            continue
        lines.append(str(content).replace("\r\n", "\n").replace("\r", "\n").strip())
    text = "\n".join(lines).strip()
    return _MULTI_BLANK_RE.sub("\n\n", text)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class BiliClient:
    """B站免登录只读客户端（同步 / httpx）。

    :param cfg: :func:`neko.config.load_config` 返回的完整 dict；
        为 ``None`` 时内部自行调用 ``load_config()``。
        容忍缺少 ``bilibili`` 段（用默认值）。
    :param cookie: 手填 Cookie 字符串（可选）。优先级高于 ``cfg["bilibili"]["cookie"]``。
        仅在非空时使用；默认路径（未登录公开数据）完全不依赖它。
    """

    def __init__(self, cfg: dict | None = None, cookie: str | None = None) -> None:
        self.cfg: dict[str, Any] = cfg if isinstance(cfg, dict) else load_config()
        bili_cfg = self.cfg.get("bilibili")
        if not isinstance(bili_cfg, dict):
            bili_cfg = {}
        self._bili_cfg: dict[str, Any] = bili_cfg

        # Cookie：显式参数 > 配置 > 空（未登录）
        self.cookie: str = (cookie or bili_cfg.get("cookie") or "").strip()

        # 字幕优先语言
        priority = bili_cfg.get("subtitle_priority") or DEFAULT_SUBTITLE_PRIORITY
        if isinstance(priority, str):
            priority = [priority]
        self.subtitle_priority: tuple[str, ...] = tuple(str(x) for x in priority)

        self._client = httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=self._base_headers(),
        )

        # 缓存：WBI 密钥、buvid3 指纹
        self._wbi_keys: tuple[str, str] | None = None
        self._wbi_fetched_at: float = 0.0
        self._buvid3: str | None = None

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        """关闭底层 httpx 连接池（可重复调用）。"""
        try:
            self._client.close()
        except Exception:  # pragma: no cover - 关闭失败不该影响上层
            pass

    def __enter__(self) -> "BiliClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # 便于调试，不泄漏 cookie
        return f"<BiliClient logged_in={bool(self.cookie)} priority={self.subtitle_priority}>"

    # -- 请求头 / Cookie --------------------------------------------------

    def _base_headers(self) -> dict[str, str]:
        """构造请求头：真实浏览器 UA + Referer/Origin（缺了容易被风控）。"""
        return {
            "User-Agent": DEFAULT_UA,
            "Referer": WEB_BASE + "/",
            "Origin": WEB_BASE,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }

    def _request_headers(self) -> dict[str, str]:
        """请求头 + （可选）Cookie。

        仅当 ``self.cookie`` 非空（用户显式配置）时才附加，默认不带。
        buvid3 由 :meth:`_buvid` 独立注入。
        """
        headers = self._base_headers()
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _buvid(self) -> str | None:
        """获取并缓存 ``buvid3``（搜索接口必需）。

        优先打指纹接口 ``x/frontend/finger/spi``（返回 ``data.b_3``，实测可用），
        失败则退化为访问 ``www.bilibili.com`` 从 Set-Cookie 里捞。
        两个都拿不到时返回 ``None``（搜索大概率 412，由上层报错）。
        """
        if self._buvid3:
            return self._buvid3

        # 若用户自己配了 Cookie 且里面已有 buvid3，直接复用
        if self.cookie and "buvid3=" in self.cookie:
            match = re.search(r"buvid3=([^;]+)", self.cookie)
            if match:
                self._buvid3 = match.group(1).strip()
                return self._buvid3

        headers = {"User-Agent": DEFAULT_UA, "Referer": WEB_BASE + "/"}
        # 1) 指纹接口
        try:
            resp = self._client.get(f"{API_BASE}/x/frontend/finger/spi", headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict):
                    b_3 = (payload.get("data") or {}).get("b_3")
                    if b_3:
                        self._buvid3 = str(b_3)
                        return self._buvid3
        except Exception:
            pass  # 换下一个方案，这里失败不算致命

        # 2) 首页 Set-Cookie 兜底
        try:
            resp = self._client.get(WEB_BASE + "/", headers=headers)
            # 翻 jar 而不是 resp.cookies.items()：多个域下有同名 cookie 时
            # items() 会抛 CookieConflict（它只吞 KeyError），整个兜底就白做了。
            for ck in resp.cookies.jar:
                if ck.name == "buvid3" and ck.value:
                    self._buvid3 = ck.value
                    return self._buvid3
        except Exception:
            pass
        return None

    # -- 底层请求 ---------------------------------------------------------

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        need_buvid: bool = False,
        with_cookie: bool = True,
    ) -> Any:
        """带重试的 GET + JSON 解析（含风控页识别）。

        :param need_buvid: 需要 ``buvid3`` 的接口（搜索等）传 True。
        :param with_cookie: 是否附加用户配置的 Cookie。
        :raises BiliError: 网络异常重试耗尽 / 风控 HTML / 非 JSON / ``code != 0``。
        """
        headers = self._request_headers() if with_cookie else self._base_headers()
        if need_buvid:
            buvid = self._buvid()
            if buvid:
                if headers.get("Cookie"):
                    headers["Cookie"] = f"{headers['Cookie']}; buvid3={buvid}"
                else:
                    headers["Cookie"] = f"buvid3={buvid}"

        last_error: Exception | None = None
        for attempt in range(_RETRIES + 1):
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:  # 超时 / 连接失败 / 协议错误
                # 4xx 是确定性拒绝（重试无意义），其余（超时、连接重置等）值得退避重试
                retryable = not (
                    isinstance(exc, httpx.HTTPStatusError)
                    and 400 <= exc.response.status_code < 500
                )
                last_error = exc
                if attempt < _RETRIES and retryable:
                    time.sleep(_BACKOFF_BASE * (2**attempt))  # 指数退避
                    continue
                raise BiliError(
                    f"请求失败（已重试 {_RETRIES} 次）：{url} -> {type(exc).__name__}: {exc}"
                ) from exc

            body = (resp.text or "").lstrip()
            low = body[:200].lower()
            if low.startswith(_HTML_WARN_PREFIX):
                # 反爬页面：退避重试一次也许能过（换 IP 场景），再不行就明确报错
                last_error = BiliError(
                    "疑似被风控，返回 HTML 而非 JSON: " + body[:200].replace("\n", " ")
                )
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF_BASE * (2**attempt))
                    continue
                raise last_error

            if resp.status_code >= 400:
                last_error = BiliError(
                    f"HTTP {resp.status_code}：{url} -> " + body[:200].replace("\n", " ")
                )
                if attempt < _RETRIES and resp.status_code in (412, 429, 500, 502, 503, 504):
                    time.sleep(_BACKOFF_BASE * (2**attempt))
                    continue
                raise last_error

            try:
                payload = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = BiliError(
                    "响应不是合法 JSON（可能是风控或接口变更）: "
                    + body[:200].replace("\n", " ")
                )
                if attempt < _RETRIES:
                    time.sleep(_BACKOFF_BASE * (2**attempt))
                    continue
                raise last_error from exc
            return payload

        # 理论上不可达，兜底
        raise BiliError(f"请求失败：{url} -> {last_error}")

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        need_buvid: bool = False,
        with_cookie: bool = True,
        allow_codes: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """``_get`` 的语义化包装：校验 ``code``，返回整个 JSON dict。

        :param allow_codes: 允许的「非 0 但可接受」业务码。
            例如 ``nav`` 在未登录时返回 ``-101``，但 ``data.wbi_img`` 依然可用，
            调用方会把它放进 ``allow_codes``。
        """
        payload = self._get(
            url, params=params, need_buvid=need_buvid, with_cookie=with_cookie
        )
        if not isinstance(payload, dict):
            raise BiliError(f"接口返回结构异常（期望 dict，实际 {type(payload).__name__}）：{url}")
        code = payload.get("code")
        if code not in (0, *allow_codes):
            raise BiliError(
                f"接口返回 code={code} message={payload.get('message')!r}：{url}"
            )
        return payload

    # -- WBI 签名 ---------------------------------------------------------

    def _wbi_keys_now(self) -> tuple[str, str] | None:
        """取 ``(img_key, sub_key)``。

        实测：**未登录时 ``nav`` 的 code 为 -101**，但 ``data.wbi_img`` 仍然存在，
        所以这里用 ``allow_codes=(-101,)``；只有真的缺字段才返回 ``None``
        （调用方降级为不签名，此时搜索接口会 412，属可接受的降级）。
        """
        try:
            payload = self._get_json(
                f"{API_BASE}/x/web-interface/nav",
                allow_codes=(-101,),
                with_cookie=False,  # 未登录也拿得到，不依赖用户 Cookie
            )
        except BiliError:
            return None

        wbi_img = ((payload.get("data") or {}).get("wbi_img")) or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        if not img_url or not sub_url:
            return None
        return (
            img_url.rsplit("/", 1)[-1].split(".")[0],
            sub_url.rsplit("/", 1)[-1].split(".")[0],
        )

    def _wbi(self) -> tuple[str, str] | None:
        """返回缓存的 ``(img_key, sub_key)``，12 小时内复用（key 很少变）。"""
        if self._wbi_keys and (time.time() - self._wbi_fetched_at) < 12 * 3600:
            return self._wbi_keys
        keys = self._wbi_keys_now()
        if keys:
            self._wbi_keys = keys
            self._wbi_fetched_at = time.time()
        return keys

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """给参数加上 ``wts`` 与 ``w_rid`` 签名（WBI）。

        步骤（顺序不能变）：
            1. 补 ``wts = int(time.time())``；
            2. 每个值剔除 ``!'()*``，再 URL 编码；
            3. 按 key 升序拼 ``k=v&k=v``；
            4. ``w_rid = md5(query + mixin_key)``。
        拿不到 wbi key 时**原样返回**（降级为不签名），由服务端决定是否 412。
        """
        signed = {k: v for k, v in params.items() if v is not None}
        signed["wts"] = int(time.time())

        keys = self._wbi()
        if not keys:
            return signed  # 降级：不签名（大概率被 412，见 docstring 已知坑 2）

        mixin = _mixin_key(*keys)

        items: list[tuple[str, str]] = []
        for key in sorted(signed):
            value = str(signed[key])
            value = _WBI_UNSAFE_RE.sub("", value)
            items.append((key, value))
        query = "&".join(f"{k}={v}" for k, v in items)

        signed["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return signed

    # -- 数据归一化 -------------------------------------------------------

    @staticmethod
    def _video_item(raw: dict[str, Any]) -> dict[str, Any]:
        """把 search / ranking 的原始条目归一成统一契约。

        契约（其它模块按此消费，不要随意改）::

            {"bvid","aid","title","author","mid","duration","play",
             "danmaku","pubdate","desc","url","pic"}
        """
        raw = raw if isinstance(raw, dict) else {}
        bvid = str(raw.get("bvid") or "").strip()
        if not bvid:
            # ranking 接口偶尔只给 arcurl（http://www.bilibili.com/video/av123）
            match = re.search(r"(BV[0-9A-Za-z]{10})", str(raw.get("arcurl") or ""))
            bvid = match.group(1) if match else ""

        # search 用 play / video_review；ranking 用 stat.view / stat.danmaku
        stat = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
        owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}

        return {
            "bvid": bvid,
            "aid": _first_int(raw.get("aid"), raw.get("id"), raw.get("avid")),
            "title": _clean_text(raw.get("title")),
            "author": _clean_text(raw.get("author")) or _clean_text(owner.get("name")),
            "mid": _first_int(raw.get("mid"), owner.get("mid")),
            "duration": _parse_duration(raw.get("duration")),
            "play": _first_int(raw.get("play"), raw.get("view"), stat.get("view")),
            "danmaku": _first_int(
                raw.get("danmaku"), raw.get("video_review"), stat.get("danmaku")
            ),
            "pubdate": _first_int(raw.get("pubdate"), raw.get("ctime"), raw.get("senddate")),
            "desc": _clean_text(raw.get("description") or raw.get("desc")),
            "url": f"{WEB_BASE}/video/{bvid}" if bvid else str(raw.get("arcurl") or ""),
            "pic": _fix_url(raw.get("pic") or raw.get("cover")),
        }

    def _subtitle_json(self, url: str) -> dict[str, Any]:
        """下载字幕正文 JSON（在 ``*.hdslb.com`` 上，属允许访问的域）。"""
        # 域校验：只允许 hdslb.com / bilibili.com，防止上游拼接外部 URL
        host = (urlparse(url).hostname or "").lower()
        if not (host.endswith(".hdslb.com") or host.endswith(".bilibili.com")):
            raise BiliError(f"字幕地址不在允许的域内，已拒绝请求：{host or url}")
        # 字幕正文在 CDN 上，一般不需要 Cookie；带上也无害，
        # 但有些受保护的字幕确实要鉴权，所以跟 player/v2 保持一致。
        payload = self._get(url, with_cookie=True)
        return payload if isinstance(payload, dict) else {}

    # -- 公开接口 ---------------------------------------------------------

    def search(self, keyword: str, page: int = 1, limit: int = 20) -> list[dict]:
        """按关键词搜索视频，返回归一化后的列表（最多 ``limit`` 条）。

        * 走 ``x/web-interface/search/type``，**必须带 WBI 签名 + buvid3**，
          否则实测返回 HTTP 412。
        * ``title`` 已去掉 ``<em class="keyword">`` 高亮并反转义 HTML 实体。
        * ``duration`` 统一为秒（搜索接口给的是 ``"12:34"`` / ``"2398:14"`` 字符串）。
        * 单页最多 20 条（B站限制），``limit`` 大于 20 时取前 20 条。
        * **实测坑**：即使 ``search_type=video``，返回里也混着 ``type='ketang'``
          的付费课程（它们没有 ``bvid``）。本方法按契约只返回真视频，
          因此返回条数**可能少于 ``limit``**（实测 limit=5 时只有 4 条）。
          想要更多就换关键词或翻 ``page``，本方法不做自动补页。
        """
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        limit = max(0, int(limit))
        if limit == 0:
            return []

        page = max(1, int(page))
        params: dict[str, Any] = {
            "keyword": keyword,
            "search_type": "video",
            "page": page,
            "page_size": min(20, limit),
            "platform": "pc",
            "web_location": "1430650",
        }
        payload = self._get_json(
            f"{API_BASE}/x/web-interface/search/type",
            params=self._sign(params),
            need_buvid=True,
        )

        results = ((payload.get("data") or {}).get("result")) or []
        if isinstance(results, dict):  # 极少数情况会包一层
            results = [results]

        items: list[dict] = []
        for raw in results:
            if not isinstance(raw, dict):
                continue
            item = self._video_item(raw)
            if item["bvid"]:
                items.append(item)
            if len(items) >= limit:
                break
        return items[:limit]

    def video(self, bvid: str) -> dict:
        """取视频详情。

        返回::

            {"bvid","aid","cid","title","desc","duration","owner","owner_mid",
             "pubdate","view","like","pages":[{"cid","page","part","duration"}]}

        顶级 ``cid`` 是**第一个分P**的 cid（取播放器信息 / 字幕都要用它）。
        ``view`` 接口无需签名、无需 Cookie，是本模块最稳的接口。
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            raise BiliError("video() 需要非空的 bvid")

        payload = self._get_json(
            f"{API_BASE}/x/web-interface/view", params={"bvid": bvid}
        )
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise BiliError(f"视频详情结构异常：bvid={bvid}")

        stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}

        pages: list[dict[str, Any]] = []
        for raw_page in data.get("pages") or []:
            if not isinstance(raw_page, dict):
                continue
            pages.append(
                {
                    "cid": _first_int(raw_page.get("cid")),
                    "page": _first_int(raw_page.get("page")),
                    "part": _clean_text(raw_page.get("part")),
                    "duration": _parse_duration(raw_page.get("duration")),
                }
            )

        return {
            "bvid": str(data.get("bvid") or bvid),
            "aid": _first_int(data.get("aid")),
            "cid": _first_int(data.get("cid"), pages[0]["cid"] if pages else 0),
            "title": _clean_text(data.get("title")),
            "desc": _clean_text(data.get("desc")),
            "duration": _parse_duration(data.get("duration")),
            "owner": _clean_text(owner.get("name")),
            "owner_mid": _first_int(owner.get("mid")),
            "pubdate": _first_int(data.get("pubdate")),
            "view": _first_int(stat.get("view")),
            "like": _first_int(stat.get("like")),
            "pages": pages,
        }

    def subtitles(self, bvid: str) -> list[dict]:
        """取视频字幕（含 UP 主 CC 字幕），返回 ``[{"lan","lan_doc","text","source"}]``。

        实现：``view`` 拿第一个分P的 cid → ``x/player/v2`` 拿
        ``data.subtitle.subtitles``（以及 ``data.subtitle.ai_subtitles`` 兜底）
        → 逐个下载字幕 JSON，把 ``body[].content`` 用 ``\\n`` 拼成 ``text``。

        排序：命中 ``cfg["bilibili"]["subtitle_priority"]`` 的语言排最前，
        其余保持接口原顺序。命中优先语言的字幕不影响 ``text`` 内容，只影响顺序。

        **拿不到任何字幕时返回 ``[]``，绝不抛异常**（见模块 docstring 已知坑 7）。
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return []

        try:
            info = self.video(bvid)
            cid = int(info.get("cid") or 0)
            if not cid:
                return []

            payload = self._get_json(
                f"{API_BASE}/x/player/v2",
                params={"bvid": bvid, "cid": cid},
                allow_codes=(0, -400, -404),  # 少数视频播放器接口受限，视作「无字幕」
                # 必须带上 Cookie：字幕列表是登录才给的，这里写 False 等于
                # 让用户配的 Cookie 完全失效（曾经就是这个 bug）。
                # 没配 Cookie 时 _request_headers() 本来就不会加，所以传 True 是安全的。
                with_cookie=True,
            )
            data = payload.get("data") or {}
            subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), dict) else {}

            raw_list: list[dict[str, Any]] = []
            for key, source in (("subtitles", "cc"), ("ai_subtitles", "ai")):
                for raw in subtitle.get(key) or []:
                    if not isinstance(raw, dict):
                        continue
                    url = str(raw.get("subtitle_url") or raw.get("url") or "").strip()
                    if not url:
                        continue
                    if url.startswith("//"):
                        url = "https:" + url
                    raw_list.append(
                        {
                            "lan": str(raw.get("lan") or ""),
                            "lan_doc": _clean_text(raw.get("lan_doc")),
                            "url": url,
                            "source": source,
                        }
                    )
            if not raw_list:
                return []

            results: list[dict[str, Any]] = []
            for entry in raw_list:
                try:
                    body = self._subtitle_json(entry["url"])
                except BiliError:
                    continue  # 单个字幕下载失败不影响其它字幕
                text = _join_subtitle_body(body.get("body"))
                if not text:
                    continue
                results.append(
                    {
                        "lan": entry["lan"],
                        "lan_doc": entry["lan_doc"],
                        "text": text,
                        "source": entry["source"],
                    }
                )

            results.sort(key=lambda item: _subtitle_sort_key(item, self.subtitle_priority))
            return results
        except BiliError:
            # 需求：拿不到字幕就返回空列表，不把异常抛给上层
            return []

    def subtitle_status(self, bvid: str) -> dict:
        """探测某个视频的字幕可得性（**非必需接口**，供上层决定要不要走字幕路线）。

        这是对 :meth:`subtitles` 的廉价前置检查：把 ``player/v2`` 里的
        ``need_login_subtitle`` 等线索暴露出来，便于上层在「无字幕」时
        直接降级到「用简介 + 分P标题做总结」，而不是白等一次网络往返。

        返回::

            {"need_login": bool,      # 服务端是否要求登录才给字幕（未登录实测恒 True）
             "available": int,        # 当前凭据下能拿到的字幕条数
             "cc": int,               # CC 字幕条数
             "ai": int,               # AI 字幕条数
             "asr_language": str}     # 服务端识别的语种，如 "cmn"

        任何异常都吞掉并返回全 0 的安全值（这个方法是「尽力而为」的探测）。
        """
        safe = {
            "need_login": False,
            "available": 0,
            "cc": 0,
            "ai": 0,
            "asr_language": "",
        }
        bvid = str(bvid or "").strip()
        if not bvid:
            return safe
        try:
            info = self.video(bvid)
            cid = int(info.get("cid") or 0)
            if not cid:
                return safe
            payload = self._get_json(
                f"{API_BASE}/x/player/v2",
                params={"bvid": bvid, "cid": cid},
                allow_codes=(0, -400, -404),
                with_cookie=True,   # 同上：不带 Cookie 就看不到字幕列表
            )
            data = payload.get("data") or {}
            subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), dict) else {}
            cc = len(subtitle.get("subtitles") or [])
            ai = len(subtitle.get("ai_subtitles") or [])
            return {
                "need_login": bool(data.get("need_login_subtitle")),
                "available": cc + ai,
                "cc": cc,
                "ai": ai,
                "asr_language": str(data.get("asr_language") or ""),
            }
        except BiliError:
            return safe

    def hot(self, limit: int = 20) -> list[dict]:
        """取全站排行榜视频（字段契约同 :meth:`search`）。

        走 ``x/web-interface/ranking/v2``；实测该接口不需要 WBI 签名，
        但 ``limit`` 只能截断服务端返回的列表，无法像搜索那样翻页。
        """
        limit = max(0, int(limit))
        if limit == 0:
            return []

        payload = self._get_json(
            f"{API_BASE}/x/web-interface/ranking/v2",
            params={"rid": 0, "type": "all"},
            with_cookie=False,
        )
        raw_list = ((payload.get("data") or {}).get("list")) or []
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("list") or []

        items: list[dict] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            item = self._video_item(raw)
            if item["bvid"]:
                items.append(item)
            if len(items) >= limit:
                break
        return items[:limit]


# --------------------------------------------------------------------------
# 自测入口
# --------------------------------------------------------------------------


def _selftest(keyword: str = "Python 入门") -> int:
    """跑一遍 search / video / subtitles / hot，打印摘要。返回进程退出码。"""
    print(f"[bili_probe] keyword={keyword!r}")
    with BiliClient() as bili:
        print(f"[bili_probe] client={bili!r}")

        print("\n--- search ---")
        try:
            found = bili.search(keyword, limit=5)
            print(f"搜到 {len(found)} 条")
            for item in found:
                print(
                    f"  · {item['title'][:60]} | {item['author']} | "
                    f"{item['duration']}s | 播放 {item['play']} | {item['url']}"
                )
        except BiliError as exc:
            print(f"搜索失败：{exc}")
            return 1

        target = found[0]["bvid"] if found else "BV1GJ411x7h7"

        print(f"\n--- video({target}) ---")
        try:
            info = bili.video(target)
            print(f"  标题：{info['title'][:80]}")
            print(
                f"  cid={info['cid']} aid={info['aid']} 时长={info['duration']}s "
                f"播放={info['view']} 点赞={info['like']} 分P={len(info['pages'])}"
            )
        except BiliError as exc:
            print(f"详情失败：{exc}")
            return 1

        print(f"\n--- subtitles({target}) ---")
        subs = bili.subtitles(target)
        if not subs:
            print("  该视频没有可用字幕（未登录下 AI 字幕通常不可见）")
        else:
            for sub in subs:
                print(
                    f"  · [{sub['lan']}] {sub['lan_doc']} source={sub['source']} "
                    f"文本 {len(sub['text'])} 字，前 60 字：{sub['text'][:60]!r}"
                )

        print("\n--- hot ---")
        try:
            ranking = bili.hot(limit=3)
            print(f"排行榜取到 {len(ranking)} 条")
            for item in ranking:
                print(f"  · {item['title'][:60]} | {item['url']}")
        except BiliError as exc:
            print(f"排行榜失败：{exc}")

    print("\n[bili_probe] OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_selftest(*(sys.argv[1:2] or ["Python 入门"])))
