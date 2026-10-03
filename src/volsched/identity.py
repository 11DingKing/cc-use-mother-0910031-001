"""身份、志愿者、监护人关系、场馆、场次、培训资格。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Any

from .clock import Clock
from .db import Database, log_event
from .errors import DomainError

ROLE_OPERATOR = "operator"
ROLE_VOLUNTEER = "volunteer"
ROLE_GUARDIAN = "guardian"
ROLE_VENUE_MANAGER = "venue_manager"
ROLES = {ROLE_OPERATOR, ROLE_VOLUNTEER, ROLE_GUARDIAN, ROLE_VENUE_MANAGER}

# 培训资格在到期前的宽限余量；到期即失效。
MINOR_AGE = 18


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError.validation("INVALID_DATETIME", "时间需为 ISO 8601 格式") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class IdentityService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # ---------- 账号 ----------

    def create_account(self, role: str, display_name: str) -> dict[str, Any]:
        if role not in ROLES:
            raise DomainError.validation("INVALID_ROLE", f"未知角色：{role}")
        if not display_name.strip():
            raise DomainError.validation("INVALID_NAME", "显示名不能为空")
        account_id = new_id("acct")
        with self.db.write() as con:
            con.execute(
                "INSERT INTO accounts (id, role, display_name, created_at) VALUES (?, ?, ?, ?)",
                (account_id, role, display_name.strip(), self.clock.iso()),
            )
            log_event(con, at=self.clock.iso(), actor_id=account_id,
                      action="account.created", details={"role": role})
        return self.get_account(account_id)  # type: ignore[return-value]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,)))

    def require_account(self, account_id: str) -> dict[str, Any]:
        row = self.get_account(account_id)
        if row is None:
            raise DomainError.not_found("ACCOUNT_NOT_FOUND", f"账号不存在：{account_id}")
        return row

    # ---------- 场馆 ----------

    def create_venue(self, name: str, *, actor_id: str | None = None) -> dict[str, Any]:
        if not name.strip():
            raise DomainError.validation("INVALID_NAME", "场馆名不能为空")
        venue_id = new_id("venue")
        with self.db.write() as con:
            con.execute(
                "INSERT INTO venues (id, name, created_at) VALUES (?, ?, ?)",
                (venue_id, name.strip(), self.clock.iso()),
            )
            log_event(con, at=self.clock.iso(), actor_id=actor_id,
                      action="venue.created", details={"venue_id": venue_id, "name": name.strip()})
        return self.get_venue(venue_id)  # type: ignore[return-value]

    def get_venue(self, venue_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM venues WHERE id = ?", (venue_id,)))

    def require_venue(self, venue_id: str) -> dict[str, Any]:
        row = self.get_venue(venue_id)
        if row is None:
            raise DomainError.not_found("VENUE_NOT_FOUND", f"场馆不存在：{venue_id}")
        return row

    def assign_venue_manager(self, account_id: str, venue_id: str) -> None:
        account = self.require_account(account_id)
        if account["role"] != ROLE_VENUE_MANAGER:
            raise DomainError.validation("NOT_VENUE_MANAGER", "该账号不是场馆负责人")
        self.require_venue(venue_id)
        with self.db.write() as con:
            con.execute(
                "INSERT OR IGNORE INTO venue_managers (account_id, venue_id) VALUES (?, ?)",
                (account_id, venue_id),
            )

    def venues_managed_by(self, account_id: str) -> list[str]:
        rows = self.db.query_all(
            "SELECT venue_id FROM venue_managers WHERE account_id = ?", (account_id,)
        )
        return [r["venue_id"] for r in rows]

    # ---------- 志愿者 ----------

    def create_volunteer(
        self,
        name: str,
        birth_date: str,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise DomainError.validation("INVALID_NAME", "志愿者姓名不能为空")
        try:
            bd = date.fromisoformat(birth_date)
        except ValueError as exc:
            raise DomainError.validation("INVALID_DATE", "出生日期需为 YYYY-MM-DD") from exc
        if bd > self.clock.now().date():
            raise DomainError.validation("INVALID_DATE", "出生日期不能晚于今天")
        if account_id is not None:
            account = self.require_account(account_id)
            if account["role"] != ROLE_VOLUNTEER:
                raise DomainError.validation("NOT_VOLUNTEER_ROLE", "绑定账号的角色必须是志愿者")
        volunteer_id = new_id("vol")
        is_minor = int(self._age(bd) < MINOR_AGE)
        with self.db.write() as con:
            con.execute(
                """
                INSERT INTO volunteers (id, account_id, name, birth_date, is_minor, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (volunteer_id, account_id, name.strip(), birth_date, is_minor, self.clock.iso()),
            )
            log_event(con, at=self.clock.iso(), actor_id=account_id,
                      action="volunteer.created", volunteer_id=volunteer_id,
                      details={"is_minor": bool(is_minor)})
        return self.get_volunteer(volunteer_id)  # type: ignore[return-value]

    def _age(self, birth: date) -> int:
        today = self.clock.now().date()
        return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))

    def get_volunteer(self, volunteer_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM volunteers WHERE id = ?", (volunteer_id,)))

    def require_volunteer(self, volunteer_id: str) -> dict[str, Any]:
        row = self.get_volunteer(volunteer_id)
        if row is None:
            raise DomainError.not_found("VOLUNTEER_NOT_FOUND", f"志愿者不存在：{volunteer_id}")
        return row

    # ---------- 监护关系 ----------

    def add_guardianship(
        self, volunteer_id: str, guardian_account_id: str, relation: str
    ) -> dict[str, Any]:
        self.require_volunteer(volunteer_id)
        guardian = self.require_account(guardian_account_id)
        if guardian["role"] != ROLE_GUARDIAN:
            raise DomainError.validation("NOT_GUARDIAN_ROLE", "该账号不是监护人")
        if not relation.strip():
            raise DomainError.validation("INVALID_RELATION", "监护关系不能为空")
        guard_id = new_id("guard")
        with self.db.write() as con:
            con.execute(
                """
                INSERT INTO guardianships (id, volunteer_id, guardian_account_id,
                                           relation, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (volunteer_id, guardian_account_id) DO NOTHING
                """,
                (guard_id, volunteer_id, guardian_account_id, relation.strip(), self.clock.iso()),
            )
        return self.get_guardianship(volunteer_id, guardian_account_id)  # type: ignore[return-value]

    def get_guardianship(self, volunteer_id: str, guardian_account_id: str) -> dict[str, Any] | None:
        return _row(
            self.db.query_one(
                "SELECT * FROM guardianships WHERE volunteer_id = ? AND guardian_account_id = ?",
                (volunteer_id, guardian_account_id),
            )
        )

    def require_guardianship(self, volunteer_id: str, guardian_account_id: str) -> dict[str, Any]:
        row = self.get_guardianship(volunteer_id, guardian_account_id)
        if row is None:
            raise DomainError.forbidden(
                "NOT_GUARDIAN", "该监护人未与此志愿者建立监护关系"
            )
        return row

    def volunteers_for_guardian(self, guardian_account_id: str) -> list[str]:
        rows = self.db.query_all(
            "SELECT volunteer_id FROM guardianships WHERE guardian_account_id = ?",
            (guardian_account_id,),
        )
        return [r["volunteer_id"] for r in rows]

    # ---------- 培训资格 ----------

    def record_training(
        self,
        volunteer_id: str,
        venue_id: str,
        status: str,
        *,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self.require_volunteer(volunteer_id)
        self.require_venue(venue_id)
        if status not in {"pending", "passed", "expired"}:
            raise DomainError.validation("INVALID_TRAINING_STATUS", f"未知培训状态：{status}")
        achieved_at = self.clock.iso() if status == "passed" else None
        with self.db.write() as con:
            con.execute(
                """
                INSERT INTO training_records (volunteer_id, venue_id, status, achieved_at,
                                              expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (volunteer_id, venue_id) DO UPDATE SET
                    status = excluded.status,
                    achieved_at = excluded.achieved_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (volunteer_id, venue_id, status, achieved_at, expires_at, self.clock.iso()),
            )
        return self.get_training(volunteer_id, venue_id)  # type: ignore[return-value]

    def get_training(self, volunteer_id: str, venue_id: str) -> dict[str, Any] | None:
        return _row(
            self.db.query_one(
                "SELECT * FROM training_records WHERE volunteer_id = ? AND venue_id = ?",
                (volunteer_id, venue_id),
            )
        )

    def training_is_valid(self, volunteer_id: str, venue_id: str, at_iso: str) -> dict[str, Any] | None:
        """返回有效的培训记录；未通过或已过期返回 None。"""
        row = self.get_training(volunteer_id, venue_id)
        if row is None or row["status"] != "passed":
            return None
        if row["expires_at"] and row["expires_at"] < at_iso:
            return None
        return dict(row)

    # ---------- 场次 ----------

    def create_session(
        self,
        venue_id: str,
        title: str,
        starts_at: str,
        ends_at: str,
        capacity: int,
        *,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        self.require_venue(venue_id)
        if not title.strip():
            raise DomainError.validation("INVALID_TITLE", "场次标题不能为空")
        if not isinstance(capacity, int) or capacity <= 0:
            raise DomainError.validation("INVALID_CAPACITY", "名额必须是正整数")
        starts_dt = _parse_dt(starts_at)
        ends_dt = _parse_dt(ends_at)
        if ends_dt <= starts_dt:
            raise DomainError.validation("INVALID_TIME_RANGE", "结束时间必须晚于开始时间")
        starts_at = starts_dt.isoformat()
        ends_at = ends_dt.isoformat()
        session_id = new_id("sess")
        with self.db.write() as con:
            con.execute(
                """
                INSERT INTO sessions (id, venue_id, title, starts_at, ends_at,
                                      capacity, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (session_id, venue_id, title.strip(), starts_at, ends_at,
                 capacity, self.clock.iso()),
            )
            log_event(con, at=self.clock.iso(), actor_id=actor_id,
                      action="session.created", session_id=session_id,
                      details={"venue_id": venue_id, "capacity": capacity})
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return _row(self.db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,)))

    def require_session(self, session_id: str) -> dict[str, Any]:
        row = self.get_session(session_id)
        if row is None:
            raise DomainError.not_found("SESSION_NOT_FOUND", f"场次不存在：{session_id}")
        return row
