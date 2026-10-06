"""用量与预算：实时看余额，并给自动化调用上闸。

## 为什么要有这个

一次真实事故：`tools/selfcheck.py` 反复用 `channel="local"` 调 `/api/chat`，
14 次，每次都真花 token。用户事后才知道钱被花了 —— **既看不见，也拦不住**。

所以这个模块做两件事：

1. **看得见**：余额、今日花费、今日调用次数。界面上一行小字，不显眼。
2. **拦得住**：三道闸（下面详述）。

## 余额和花费怎么来的

DeepSeek 有官方余额接口 `GET /user/balance`（实测可用，返回 CNY 总额）。
于是：

- **余额** = 接口直接给的真实数字
- **今日花费** = 「今日第一次查询时的余额」− 「当前余额」

**不需要猜单价，也不怕它调价。** 这是这个方案最关键的一点。

## 三道闸

| 闸 | 规则 | 拦的是什么 |
|---|---|---|
| **重复消息抑制** | 同一句话在 10 分钟内出现第 4 次就不再调模型 | **直接对应那次事故**（14 次"你好呀"）。正常人不会连发 4 遍一模一样的字 |
| **频率限制** | 每分钟最多 12 次 | 死循环、脚本 bug |
| **每日花费上限** | 默认每日 2 元 | 兜底：前两道漏了也不会失控 |

余额接口拿不到时（断网等）**不拦，只记警告** —— "网络抖一下就不能聊天"
比"多花几毛钱"更让人难受。但会记下来，界面上显示为"余额未知"。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from .config import resolve_path

_lock = threading.RLock()
_balance_cache: dict[str, Any] = {"ts": 0.0, "value": None, "error": ""}

#: 被闸拦下来的次数（进程内），界面上用来提示"最近拦了几次"
_blocked = {"count": 0, "last": "", "last_ts": 0.0}

_DDL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL,
    source            TEXT,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cache_hit         INTEGER DEFAULT 0,
    cache_miss        INTEGER DEFAULT 0,
    ok                INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON llm_usage(ts);
"""

#: 老库缺的列，启动时补上（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
_MIGRATE = (
    ("cache_hit", "INTEGER DEFAULT 0"),
    ("cache_miss", "INTEGER DEFAULT 0"),
)


def _price(cfg: dict[str, Any]) -> dict[str, float]:
    c = conf(cfg)
    return {
        "hit": float(c.get("price_in_cache_hit", 0.5) or 0),
        "miss": float(c.get("price_in_cache_miss", 2.0) or 0),
        "out": float(c.get("price_out", 8.0) or 0),
    }


def estimate_cny(cfg: dict[str, Any], *, cache_hit: int = 0, cache_miss: int = 0,
                 out: int = 0) -> float:
    """按 token 数估算花费（元）。**只算蕴自己发起的调用。**"""
    p = _price(cfg)
    return (cache_hit * p["hit"] + cache_miss * p["miss"] + out * p["out"]) / 1_000_000.0


def conf(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return ((cfg or {}).get("budget") or {})


def enabled(cfg: dict[str, Any] | None) -> bool:
    return bool(conf(cfg).get("enabled", True))


def _db(cfg: dict[str, Any]) -> Path:
    p = Path((cfg.get("memory") or {}).get("db_path") or "data/neko.db")
    return p if p.is_absolute() else resolve_path(p)


@contextmanager
def _conn(cfg: dict[str, Any]):
    """借出连接，保证关闭（`with sqlite3.connect()` 只管事务、不关连接）。"""
    conn = sqlite3.connect(_db(cfg), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_DDL)
        for col, decl in _MIGRATE:            # 老库补列
            try:
                conn.execute(f"ALTER TABLE llm_usage ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        yield conn
    finally:
        conn.close()


# ---------- kv 小工具（不依赖 Memory，避免循环导入） ----------

def _kv_get(cfg: dict[str, Any], key: str) -> Any:
    try:
        with _conn(cfg) as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            return row[0]
    except sqlite3.Error:
        return None


def _kv_set(cfg: dict[str, Any], key: str, value: Any) -> None:
    try:
        with _conn(cfg) as c, c:
            c.execute("INSERT INTO kv(key, value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))
    except sqlite3.Error as exc:
        print(f"[budget] 写 kv 失败：{exc}")


# ---------- 余额 ----------

def balance(cfg: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """查余额（带缓存）。返回 {ok, cny, currency, error}。"""
    if not enabled(cfg):
        return {"ok": False, "cny": None, "error": "预算功能已关闭"}
    ttl = float(conf(cfg).get("balance_ttl_sec", 120) or 120)
    now = time.time()
    with _lock:
        if not force and _balance_cache["value"] is not None and now - _balance_cache["ts"] < ttl:
            return dict(_balance_cache["value"])

    llm = (cfg.get("llm") or {})
    key = str(llm.get("api_key") or "")
    base = str(llm.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not key:
        out = {"ok": False, "cny": None, "error": "没配 api_key"}
    else:
        try:
            with httpx.Client(timeout=12.0) as c:
                r = c.get(f"{base}/user/balance",
                          headers={"Authorization": f"Bearer {key}"})
            if r.status_code != 200:
                out = {"ok": False, "cny": None, "error": f"HTTP {r.status_code}"}
            else:
                data = r.json()
                infos = data.get("balance_infos") or []
                info = infos[0] if infos else {}
                val = info.get("total_balance")
                out = {"ok": val is not None, "cny": float(val) if val is not None else None,
                       "currency": info.get("currency") or "CNY",
                       "available": bool(data.get("is_available")),
                       "error": "" if val is not None else "返回里没有余额"}
        except (httpx.HTTPError, ValueError) as exc:
            out = {"ok": False, "cny": None, "error": f"{type(exc).__name__}"}
    with _lock:
        _balance_cache.update(ts=now, value=out, error=out.get("error") or "")
    return dict(out)


# ---------- 今日花费 ----------

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def spend_today(cfg: dict[str, Any], *, refresh: bool = False) -> dict[str, Any]:
    """今日花费 = 今日首次记录的余额 − 当前余额。

    同时维护一个**累计基线**：第一次记录到的余额。这样"自本功能上线以来
    一共花了多少"也能算出来 —— 界面上主显示用的是消耗金额。
    """
    if not enabled(cfg):
        return {"cny": None, "balance": None, "error": "已关闭"}
    bal = balance(cfg, force=refresh)
    if not bal.get("ok"):
        return {"cny": None, "balance": None, "error": bal.get("error") or "余额未知"}

    today = _today()
    day = _kv_get(cfg, "budget_day")
    start = _kv_get(cfg, "budget_day_start")
    if day != today or not isinstance(start, (int, float)):
        _kv_set(cfg, "budget_day", today)
        _kv_set(cfg, "budget_day_start", bal["cny"])
        start = bal["cny"]

    # 累计基线：只在第一次（或余额被充值后变大）时记录。
    # 注意取 max(当前, 今日起点)：基线是后加的，若只取当前余额，
    # 会出现"累计消耗 < 今日消耗"这种自相矛盾的显示。
    base = _kv_get(cfg, "budget_baseline")
    if not isinstance(base, (int, float)) or bal["cny"] > float(base):
        base = max(float(bal["cny"]), float(start))
        _kv_set(cfg, "budget_baseline", base)
        _kv_set(cfg, "budget_baseline_ts", time.strftime("%Y-%m-%d"))

    spent = max(0.0, float(start) - float(bal["cny"]))
    total = max(spent, max(0.0, float(base) - float(bal["cny"])))
    return {"cny": round(spent, 4), "balance": bal["cny"], "start": start,
            "total": round(total, 4), "since": _kv_get(cfg, "budget_baseline_ts") or "",
            "limit": float(conf(cfg).get("daily_cny", 2.0) or 2.0), "error": ""}


# ---------- 记录用量 ----------

def record(cfg: dict[str, Any] | None, *, prompt_tokens: int = 0,
           completion_tokens: int = 0, source: str = "chat", model: str = "",
           ok: bool = True, cache_hit: int | None = None,
           cache_miss: int | None = None) -> None:
    """记一次调用。**记账失败绝不能影响聊天本身。**

    ``cache_hit``/``cache_miss`` 有就存（用来按真实单价估算）；
    没给就按"全部未命中"处理，属于偏保守的估法。
    """
    if not cfg or not enabled(cfg):
        return
    pt = int(prompt_tokens or 0)
    if cache_hit is None and cache_miss is None:
        cache_hit, cache_miss = 0, pt          # 保守：全按未命中算
    else:
        cache_hit = int(cache_hit or 0)
        cache_miss = int(cache_miss if cache_miss is not None else max(0, pt - cache_hit))
    try:
        with _conn(cfg) as c, c:
            c.execute("INSERT INTO llm_usage(ts, source, model, prompt_tokens,"
                      " completion_tokens, cache_hit, cache_miss, ok)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (time.time(), source, model or (cfg.get("llm") or {}).get("model", ""),
                       pt, int(completion_tokens or 0), cache_hit, cache_miss,
                       1 if ok else 0))
    except sqlite3.Error as exc:
        print(f"[budget] 用量记录失败：{exc}")


def usage_today(cfg: dict[str, Any]) -> dict[str, Any]:
    since = time.mktime(time.strptime(_today(), "%Y-%m-%d"))
    try:
        with _conn(cfg) as c:
            row = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(prompt_tokens),0) p,"
                " COALESCE(SUM(completion_tokens),0) o,"
                " COALESCE(SUM(cache_hit),0) h, COALESCE(SUM(cache_miss),0) m"
                " FROM llm_usage WHERE ts>=?", (since,)).fetchone()
        return {"calls": row["n"], "prompt_tokens": row["p"], "completion_tokens": row["o"],
                "cache_hit": row["h"], "cache_miss": row["m"]}
    except sqlite3.Error:
        return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "cache_hit": 0, "cache_miss": 0}


def own_spend_today(cfg: dict[str, Any]) -> dict[str, Any]:
    """**蕴自己**今天花了多少（按 token 估算）。

    和 ``spend_today``（余额差）的区别：余额差会把同一个 key 在**别处**
    的消耗也算进来，用户反馈过这个问题。这个函数只统计我们自己记录的调用。
    """
    u = usage_today(cfg)
    cny = estimate_cny(cfg, cache_hit=u.get("cache_hit", 0),
                       cache_miss=u.get("cache_miss", 0),
                       out=u.get("completion_tokens", 0))
    return {"cny": round(cny, 4), "tokens": u}


# ---------- 额度闸（全局，任何调用路径都要过） ----------

def check_cap(cfg: dict[str, Any] | None) -> tuple[bool, str]:
    """每日额度检查。**放在 LLM 内部调用**，这样学习/测验/定时自学都绕不过去。

    原来这个检查只写在了两个聊天接口上，于是额度用完之后
    `learner.summarize()`（学习总结）和 `learner.quiz()`（测验出题）
    照样在调模型 —— 用户反馈的"到达设定额度后没有进行措施"就是这个。
    """
    if not cfg or not enabled(cfg):
        return True, ""
    limit = float(conf(cfg).get("daily_cny", 2.0) or 0)
    if limit <= 0:
        return True, ""
    spent = own_spend_today(cfg)["cny"]
    if spent >= limit:
        why = f"今日额度已用尽（估算 ¥{spent:.3f} / 上限 ¥{limit:.2f}）"
        _block(why)
        return False, why
    return True, ""


# ---------- 三道闸 ----------

def _repeat_count(cfg: dict[str, Any], text: str, window: float) -> int:
    """这条完全相同的话，在窗口内已经出现几次（只算用户发的）。"""
    body = (text or "").strip()
    if not body:
        return 0
    try:
        with _conn(cfg) as c:
            row = c.execute(
                "SELECT COUNT(*) n FROM messages WHERE role='user' AND content=? AND ts>=?",
                (body, time.time() - window)).fetchone()
        return int(row["n"] or 0)
    except sqlite3.Error:
        return 0


def _calls_last_minute(cfg: dict[str, Any]) -> int:
    try:
        with _conn(cfg) as c:
            row = c.execute("SELECT COUNT(*) n FROM llm_usage WHERE ts>=?",
                            (time.time() - 60,)).fetchone()
        return int(row["n"] or 0)
    except sqlite3.Error:
        return 0


def _block(reason: str) -> None:
    with _lock:
        _blocked.update(count=_blocked["count"] + 1, last=reason, last_ts=time.time())
    print(f"[budget] 拦下一次模型调用：{reason}")


def guard(cfg: dict[str, Any] | None, text: str, channel: str = "local") -> tuple[bool, str]:
    """放行前检查。返回 (是否放行, 拦截原因)。**

    任何一项拿不到数据都**放行**（记警告），因为把用户挡在门外比多花几毛钱更糟。
    """
    if not cfg or not enabled(cfg):
        return True, ""
    b = conf(cfg)

    # 闸 1：重复消息抑制 —— 直接对应那次 14 连发的事故
    window = float(b.get("duplicate_window_sec", 600) or 600)
    dup_max = int(b.get("duplicate_max", 3) or 3)
    n = _repeat_count(cfg, text, window)
    if dup_max > 0 and n >= dup_max:
        why = f"同一句话在 {int(window/60)} 分钟内已出现 {n} 次"
        _block(why)
        return False, (f"你已经连着发了 {n + 1} 遍一模一样的话了喵。"
                       "我不重复烧钱 —— 换个说法，或者告诉我是哪个脚本在循环。")

    # 闸 2：频率限制
    per_min = int(b.get("max_per_minute", 12) or 0)
    if per_min > 0:
        used = _calls_last_minute(cfg)
        if used >= per_min:
            why = f"每分钟调用已达 {used} 次（上限 {per_min}）"
            _block(why)
            return False, (f"一分钟内已经调用 {used} 次模型了喵，先喘口气。"
                           "如果这不是你在操作，检查一下是不是有脚本在循环。")

    # 闸 3：每日额度。这里复用 check_cap（它同时被 LLM 内部调用，
    # 所以学习/测验/定时自学那些路径也拦得住）。
    ok, why = check_cap(cfg)
    if not ok:
        spent = own_spend_today(cfg)["cny"]
        limit = float(b.get("daily_cny", 2.0) or 0)
        return False, (f"今天的额度用完了喵（蕴自己已花约 ¥{spent:.3f}，"
                       f"上限 ¥{limit:.2f}）。要找我就等明天，"
                       "或者去设置里把上限调高。")

    return True, ""


# ---------- 汇总（界面用） ----------

def status(cfg: dict[str, Any], *, refresh: bool = False) -> dict[str, Any]:
    if not enabled(cfg):
        return {"enabled": False}
    # **主指标 = 蕴自己的消耗（按 token 估算）**，不是余额差。
    # 余额差会把同一个 key 在别处的消耗也算进来，用户反馈过这个问题。
    own = own_spend_today(cfg)
    acct = spend_today(cfg, refresh=refresh)      # 账户口径，仅供参考
    b = conf(cfg)
    with _lock:
        blocked = dict(_blocked)
    return {
        "enabled": True,
        "show": bool(conf(cfg).get("show", True)),
        # 主显示用它
        "spent_today": own["cny"],
        "since": acct.get("since") or "",
        "limit": float(b.get("daily_cny", 2.0) or 0),
        "currency": "CNY",
        # 账户口径（含别处消耗），放在详情里说明
        "balance": acct.get("balance"),
        "account_spent_today": acct.get("cny"),
        "account_spent_total": acct.get("total"),
        "error": acct.get("error") or "",
        "today": own["tokens"],
        "limits": {
            "duplicate_max": int(b.get("duplicate_max", 3) or 3),
            "duplicate_window_sec": float(b.get("duplicate_window_sec", 600) or 600),
            "max_per_minute": int(b.get("max_per_minute", 12) or 12),
            "daily_cny": float(b.get("daily_cny", 2.0) or 0),
        },
        "prices": _price(cfg),
        "blocked": blocked,
    }


if __name__ == "__main__":  # 自检：python -m neko.budget（文件名为 meter.py）
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .config import load_config

    cfg = load_config()
    print("预算功能:", "已启用" if enabled(cfg) else "已关闭")
    print("余额:", balance(cfg, force=True))
    print("今日:", spend_today(cfg, refresh=True))
    print("用量:", usage_today(cfg))
    print("放行检查:", guard(cfg, "这是一句正常的话"))
