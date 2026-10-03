"""监护授权版本管理。

授权以**不可变版本**的方式累积：

- 监护人每次重新签发产生 ``seq`` 严格递增的新版本，旧的有效版本
  自动变为 ``superseded``；
- 撤回把当前有效版本标记为 ``revoked`` 并记录时间与原因，不可删除；
- 每位监护人对同一志愿者至多有一个 ``active`` 版本（数据库约束保证）；
- 排班确认时引用的是具体版本 ID，并复制其授权范围作为快照，此后
  授权再被撤回或改范围，都不影响已经确认安排的历史有效性解释。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from .clock import Clock
from .db import Database, log_event
from .errors import DomainError
from .identity import IdentityService, new_id


def normalize_scope(venue_ids: Iterable[str]) -> dict[str, list[str]]:
    ids = sorted({v for v in venue_ids if v})
    if not ids:
        raise DomainError.validation("EMPTY_SCOPE", "授权范围至少包含一个场馆")
    return {"venues": ids}


def scope_from_json(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("venues"), list):
        raise DomainError("CONSENT_SCOPE_CORRUPT", "授权范围数据已损坏", http_status=500)
    return value


def scope_dumps(scope: dict[str, Any]) -> str:
    return json.dumps(scope, ensure_ascii=False, sort_keys=True)


def scope_covers(scope: dict[str, Any], venue_id: str) -> bool:
    return venue_id in set(scope.get("venues", []))


class ConsentService:
    def __init__(self, db: Database, identities: IdentityService, clock: Clock) -> None:
        self.db = db
        self.identities = identities
        self.clock = clock

    def issue(
        self,
        volunteer_id: str,
        guardian_account_id: str,
        venue_ids: list[str],
        *,
        statement: str = "",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """监护人签发新版本授权；旧版本确定地被新版本取代。"""
        self.identities.require_guardianship(volunteer_id, guardian_account_id)
        for venue_id in venue_ids:
            self.identities.require_venue(venue_id)
        scope = normalize_scope(venue_ids)
        version_id = new_id("consent")
        with self.db.write() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(version_seq), 0) AS m FROM consent_versions WHERE volunteer_id = ?",
                (volunteer_id,),
            ).fetchone()
            seq = row["m"] + 1
            con.execute(
                """
                UPDATE consent_versions
                   SET status = 'superseded', superseded_by_id = ?
                 WHERE volunteer_id = ? AND guardian_account_id = ? AND status = 'active'
                """,
                (version_id, volunteer_id, guardian_account_id),
            )
            con.execute(
                """
                INSERT INTO consent_versions
                    (id, volunteer_id, guardian_account_id, version_seq, scope_json,
                     statement, status, issued_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (version_id, volunteer_id, guardian_account_id, seq,
                 scope_dumps(scope), statement, self.clock.iso()),
            )
            log_event(
                con, at=self.clock.iso(), actor_id=actor_id or guardian_account_id,
                action="consent.issued", volunteer_id=volunteer_id,
                details={"consent_version_id": version_id, "seq": seq, "scope": scope},
            )
        return self.get_version(version_id)  # type: ignore[return-value]

    def revoke(
        self,
        volunteer_id: str,
        guardian_account_id: str,
        *,
        reason: str = "",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """撤回当前有效版本。重复撤回得到确定的 CONFLICT 结果。"""
        self.identities.require_guardianship(volunteer_id, guardian_account_id)
        with self.db.write() as con:
            current = self._active_row(con, volunteer_id, guardian_account_id)
            if current is None:
                raise DomainError.conflict(
                    "NO_ACTIVE_CONSENT",
                    "该监护人对此志愿者没有可撤回的有效授权版本",
                )
            con.execute(
                """
                UPDATE consent_versions
                   SET status = 'revoked', revoked_at = ?, revoke_reason = ?
                 WHERE id = ?
                """,
                (self.clock.iso(), reason, current["id"]),
            )
            log_event(
                con, at=self.clock.iso(), actor_id=actor_id or guardian_account_id,
                action="consent.revoked", volunteer_id=volunteer_id,
                details={"consent_version_id": current["id"], "seq": current["version_seq"],
                         "reason": reason},
            )
        return self.get_version(current["id"])  # type: ignore[return-value]

    @staticmethod
    def _active_row(
        con: sqlite3.Connection, volunteer_id: str, guardian_account_id: str
    ) -> sqlite3.Row | None:
        return con.execute(
            """
            SELECT * FROM consent_versions
             WHERE volunteer_id = ? AND guardian_account_id = ? AND status = 'active'
            """,
            (volunteer_id, guardian_account_id),
        ).fetchone()

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM consent_versions WHERE id = ?", (version_id,))
        return self._decorate(row) if row else None

    def require_version(self, version_id: str) -> dict[str, Any]:
        row = self.get_version(version_id)
        if row is None:
            raise DomainError.not_found("CONSENT_NOT_FOUND", f"授权版本不存在：{version_id}")
        return row

    def list_versions(self, volunteer_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM consent_versions WHERE volunteer_id = ? ORDER BY version_seq",
            (volunteer_id,),
        )
        return [self._decorate(r) for r in rows]

    def list_for_guardian(
        self, volunteer_id: str, guardian_account_id: str
    ) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            """
            SELECT * FROM consent_versions
             WHERE volunteer_id = ? AND guardian_account_id = ?
             ORDER BY version_seq
            """,
            (volunteer_id, guardian_account_id),
        )
        return [self._decorate(r) for r in rows]

    @staticmethod
    def _decorate(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["scope"] = scope_from_json(data.pop("scope_json"))
        return data

    # ---------- 事务内查询（供排班临界区复用，保证读到同一快照） ----------

    def covering_consent_for(self, volunteer_id: str, venue_id: str) -> dict[str, Any] | None:
        """只读：任一关联监护人的 active 版本覆盖该场馆即返回。"""
        rows = self.db.query_all(
            """
            SELECT cv.* FROM consent_versions cv
            JOIN guardianships g
              ON g.volunteer_id = cv.volunteer_id
             AND g.guardian_account_id = cv.guardian_account_id
             WHERE cv.volunteer_id = ? AND cv.status = 'active'
             ORDER BY cv.version_seq
            """,
            (volunteer_id,),
        )
        for row in rows:
            if scope_covers(scope_from_json(row["scope_json"]), venue_id):
                return self._decorate(row)
        return None

    @staticmethod
    def find_covering_consent(
        con: sqlite3.Connection, volunteer_id: str, venue_id: str
    ) -> sqlite3.Row | None:
        """任一关联监护人的 active 版本覆盖该场馆即满足。"""
        rows = con.execute(
            """
            SELECT cv.* FROM consent_versions cv
            JOIN guardianships g
              ON g.volunteer_id = cv.volunteer_id
             AND g.guardian_account_id = cv.guardian_account_id
             WHERE cv.volunteer_id = ? AND cv.status = 'active'
             ORDER BY cv.version_seq
            """,
            (volunteer_id,),
        ).fetchall()
        for row in rows:
            if scope_covers(scope_from_json(row["scope_json"]), venue_id):
                return row
        return None
