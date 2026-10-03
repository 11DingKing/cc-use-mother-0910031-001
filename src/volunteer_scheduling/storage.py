"""SQLite 持久化：表结构定义与连接管理。

所有实体持久化在 SQLite 文件中，服务重启后数据不丢失；
过期暂占（holds）以 ``expires_at`` 落库，重启后由服务层继续清理。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS guardians (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volunteers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    guardian_id TEXT NOT NULL REFERENCES guardians(id),
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 监护授权按版本管理：同一时间每名志愿者至多一个 active 版本；
-- 新版本发布时旧版本变为 superseded（不影响已固定它的排班），
-- 显式撤回（withdrawn）才会级联取消固定该版本的排班。
CREATE TABLE IF NOT EXISTS consent_versions (
    id TEXT PRIMARY KEY,
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    version_no INTEGER NOT NULL,
    venue_scope TEXT NOT NULL,
    status TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    UNIQUE (volunteer_id, version_no)
);
CREATE INDEX IF NOT EXISTS consent_active_idx
    ON consent_versions(volunteer_id, status);

CREATE TABLE IF NOT EXISTS trainings (
    id TEXT PRIMARY KEY,
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    qualification TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    required_qualifications TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

-- 暂占：确认前的临时名额占用，带过期时间；状态机 active -> consumed/expired。
CREATE TABLE IF NOT EXISTS holds (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS holds_active_unique
    ON holds(session_id, volunteer_id) WHERE status = 'active';

-- 排班：确认时原子写入，consent_version_id 固定当时的授权版本，
-- snapshot 保存确认瞬间的全部校验依据，供事后解释。
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    consent_version_id TEXT NOT NULL REFERENCES consent_versions(id),
    hold_id TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    cancel_reason TEXT,
    snapshot TEXT NOT NULL,
    transferred_from TEXT,
    created_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS assignments_confirmed_unique
    ON assignments(session_id, volunteer_id) WHERE status = 'confirmed';

-- 候补：seq 在场次内单调递增，递补严格按 seq 顺序。
CREATE TABLE IF NOT EXISTS waitlist_entries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (session_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS waitlist_waiting_unique
    ON waitlist_entries(session_id, volunteer_id) WHERE status = 'waiting';

-- 审计事件：每次状态变更一条，供运营员追溯。
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


class Database:
    """SQLite 连接工厂；每个操作使用独立连接，写事务由服务层显式控制。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
