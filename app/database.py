"""SQLite 持久化层。

设计要点：
- 所有写操作在单连接上串行执行（ThreadingHTTPServer 每个请求一个线程，
  用一把可重入锁保护写事务），数据库开启 WAL 以保证读写并发不互相阻塞。
- 观测按 (station_id, observed_at_utc) 唯一约束去重；
  批次以 batch_id 为主键，保存规范化 JSON 用于重试/冲突判定。
- 事件表保存重算结果，每次入库后按站点整体重写。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    flood_level     REAL NOT NULL,
    recovery_level  REAL NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id       TEXT NOT NULL REFERENCES stations(id),
    observed_at_utc  TEXT NOT NULL,
    level            REAL NOT NULL,
    batch_id         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (station_id, observed_at_utc)
);

CREATE INDEX IF NOT EXISTS idx_obs_station_time
    ON observations (station_id, observed_at_utc);

CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,   -- 规范化后的批次内容（用于内容比对）
    result_json  TEXT NOT NULL,   -- 首次成功入库时返回给客户端的结果
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id    TEXT NOT NULL REFERENCES stations(id),
    start_at      TEXT NOT NULL,
    end_at        TEXT,
    peak_at       TEXT NOT NULL,
    peak_level    REAL NOT NULL,
    sample_count  INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    UNIQUE (station_id, start_at)
);

CREATE INDEX IF NOT EXISTS idx_events_station
    ON events (station_id, start_at);
"""


class Database:
    """SQLite 连接封装。"""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # 显式事务管理
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._lock:
            self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        """写事务串行锁（service 层用它把"校验+写入+重算"包成原子操作）。"""
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务上下文：BEGIN ... COMMIT，异常时 ROLLBACK。"""
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def connect_read(self) -> sqlite3.Connection:
        """读连接：WAL 模式下读不阻塞写，直接使用主连接即可。"""
        return self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()
