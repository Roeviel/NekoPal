"""SQLite 记忆库：对话历史 / 长期记忆 / 学习笔记 / 键值状态。

只用标准库 sqlite3，不引入向量库：
- 对话历史直接按时间取最近 N 条；
- 长期记忆和笔记用**中文二元组 + 英文词**做关键词打分检索，
  数据量在几千条以内够用，且零依赖、可离线。

线程安全：每个线程各自拿连接（check_same_thread=False + 短连接），
并用一把锁串行化写操作，面板和微信桥可以同时跑。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .config import load_config, resolve_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    channel TEXT    NOT NULL DEFAULT 'local',
    role    TEXT    NOT NULL,
    content TEXT    NOT NULL,
    meta    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, ts);

CREATE TABLE IF NOT EXISTS memories (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    kind   TEXT NOT NULL DEFAULT 'fact',
    key    TEXT,
    value  TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    UNIQUE(kind, key, value)
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    bvid       TEXT NOT NULL,
    title      TEXT NOT NULL,
    author     TEXT,
    url        TEXT,
    summary    TEXT,
    points     TEXT,
    tags       TEXT,
    duration   INTEGER,
    shared     INTEGER NOT NULL DEFAULT 0,
    -- 下面这几列以前漏了：来源等级是"诚实性"的核心信息（有字幕/观众视角/仅简介/仅标题），
    -- 只在接口返回里存在、没落库，导致界面上的徽章永远不显示、重启就没了。
    topic      TEXT,
    level      TEXT,
    confidence TEXT,
    source     TEXT,
    UNIQUE(bvid)
);
CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes(ts);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 设备动作审计：谁在什么时候对哪个设备做了什么、成功没有。
-- 物理世界的操作没有"读档"，出问题时这张表是唯一能查的东西。
CREATE TABLE IF NOT EXISTS actions (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL,
    device TEXT,
    name   TEXT,
    action TEXT,
    ok     INTEGER,
    detail TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);
"""

# 常见中文停用词（避免"的了吗呢"这类字把检索带偏）
_STOP = set("的了是我你他她它们这那有和与就都而及或在吗呢吧啊呀哦嘛么什么怎么一个人不也还")


def _tokens(text: str) -> list[str]:
    """中文二元组 + 英文/数字词。"""
    text = (text or "").lower()
    out: list[str] = []
    for word in re.findall(r"[a-z0-9_+#]{2,}", text):
        out.append(word)
    han = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in han:
        chars = [c for c in run if c not in _STOP]
        if len(chars) == 1:
            out.append(chars[0])
        for i in range(len(chars) - 1):
            out.append(chars[i] + chars[i + 1])
    return out


class Memory:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        cfg = cfg or load_config()
        self.cfg = cfg
        db_path = cfg.get("memory", {}).get("db_path", "data/neko.db")
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = resolve_path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.recent_n = int(cfg.get("memory", {}).get("recent_messages", 20))
        self._lock = threading.RLock()
        self._init()

    # ---------- 基础设施 ----------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _conn_ctx(self):
        """借出一个连接，**保证关闭**。

        注意：sqlite3 的 `with conn` 只管事务（提交/回滚），**不会关闭连接**。
        早先写成 `with self._conn() as conn` 导致每次读写都泄漏一个连接，
        在 Windows 上表现为数据库文件一直被占用、删不掉。
        所以这里用 `with conn` 管事务 + `finally` 管关闭，两件事都要做。
        """
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init(self) -> None:
        with self._lock, self._conn_ctx() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """给老库补列（CREATE TABLE IF NOT EXISTS 不会改动已存在的表）。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)")}
        for name in ("topic", "level", "confidence", "source"):
            if name not in cols:
                conn.execute(f"ALTER TABLE notes ADD COLUMN {name} TEXT")
                print(f"[memory] notes 表补上缺失的列：{name}")

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock, self._conn_ctx() as conn:
            return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]

    def _write(self, sql: str, args: Iterable[Any] = ()) -> None:
        with self._lock, self._conn_ctx() as conn:
            conn.execute(sql, tuple(args))

    # ---------- 对话历史 ----------

    def add_message(
        self, role: str, content: str, channel: str = "local", meta: dict | None = None
    ) -> int:
        """写一条消息，**返回它的 id**。

        返回 id 是为了让前端能"按条删除"：流式回复刚生成时前端还不知道这条
        在库里是几号，后端把 id 带回去，前端就能把它挂到 DOM 上。
        """
        with self._lock, self._conn_ctx() as conn:
            cur = conn.execute(
                "INSERT INTO messages(ts, channel, role, content, meta) VALUES(?,?,?,?,?)",
                (time.time(), channel, role, content,
                 json.dumps(meta, ensure_ascii=False) if meta else None),
            )
            return int(cur.lastrowid or 0)

    def delete_messages(self, ids: Iterable[int]) -> int:
        """按 id 删消息（支持一条或多条）。返回实际删掉的条数。"""
        clean = [int(i) for i in ids if str(i).strip().lstrip("-").isdigit()]
        if not clean:
            return 0
        with self._lock, self._conn_ctx() as conn:
            marks = ",".join("?" * len(clean))
            n = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE id IN ({marks})", clean
            ).fetchone()[0]
            conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", clean)
        return int(n)

    # ---------- 设备动作审计 ----------

    def add_action(self, device: str, name: str, action: str, ok: bool,
                   detail: str = "", source: str = "chat") -> None:
        self._write(
            "INSERT INTO actions(ts, device, name, action, ok, detail, source) "
            "VALUES(?,?,?,?,?,?,?)",
            (time.time(), device, name, action, 1 if ok else 0, detail, source),
        )

    def recent_actions(self, n: int = 50) -> list[dict]:
        return self._rows(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (n,)
        )

    def recent(self, n: int | None = None, channel: str | None = None) -> list[dict]:
        """最近 n 条，按时间正序返回 [{role, content, ts, channel}]。"""
        n = n or self.recent_n
        if channel:
            rows = self._rows(
                "SELECT * FROM messages WHERE channel=? ORDER BY id DESC LIMIT ?", (channel, n)
            )
        else:
            rows = self._rows("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (n,))
        rows.reverse()
        return rows

    def history_for_llm(self, n: int | None = None) -> list[dict[str, str]]:
        """转成 OpenAI messages 格式（只保留 role/content）。"""
        return [
            {"role": r["role"], "content": r["content"]}
            for r in self.recent(n)
            if r.get("role") in ("user", "assistant") and r.get("content")
        ]

    def clear_history(self) -> int:
        with self._lock, self._conn_ctx() as conn:
            n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.execute("DELETE FROM messages")
        return int(n)

    def clear_notes(self) -> int:
        """删掉全部学习笔记。删干净比"留一半"更符合用户点这个按钮的预期。"""
        with self._lock, self._conn_ctx() as conn:
            n = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            conn.execute("DELETE FROM notes")
        return int(n)

    def delete_note(self, bvid: str) -> bool:
        """删掉单条笔记。返回是否真的删到了（bvid 不存在返回 False）。

        只删笔记，不动对话记录 —— 用户想清掉某条学歪了的笔记时，
        不应该顺带把他和她的聊天也删了。
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return False
        with self._lock, self._conn_ctx() as conn:
            cur = conn.execute("DELETE FROM notes WHERE bvid=?", (bvid,))
            return cur.rowcount > 0

    def clear_memories(self) -> int:
        """清空长期记忆（区别于 forget 的单条删除）。"""
        with self._lock, self._conn_ctx() as conn:
            n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.execute("DELETE FROM memories")
        return int(n)

    def count_messages(self) -> int:
        rows = self._rows("SELECT COUNT(*) AS c FROM messages")
        return int(rows[0]["c"]) if rows else 0

    # ---------- 长期记忆 ----------

    def remember(self, value: str, kind: str = "fact", key: str = "", weight: float = 1.0) -> None:
        value = (value or "").strip()
        if not value:
            return
        with self._lock, self._conn_ctx() as conn:
            conn.execute(
                "INSERT INTO memories(ts, kind, key, value, weight) VALUES(?,?,?,?,?) "
                "ON CONFLICT(kind, key, value) DO UPDATE SET ts=excluded.ts, weight=excluded.weight",
                (time.time(), kind, key or "", value, weight),
            )

    def forget(self, value: str) -> None:
        self._write("DELETE FROM memories WHERE value=?", (value,))

    def all_memories(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self._rows("SELECT * FROM memories WHERE kind=? ORDER BY ts DESC", (kind,))
        return self._rows("SELECT * FROM memories ORDER BY ts DESC")

    def search_memories(self, query: str, k: int = 8) -> list[dict]:
        """关键词打分检索长期记忆。"""
        rows = self.all_memories()
        if not rows:
            return []
        want = set(_tokens(query))
        if not want:
            return rows[:k]
        scored: list[tuple[float, dict]] = []
        for r in rows:
            hay = set(_tokens(f"{r.get('key') or ''} {r.get('value') or ''}"))
            if not hay:
                continue
            hit = len(want & hay)
            if hit:
                score = hit / (len(want) ** 0.5) * float(r.get("weight") or 1.0)
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -float(x[1].get("ts") or 0)))
        return [r for _, r in scored[:k]]

    # ---------- 学习笔记 ----------

    def add_note(
        self,
        bvid: str,
        title: str,
        *,
        author: str = "",
        url: str = "",
        summary: str = "",
        points: list[str] | None = None,
        tags: list[str] | None = None,
        duration: int = 0,
        topic: str = "",
        level: str = "",
        confidence: str = "",
        source: str = "",
    ) -> dict:
        points = points or []
        tags = tags or []
        with self._lock, self._conn_ctx() as conn:
            conn.execute(
                "INSERT INTO notes(ts,bvid,title,author,url,summary,points,tags,duration,shared,"
                "topic,level,confidence,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?) "
                "ON CONFLICT(bvid) DO UPDATE SET ts=excluded.ts, title=excluded.title, "
                "author=excluded.author, url=excluded.url, summary=excluded.summary, "
                "points=excluded.points, tags=excluded.tags, duration=excluded.duration, "
                "topic=excluded.topic, level=excluded.level, "
                "confidence=excluded.confidence, source=excluded.source",
                (
                    time.time(),
                    bvid,
                    title,
                    author,
                    url,
                    summary,
                    json.dumps(points, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    duration,
                    topic,
                    level,
                    confidence,
                    source,
                ),
            )
        return self.get_note(bvid) or {}

    def _note_row(self, row: dict) -> dict:
        out = dict(row)
        for field in ("points", "tags"):
            raw = out.get(field)
            try:
                out[field] = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                out[field] = []
        return out

    def get_note(self, bvid: str) -> dict | None:
        rows = self._rows("SELECT * FROM notes WHERE bvid=?", (bvid,))
        return self._note_row(rows[0]) if rows else None

    def notes(self, limit: int = 50) -> list[dict]:
        return [self._note_row(r) for r in self._rows(
            "SELECT * FROM notes ORDER BY ts DESC LIMIT ?", (limit,)
        )]

    def unshared_notes(self) -> list[dict]:
        return [self._note_row(r) for r in self._rows(
            "SELECT * FROM notes WHERE shared=0 ORDER BY ts ASC"
        )]

    def mark_shared(self, bvid: str) -> None:
        self._write("UPDATE notes SET shared=1 WHERE bvid=?", (bvid,))

    def studied_bvids(self) -> set[str]:
        return {r["bvid"] for r in self._rows("SELECT bvid FROM notes")}

    def search_notes(self, query: str, k: int = 5) -> list[dict]:
        rows = self.notes(limit=200)
        want = set(_tokens(query))
        if not want:
            return rows[:k]
        scored: list[tuple[float, dict]] = []
        for r in rows:
            blob = " ".join(
                [
                    str(r.get("title") or ""),
                    str(r.get("summary") or ""),
                    " ".join(r.get("points") or []),
                    " ".join(r.get("tags") or []),
                ]
            )
            hay = set(_tokens(blob))
            hit = len(want & hay)
            if hit:
                scored.append((hit / (len(want) ** 0.5), r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:k]]

    # ---------- 键值状态 ----------

    def set_kv(self, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    # ---------- 统计 ----------

    def stats(self) -> dict:
        return {
            "messages": self.count_messages(),
            "memories": len(self.all_memories()),
            "notes": len(self.notes(limit=100000)),
            "db": str(self.db_path),
        }


if __name__ == "__main__":  # 自检：python -m neko.memory
    m = Memory()
    print("数据库:", m.db_path)
    m.remember("主人喜欢用 Python", kind="preference", key="喜欢的语言")
    m.remember("主人是上班族，晚上十点后比较闲", kind="fact", key="作息")
    m.add_message("user", "你好呀", channel="selftest")
    m.add_message("assistant", "喵～主人好！", channel="selftest")
    print("统计:", json.dumps(m.stats(), ensure_ascii=False))
    print("检索 'Python':", m.search_memories("Python 语言"))
    print("最近对话:", m.history_for_llm(5))
