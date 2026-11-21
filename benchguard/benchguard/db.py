"""SQLite 连接管理与表结构。

单文件数据库；所有写入走 BEGIN IMMEDIATE，配合进程内裁决锁保证
"放行登记与公开数字同时生效"的原子性。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 系统级元信息：当前冻结批次（全系统只允许从同一个冻结批次选件）
CREATE TABLE IF NOT EXISTS meta (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    frozen_batch_id INTEGER,
    k_anonymity     INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 冻结批次：一经冻结不可增删部件
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    frozen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    part_count  INTEGER NOT NULL DEFAULT 0
);

-- 部件：只属于一个冻结批次；名单仅机务角色可查
CREATE TABLE IF NOT EXISTS parts (
    id         INTEGER PRIMARY KEY,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    serial     TEXT NOT NULL,
    model      TEXT NOT NULL,
    UNIQUE(batch_id, serial)
);

-- 用户（演示用本地账号；按钮显隐不替代服务端校验）
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('maintainer','applicant')),
    pwd_salt      TEXT NOT NULL,
    pwd_hash      TEXT NOT NULL
);

-- 会话令牌
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);

-- 发布（一次申请通过后登记一条；公开数字由 inclusion 派生）
CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- 对外公开编码 R-序号
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    applicant_id    INTEGER NOT NULL REFERENCES users(id),
    status          TEXT NOT NULL DEFAULT 'listed'
                    CHECK (status IN ('listed','withdrawn')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at    TEXT
);

-- 入选明细：某发布中某件是否入选、是否失效。撤下的发布行保留，
-- 因此"已撤下的仍参与核对"。
CREATE TABLE IF NOT EXISTS inclusions (
    release_id  INTEGER NOT NULL REFERENCES releases(id),
    part_id     INTEGER NOT NULL REFERENCES parts(id),
    failed      INTEGER NOT NULL CHECK (failed IN (0,1)),
    PRIMARY KEY (release_id, part_id)
);

-- 申请
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY,
    applicant_id    INTEGER NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('pending','processing','approved','rejected')),
    reject_reason   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT,
    UNIQUE(applicant_id, idempotency_key)
);

-- 申请选件（待裁决的"本次申请入选组合"）
CREATE TABLE IF NOT EXISTS application_items (
    application_id INTEGER NOT NULL REFERENCES applications(id),
    part_id        INTEGER NOT NULL REFERENCES parts(id),
    failed         INTEGER NOT NULL CHECK (failed IN (0,1)),
    PRIMARY KEY (application_id, part_id)
);

-- 审计轨迹
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    actor_id   INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL
);
"""


class Database:
    """进程级数据库句柄，持有裁决串行锁。"""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """立即型写事务：同一时刻只允许一个写入者。"""
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @property
    def adjudicating(self) -> threading.RLock:
        return self._lock

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
