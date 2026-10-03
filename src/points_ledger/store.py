"""SQLite 事件存储。

只追加（append-only）：事实一旦入册不可修改，更正只能通过新事件表达。
所有写操作在 *进程锁 + ``BEGIN IMMEDIATE``* 内串行提交，保证并发封账、
并发报送等场景下事件顺序全局确定，投影重放结果唯一。
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .events import Event, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """事件库连接封装。``path=":memory:"`` 时为单连接内存库（测试用）。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- 读 ----------------------------------------------------------------

    def read_events(self, seq_from: int | None = None, seq_to: int | None = None) -> list[Event]:
        """按序号读取事件区间（闭区间），供投影重放与审计复算。"""
        clauses, params = [], []
        if seq_from is not None:
            clauses.append("seq >= ?")
            params.append(seq_from)
        if seq_to is not None:
            clauses.append("seq <= ?")
            params.append(seq_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT seq, type, payload, created_at FROM events{where} ORDER BY seq",
                params,
            ).fetchall()
        return [Event.from_row(tuple(r)) for r in rows]

    def last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()
        return int(row["s"])

    # --- 写 ----------------------------------------------------------------

    @contextmanager
    def write_batch(self) -> Iterator[Callable[[str, dict], Event]]:
        """在一个串行化事务里追加多条事件（全有或全无）。

        用法::

            with store.write_batch() as append:
                append(type1, payload1)
                append(type2, payload2)

        ``BEGIN IMMEDIATE`` 立即获取 RESERVED 写锁，配合进程锁，
        使两个并发封账/入账命令严格排队，事件序号顺序确定。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            created: list[Event] = []
            try:
                def append(event_type: str, payload: dict) -> Event:
                    cur = self._conn.execute(
                        "INSERT INTO events(type, payload, created_at) VALUES(?, ?, ?)",
                        (event_type, _dumps(payload), now_iso()),
                    )
                    seq = int(cur.lastrowid)
                    ts = self._conn.execute(
                        "SELECT created_at FROM events WHERE seq=?", (seq,)
                    ).fetchone()[0]
                    event = Event(seq=seq, type=event_type, payload=payload, created_at=ts)
                    created.append(event)
                    return event

                yield append
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def append(self, event_type: str, payload: dict) -> Event:
        """追加单条事件的便捷方法。"""
        with self.write_batch() as append:
            return append(event_type, payload)


def _dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
