"""存档：把对话 / 记忆 / 笔记整体快照成文件，随时可以读回来。

## 为什么需要

用户的原话是「关闭重开后会清除记录，修复一下，可以制作为类似游戏存档式的方式」。

实测结论（对照实验，启动 → 聊天 → 关闭 → 重开）：
**关闭重开本身不会丢数据**，7 条消息完整活过了整个循环。真正抹掉数据的是
「全部清除」按钮被触发（日志里连着三次 `已清除 all：{'chat':1,'memory':1,'notes':8}`，
把对话、记忆、笔记一起清空了）。

所以这里做两件事：

1. **任何清除动作之前先自动存档** —— 清错了也能读回来。
2. **像游戏存档一样**：可以手动保存成具名槽位、列出来、随时读取。
   关闭重开时自动存一次，保证再怎么操作都有退路。

## 为什么用"复制行"而不是"替换文件"

`Memory` 每次操作都新开一个连接（见 `memory._conn_ctx`），没有长连接，
所以两种做法都可行。但**复制行**更稳：不需要让运行中的服务停掉、不碰文件锁、
也不会因为 Windows 文件占用而失败。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import resolve_path

#: 存档覆盖的表。**故意不含 kv** —— kv 里是 topic_cursor / last_study_date
#: 这类内部进度，读一个旧存档不该把"学到哪了"倒退回去。
TABLES = ("messages", "memories", "notes")

#: 自动存档保留上限（只对"清除前-时间戳"这种会累积的生效）
KEEP_AUTO = 20
#: 「清除前-时间戳」最多留几份
KEEP_CLEAR = 5


def _signature(cfg: dict[str, Any]) -> str:
    """当前库的内容指纹：各表条数 + 最大 rowid。

    够判断"和上次相比有没有变"，而且极便宜 —— 用来避免**内容没变也重写存档**，
    免得每次无关紧要的重启都刷新一遍 mtime、制造一堆看似"新"的记录。
    """
    try:
        with _conn(_live(cfg), timeout=5.0) as c:
            parts = []
            for t in TABLES:
                try:
                    row = c.execute(
                        f"SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM {t}").fetchone()
                    parts.append(f"{t}:{row[0]}:{row[1]}")
                except sqlite3.Error:
                    parts.append(f"{t}:0:0")
            return "|".join(parts)
    except sqlite3.Error:
        return ""

_NAME_OK = re.compile(r"^[\w\u4e00-\u9fa5\- ]{1,40}$")


def saves_dir(cfg: dict[str, Any] | None = None) -> Path:
    d = Path(((cfg or {}).get("memory") or {}).get("saves_dir") or "data/saves")
    if not d.is_absolute():
        d = resolve_path(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    """把存档名收敛成安全的文件名。挡住路径穿越。"""
    raw = str(name or "").strip()
    raw = raw.replace("/", "_").replace("\\", "_").replace(":", "_")
    raw = re.sub(r"[\x00-\x1f]", "", raw)
    if not raw or not _NAME_OK.match(raw):
        raw = time.strftime("存档-%Y%m%d-%H%M%S")
    return raw[:40]


def _live(cfg: dict[str, Any]) -> Path:
    p = Path((cfg.get("memory") or {}).get("db_path") or "data/neko.db")
    return p if p.is_absolute() else resolve_path(p)


@contextmanager
def _conn(path: Path, timeout: float = 15.0):
    """借出一个连接，**保证关闭**。

    踩过的坑，别再犯：`with sqlite3.connect(p) as c:` **不会关闭连接** ——
    `with` 在 sqlite3 里只管事务（提交/回滚）。早先 `list_saves()` 这么写，
    每列一次存档就泄漏一个**持有文件锁**的连接，后果是存档文件删不掉
    （`PermissionError: [WinError 32] 另一个程序正在使用此文件`）。
    memory.py 里为同一个坑写过注释，这里是第二次踩，所以单独抽成工具。
    """
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = 0
    return out


# ---------- 存 ----------

def snapshot(cfg: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """把当前库快照成一个存档文件。返回存档信息。

    **不用 `sqlite3.backup()`** —— 实测它有两个坑：
    1. 它会继承源库的 `journal_mode`（主库是 WAL），于是存档内容落进
       `<名字>.db-wal` 附属文件；之后重新打开时没恢复，读出来是 0 条。
       存档"看着有 N 条、其实是个空壳"，这比没有存档更危险。
    2. 想救回来就得 checkpoint，而 checkpoint 在 backup 之后不一定生效；
       一旦顺手把 `-wal` 当垃圾删掉，数据就真的没了（这个坑我踩过）。

    所以改成**把行导进一个用 rollback journal 新建的库**：结果就是一个
    干干净净的单文件 `.db`，没有附属文件，读写都不会有惊喜。
    """
    label = _safe_name(name or time.strftime("存档-%Y%m%d-%H%M%S"))
    target = saves_dir(cfg) / f"{label}.db"
    src_path = _live(cfg)
    if not src_path.exists():
        return {"ok": False, "reason": "还没有数据库文件"}

    # **内容没变就不重写。** 否则每次无关紧要的重启都会刷新存档文件的 mtime，
    # 列表上看起来就像"又多了一条"。滚动存档（最近-启动/最近-退出）尤其需要这个。
    sig = _signature(cfg)
    meta_path = saves_dir(cfg) / f"{label}.json"
    if target.exists() and sig:
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))
            if old.get("sig") == sig:
                return {"ok": True, "name": label, "size": target.stat().st_size,
                        "counts": old.get("counts") or {}, "unchanged": True}
        except (OSError, ValueError):
            pass

    if target.exists():
        target.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)

    src = sqlite3.connect(src_path, timeout=15.0)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(target, timeout=15.0)
    counts: dict[str, int] = {}
    try:
        dst.execute("PRAGMA journal_mode=DELETE")
        for table in TABLES:
            row = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not row or not row[0]:
                counts[table] = 0
                continue
            dst.execute(row[0])                      # 用原表结构建表
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            if not cols:
                counts[table] = 0
                continue
            rows = src.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
            if rows:
                dst.executemany(
                    f"INSERT INTO {table}({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [tuple(r) for r in rows],
                )
            counts[table] = len(rows)
        dst.commit()
    finally:
        src.close()
        dst.close()

    (saves_dir(cfg) / f"{label}.json").write_text(
        _meta_json(label, target, counts, sig), encoding="utf-8")
    _prune_auto(cfg)
    return {"ok": True, "name": label, "size": target.stat().st_size, "counts": counts}


def auto_policy(cfg: dict[str, Any]) -> dict[str, bool]:
    """哪些时机自动存档。默认**只留"关闭程序时"**。

    用户要求：取消除关闭程序存档以外的其他自动存档（启动 / 清除前 / 读档前）。
    原因很直接 —— 那些备份堆在列表里很吵，而他真正想要的只是
    "上次关掉之前是什么样"。
    """
    conf = ((cfg or {}).get("memory") or {}).get("auto_save") or {}
    return {
        "on_quit": bool(conf.get("on_quit", True)),
        "on_start": bool(conf.get("on_start", False)),
        "before_clear": bool(conf.get("before_clear", False)),
    }


#: 时机 -> 策略里的开关名
_REASON_FLAG = {"start": "on_start", "quit": "on_quit", "clear": "before_clear"}

#: **"我的进度"这个槽位**。退出时写进它，下次启动自动续接它。
#: 固定名 = 永远只有一条，不会因为反复开关而堆积。
CURRENT = "当前进度"
#: 老版本用过的名字，启动时迁移过来
_LEGACY_CURRENT = ("最近-退出",)


def auto(cfg: dict[str, Any], reason: str = "auto") -> dict[str, Any]:
    """按策略自动存档。**被关掉的时机直接什么都不做。**

    - ``quit``（关闭程序）：写进固定槽 ``当前进度``，滚动覆盖，永不堆积
    - ``start`` / ``clear``：默认关闭（见 ``auto_policy``）
    """
    flag = _REASON_FLAG.get(reason)
    if flag and not auto_policy(cfg).get(flag, False):
        return {"ok": True, "skipped": True, "reason": f"{reason} 时机的自动存档已关闭"}

    if reason == "clear":
        return snapshot(cfg, f"清除前-{time.strftime('%Y%m%d-%H%M%S')}")
    if reason == "quit":
        return snapshot(cfg, CURRENT)
    if reason == "start":
        return snapshot(cfg, "最近-启动")
    return snapshot(cfg, f"自动-{time.strftime('%Y%m%d-%H%M%S')}")


def _msg_count(path: Path) -> int:
    try:
        with _conn(path, timeout=5.0) as c:
            return int(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    except sqlite3.Error:
        return -1


def resume(cfg: dict[str, Any]) -> dict[str, Any]:
    """启动时**续接上次进度**，并且不新增任何存档。

    语义就是游戏存档：关掉再打开，应该在原来那个进度上继续。

    但直接"启动就加载槽位"是危险的 —— 如果上次是**崩溃/被强杀**退出，
    根本没写成退出存档，实时库反而比槽位新；这时候加载槽位会**丢掉新对话**。
    所以两种都看一眼，谁新用谁：

    - 实时库 >= 槽位：保留实时库（上次没存成，实时库才是最新的），顺便把槽位同步过来
    - 实时库 <  槽位：用槽位恢复（库被清空/损坏了，槽位是最后一份好的）

    两种情况都不会丢东西，而且**一次都不会新增存档条目**。
    """
    # 老版本的 "最近-退出" 迁移成固定槽
    for old in _LEGACY_CURRENT:
        op = saves_dir(cfg) / f"{old}.db"
        if op.exists() and not (saves_dir(cfg) / f"{CURRENT}.db").exists():
            try:
                op.rename(saves_dir(cfg) / f"{CURRENT}.db")
                om = saves_dir(cfg) / f"{old}.json"
                if om.exists():
                    om.unlink()
                print(f"[saves] 存档槽改名：{old} -> {CURRENT}")
            except OSError:
                pass

    # 槽位标记指向一个已经不在的存档（改名、手删、老版本遗留）就修正掉，
    # 否则界面上会一直显示一个不存在的"当前"
    act = active_save(cfg)
    if act and act not in {s["name"] for s in list_saves(cfg)}:
        set_active(cfg, CURRENT if (saves_dir(cfg) / f"{CURRENT}.db").exists() else "")
        print(f"[saves] 当前槽位标记已失效（{act}），修正为 {active_save(cfg) or '(空)'}")

    slot = saves_dir(cfg) / f"{CURRENT}.db"
    live = _live(cfg)
    live_n = _msg_count(live) if live.exists() else 0
    slot_n = _msg_count(slot) if slot.exists() else -1

    if slot_n < 0:                      # 还没有槽位，建一个
        out = snapshot(cfg, CURRENT)
        return {"ok": True, "action": "init", "messages": live_n, "name": out.get("name")}

    if live_n >= slot_n:                # 实时库更新（或持平）—— 保留它，同步槽位
        snapshot(cfg, CURRENT)
        return {"ok": True, "action": "keep-live", "messages": live_n, "slot": slot_n}

    # 实时库更旧（被清空/损坏）—— 用槽位恢复
    out = load_save(cfg, CURRENT, backup=False)
    set_active(cfg, CURRENT)
    print(f"[saves] 启动续接：实时库 {live_n} 条 < 槽位 {slot_n} 条，用槽位恢复")
    return {"ok": True, "action": "restore", "messages": slot_n,
            "from": live_n, "name": out.get("name")}


# ---------- 查 ----------

def list_saves(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    active = active_save(cfg)
    out = []
    for p in saves_dir(cfg).glob("*.db"):
        try:
            # 必须用 _conn：`with sqlite3.connect()` 不关连接，会锁住文件删不掉
            with _conn(p, timeout=5.0) as c:
                counts = _counts(c)
            st = p.stat()
            out.append({"name": p.stem, "size": st.st_size,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                        "mtime": st.st_mtime, "counts": counts,
                        "auto": bool(re.match(r"^(自动|清除前|启动|退出|切换前|最近)-", p.stem))
                        or p.stem == CURRENT,
                        "active": p.stem == active})
        except sqlite3.Error:
            continue
    out.sort(key=lambda x: -x["mtime"])
    return out


# ---------- 当前存档槽 ----------

def _active_file(cfg: dict[str, Any]) -> Path:
    return saves_dir(cfg) / "_active.txt"


def active_save(cfg: dict[str, Any]) -> str:
    """当前"正在用的"存档槽名字。空字符串表示当前进度还没归入任何槽位。"""
    f = _active_file(cfg)
    try:
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""
    except OSError:
        return ""


def set_active(cfg: dict[str, Any], name: str) -> None:
    _active_file(cfg).write_text(str(name or ""), encoding="utf-8")


# ---------- 跳转（不覆盖当前进度） ----------

def switch_to(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """**跳转**到某个存档槽。

    和"读取覆盖"的区别：跳转**不销毁当前进度**，而是先把当前进度写回它自己
    所属的槽位，再切过去。所以来回跳不会丢东西 —— 用户的原话是
    「不要覆盖存档，改为跳转存档」。

    当前进度还没归属任何槽位时，会先给它建一个「我的进度」槽位收好。
    """
    target = _safe_name(name)
    path = saves_dir(cfg) / f"{target}.db"
    if not path.is_file():
        return {"ok": False, "reason": f"没有这个存档：{name}"}

    active = active_save(cfg)
    kept: str | None = None
    if active and active != target:
        # 把当前进度写回它自己的槽位（更新，不是新建）
        snapshot(cfg, active)
        kept = active
    elif not active:
        # 当前进度还没归属：给它建一个，别让跳转把这段对话搞丢
        kept = snapshot(cfg, "我的进度")["name"]
        active = kept

    if target == active:
        return {"ok": True, "switched_to": target, "kept": kept,
                "restored": None, "note": "已经在这个存档里了"}

    out = load_save(cfg, target, backup=False)   # 当前进度已存进槽位，不必再另存
    if not out.get("ok"):
        return out
    set_active(cfg, target)
    return {"ok": True, "switched_to": target, "kept": kept,
            "restored": out.get("restored")}


# ---------- 读 ----------

def load_save(cfg: dict[str, Any], name: str, *, backup: bool = True) -> dict[str, Any]:
    """把某个存档读回当前库（覆盖对话/记忆/笔记）。

    ``backup=True`` 时读之前先自动备份现状。**跳转**场景（`switch_to`）会传
    ``backup=False`` —— 因为跳转前已经把当前进度写回它自己的槽位了，
    再存一份自动备份只是徒增垃圾。
    """
    path = saves_dir(cfg) / f"{_safe_name(name)}.db"
    if not path.is_file():
        return {"ok": False, "reason": f"没有这个存档：{name}"}

    before = auto(cfg, "clear") if backup else {"name": None}
    live = _live(cfg)
    restored: dict[str, int] = {}
    with _conn(path) as src, _conn(live) as dst:
        src.row_factory = sqlite3.Row
        with dst:
            for t in TABLES:
                cols = [r[1] for r in dst.execute(f"PRAGMA table_info({t})")]
                if not cols:
                    continue
                have = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
                use = [c for c in cols if c in have]
                if not use:
                    continue
                rows = src.execute(f"SELECT {','.join(use)} FROM {t}").fetchall()
                dst.execute(f"DELETE FROM {t}")
                if rows:
                    dst.executemany(
                        f"INSERT INTO {t}({','.join(use)}) "
                        f"VALUES ({','.join('?' * len(use))})",
                        [tuple(r) for r in rows],
                    )
                restored[t] = len(rows)
    return {"ok": True, "name": name, "restored": restored,
            "backup_before_load": before.get("name")}


def delete_save(cfg: dict[str, Any], name: str) -> bool:
    safe = _safe_name(name)
    d = saves_dir(cfg)
    p = d / f"{safe}.db"
    if not p.is_file():
        return False
    # 删之前先把可能的文件句柄放掉：早期版本 list_saves() 会泄漏连接锁住文件，
    # 表现是 PermissionError: [WinError 32]。留一句提示方便再遇到时定位。
    try:
        p.unlink()
    except PermissionError:
        time.sleep(0.3)
        p.unlink()
    if active_save(cfg) == safe:
        set_active(cfg, "")              # 删掉的正好是当前槽位，清掉标记
    # 附属文件和元数据一起清掉，别留下孤儿
    for extra in (f"{safe}.db-wal", f"{safe}.db-shm", f"{safe}.json"):
        side = d / extra
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass
    return True


# ---------- 内部 ----------

def _prune_auto(cfg: dict[str, Any]) -> None:
    """清理会累积的那种自动存档。

    - 「清除前-时间戳」：只留最近 KEEP_CLEAR 份（这个时机的历史有回溯价值）
    - 「当前进度」：固定名滚动覆盖，永远不会多出来，**绝对不清理**
    - 其它带时间戳的老式自动存档：按总量收敛
    """
    clears = [s for s in list_saves(cfg) if s["name"].startswith("清除前-")]
    for s in clears[KEEP_CLEAR:]:
        delete_save(cfg, s["name"])
    others = [s for s in list_saves(cfg)
              if s["auto"] and s["name"] != CURRENT
              and not s["name"].startswith(("清除前-", "最近-"))]
    for s in others[KEEP_AUTO:]:
        delete_save(cfg, s["name"])


def _meta_json(label: str, target: Path, counts: dict[str, int],
               sig: str = "") -> str:
    import json

    return json.dumps({"name": label, "counts": counts, "sig": sig,
                       "created": time.strftime("%Y-%m-%d %H:%M:%S")},
                      ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":  # 自检：python -m neko.saves
    import io as _io
    import sys

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from .config import load_config

    cfg = load_config()
    print("存档目录:", saves_dir(cfg))
    print("现有存档:")
    for s in list_saves(cfg):
        print(f"  {s['name']:<26} {s['time']}  {s['size']:>8}B  {s['counts']}")
