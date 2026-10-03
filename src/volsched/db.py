"""SQLite 持久化与事务边界。

所有写操作都在 ``BEGIN IMMEDIATE`` 事务中完成：进程内用锁串行化，
进程间由 SQLite 的保留锁串行化（配合 ``busy_timeout``）。因此“检查容量
→ 占名额 / 递补候补”在任何并发下都是一条可串行化的临界区，重启后
已提交的状态完整保留。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('operator','volunteer','guardian','venue_manager')),
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venues (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venue_managers (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    venue_id TEXT NOT NULL REFERENCES venues(id),
    PRIMARY KEY (account_id, venue_id)
);

CREATE TABLE IF NOT EXISTS volunteers (
    id TEXT PRIMARY KEY,
    account_id TEXT UNIQUE REFERENCES accounts(id),
    name TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    is_minor INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardianships (
    id TEXT PRIMARY KEY,
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    guardian_account_id TEXT NOT NULL REFERENCES accounts(id),
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (volunteer_id, guardian_account_id)
);

CREATE TABLE IF NOT EXISTS consent_versions (
    id TEXT PRIMARY KEY,
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    guardian_account_id TEXT NOT NULL REFERENCES accounts(id),
    version_seq INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    statement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active','superseded','revoked')),
    issued_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    superseded_by_id TEXT,
    UNIQUE (volunteer_id, version_seq)
);
CREATE INDEX IF NOT EXISTS idx_consent_volunteer ON consent_versions(volunteer_id, version_seq);

CREATE TABLE IF NOT EXISTS training_records (
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    venue_id TEXT NOT NULL REFERENCES venues(id),
    status TEXT NOT NULL CHECK (status IN ('pending','passed','expired')),
    achieved_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (volunteer_id, venue_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL REFERENCES venues(id),
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    created_at TEXT NOT NULL,
    CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS idx_session_venue ON sessions(venue_id);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    state TEXT NOT NULL CHECK (state IN ('held','confirmed','cancelled')),
    created_at TEXT NOT NULL,
    held_at TEXT,
    hold_expires_at TEXT,
    confirmed_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    consent_version_id TEXT,
    consent_seq INTEGER,
    consent_scope_json TEXT,
    consent_issued_at TEXT,
    training_venue_id TEXT,
    training_achieved_at TEXT,
    training_expires_at TEXT,
    source TEXT NOT NULL DEFAULT 'direct'
        CHECK (source IN ('direct','waitlist','transfer')),
    waitlist_entry_id TEXT,
    waitlist_seq INTEGER,
    transferred_from_assignment_id TEXT
);
-- 同一有效（未取消）安排在同一场次只能有一条：取消后可重新报名。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_assignment
    ON assignments(session_id, volunteer_id) WHERE state != 'cancelled';
CREATE INDEX IF NOT EXISTS idx_assignment_session ON assignments(session_id, state);
CREATE INDEX IF NOT EXISTS idx_assignment_volunteer ON assignments(volunteer_id);
CREATE INDEX IF NOT EXISTS idx_assignment_holds ON assignments(hold_expires_at) WHERE state = 'held';

CREATE TABLE IF NOT EXISTS waitlist_entries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    volunteer_id TEXT NOT NULL REFERENCES volunteers(id),
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('waiting','promoted','cancelled','expired')),
    enqueued_at TEXT NOT NULL,
    promoted_at TEXT,
    cancelled_at TEXT,
    assignment_id TEXT,
    UNIQUE (session_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_waiting_waitlist
    ON waitlist_entries(session_id, volunteer_id) WHERE status = 'waiting';
CREATE INDEX IF NOT EXISTS idx_waitlist_session ON waitlist_entries(session_id, status, seq);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    assignment_id TEXT,
    session_id TEXT,
    volunteer_id TEXT,
    details_json TEXT NOT NULL
);
"""


class Database:
    """单个 SQLite 连接的线程安全封装。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._con.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._con.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写临界区：BEGIN IMMEDIATE 立即拿库级保留锁。"""
        con = self._con
        with self._lock:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._con.execute(sql, params).fetchone()
    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._con.execute(sql, params).fetchall()


def log_event(
    con: sqlite3.Connection,
    *,
    at: str,
    actor_id: str | None,
    action: str,
    assignment_id: str | None = None,
    session_id: str | None = None,
    volunteer_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO events (at, actor_id, action, assignment_id, session_id,
                            volunteer_id, details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            at,
            actor_id,
            action,
            assignment_id,
            session_id,
            volunteer_id,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
