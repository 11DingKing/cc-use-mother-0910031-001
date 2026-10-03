"""排班核心：名额暂占、确认固化、候补 FIFO 递补、跨馆调班、过期清理。

并发模型
========
所有改变名额的动作都在单个 ``BEGIN IMMEDIATE`` 事务里完成“读计数 →
判定 → 写入”，进程内由 ``Database`` 的锁串行化、进程间由 SQLite 保留锁
串行化。因此容量超限、重复确认、并发候补递补在任何调度顺序下都得到
确定结果：一个名额永远只可能被一个递补者拿到。

授权固化
========
``held`` 暂占只锁名额、不锁授权；``confirmed`` 确认时重新校验监护授权
与培训资格，并把**当时的授权版本 ID / 序号 / 范围 / 签发时间与培训
记录**复制进安排行。之后监护人撤回或改版授权，只影响尚未确认的安排，
已确认安排仍可用快照逐字段解释其有效性。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .clock import Clock
from .consents import ConsentService, scope_covers, scope_from_json
from .db import Database, log_event
from .errors import DomainError
from .identity import IdentityService, new_id

DEFAULT_HOLD_SECONDS = 120
MAX_HOLD_SECONDS = 3600

STATE_HELD = "held"
STATE_CONFIRMED = "confirmed"
STATE_CANCELLED = "cancelled"


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SchedulingService:
    def __init__(
        self,
        db: Database,
        identities: IdentityService,
        consents: ConsentService,
        clock: Clock,
        default_hold_seconds: int = DEFAULT_HOLD_SECONDS,
    ) -> None:
        self.db = db
        self.identities = identities
        self.consents = consents
        self.clock = clock
        self.default_hold_seconds = default_hold_seconds

    # ==================================================================
    # 请求名额：有余量则暂占，满员则按 seq 入候补队列
    # ==================================================================

    def request_slot(
        self,
        session_id: str,
        volunteer_id: str,
        *,
        hold_seconds: int | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.identities.require_session(session_id)
        volunteer = self.identities.require_volunteer(volunteer_id)
        hold_seconds = self._check_hold_seconds(hold_seconds)
        now = self.clock.now()
        now_iso = now.isoformat()
        if session["status"] != "open":
            raise DomainError.conflict("SESSION_CLOSED", "该场次已关闭报名")
        # 先在独立已提交事务中释放过期暂占并递补，再进入本次报名临界区。
        self._expire_and_promote(session)
        with self.db.write() as con:
            session = con.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session["status"] != "open":
                raise DomainError.conflict("SESSION_CLOSED", "该场次已关闭报名")
            existing = con.execute(
                "SELECT * FROM assignments WHERE session_id = ? AND volunteer_id = ? "
                "AND state != 'cancelled'",
                (session_id, volunteer_id),
            ).fetchone()
            if existing is not None:
                raise DomainError.conflict(
                    "ALREADY_ASSIGNED",
                    f"志愿者在该场次已有{_state_label(existing['state'])}安排",
                    {"assignment_id": existing["id"], "state": existing["state"]},
                )
            waiting_ahead = con.execute(
                "SELECT COUNT(*) AS c FROM waitlist_entries "
                "WHERE session_id = ? AND status = 'waiting'",
                (session_id,),
            ).fetchone()["c"]
            waiting = con.execute(
                "SELECT * FROM waitlist_entries WHERE session_id = ? AND volunteer_id = ? "
                "AND status = 'waiting'",
                (session_id, volunteer_id),
            ).fetchone()
            if waiting is not None:
                position = self._waitlist_position(con, session_id, waiting["seq"])
                raise DomainError.conflict(
                    "ALREADY_WAITLISTED",
                    "志愿者已在该场次候补队列中",
                    {"waitlist_entry_id": waiting["id"], "seq": waiting["seq"],
                     "position": position},
                )
            self._check_session_time_locked(con, volunteer_id, session, exclude_assignment=None)

            if waiting_ahead == 0 and self._free_capacity_locked(con, session) > 0:
                # 直接占名额需要此刻资格齐备且时间不冲突；候补入队不占名额，
                # 允许先排队，递补时再统一校验（队头不满足会确定性阻塞后续）。
                self._check_eligibility_locked(con, volunteer, session["venue_id"], now_iso)
                assignment_id = new_id("asg")
                expires = (now + timedelta(seconds=hold_seconds)).isoformat()
                con.execute(
                    """
                    INSERT INTO assignments (id, session_id, volunteer_id, state,
                                             created_at, held_at, hold_expires_at,
                                             source)
                    VALUES (?, ?, ?, 'held', ?, ?, ?, 'direct')
                    """,
                    (assignment_id, session_id, volunteer_id, now_iso, now_iso, expires),
                )
                log_event(con, at=now_iso, actor_id=actor_id, action="assignment.held",
                          assignment_id=assignment_id, session_id=session_id,
                          volunteer_id=volunteer_id,
                          details={"hold_expires_at": expires})
                result = self.get_assignment(assignment_id)
                result["outcome"] = "held"
                return result  # type: ignore[return-value]

            entry_id = new_id("wl")
            seq_row = con.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM waitlist_entries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = seq_row["m"] + 1
            con.execute(
                """
                INSERT INTO waitlist_entries (id, session_id, volunteer_id, seq, status,
                                              enqueued_at)
                VALUES (?, ?, ?, ?, 'waiting', ?)
                """,
                (entry_id, session_id, volunteer_id, seq, now_iso),
            )
            log_event(con, at=now_iso, actor_id=actor_id, action="waitlist.enqueued",
                      session_id=session_id, volunteer_id=volunteer_id,
                      details={"waitlist_entry_id": entry_id, "seq": seq})
            entry = self.get_waitlist_entry(entry_id)
            entry["outcome"] = "waitlisted"
            entry["position"] = self._waitlist_position(con, session_id, seq)
            return entry  # type: ignore[return-value]

    # ==================================================================
    # 确认：重新校验并固定当时的授权版本与培训记录
    # ==================================================================

    def confirm_assignment(
        self, assignment_id: str, *, actor_id: str | None = None
    ) -> dict[str, Any]:
        assignment = self._require_assignment_row(assignment_id)
        session = self.identities.require_session(assignment["session_id"])
        volunteer = self.identities.require_volunteer(assignment["volunteer_id"])
        now_iso = self.clock.iso()
        # 先独立提交地清理该场次过期暂占并递补；若本暂占恰好已到期，
        # 下方会读到 cancelled 并给出确定结果。
        self._expire_and_promote(session)
        with self.db.write() as con:
            session = con.execute(
                "SELECT * FROM sessions WHERE id = ?", (session["id"],)
            ).fetchone()
            current = con.execute(
                "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
            if current["state"] == STATE_CONFIRMED:
                raise DomainError.conflict(
                    "ALREADY_CONFIRMED",
                    "安排已确认，重复确认不会产生第二个名额",
                    {"assignment_id": assignment_id,
                     "confirmed_at": current["confirmed_at"],
                     "consent_version_id": current["consent_version_id"]},
                )
            if current["state"] == STATE_CANCELLED:
                raise DomainError.conflict(
                    "ASSIGNMENT_CANCELLED",
                    f"安排已取消（{current['cancel_reason'] or '未说明原因'}），请重新报名",
                    {"assignment_id": assignment_id},
                )
            if session["status"] != "open":
                raise DomainError.conflict("SESSION_CLOSED", "该场次已关闭，无法确认")
            # held 的名额自暂占时起一直计入容量，确认不改变计数，无需再判容量。
            training, consent = self._check_eligibility_locked(
                con, volunteer, session["venue_id"], now_iso
            )
            self._check_session_time_locked(
                con, volunteer["id"], session, exclude_assignment=assignment_id
            )
            self._apply_confirmation_snapshot_locked(
                con, current, session, training, consent, now_iso
            )
            log_event(con, at=now_iso, actor_id=actor_id, action="assignment.confirmed",
                      assignment_id=assignment_id, session_id=session["id"],
                      volunteer_id=volunteer["id"],
                      details={"consent_version_id": consent["id"] if consent else None,
                               "source": current["source"],
                               "waitlist_seq": current["waitlist_seq"]})
        return self.get_assignment(assignment_id)  # type: ignore[return-value]

    def _apply_confirmation_snapshot_locked(
        self,
        con: sqlite3.Connection,
        assignment: sqlite3.Row,
        session: sqlite3.Row,
        training: sqlite3.Row | None,
        consent: sqlite3.Row | None,
        now_iso: str,
    ) -> None:
        con.execute(
            """
            UPDATE assignments SET
                state = 'confirmed',
                confirmed_at = ?,
                consent_version_id = ?, consent_seq = ?, consent_scope_json = ?,
                consent_issued_at = ?,
                training_venue_id = ?, training_achieved_at = ?, training_expires_at = ?
              WHERE id = ?
            """,
            (
                now_iso,
                consent["id"] if consent else None,
                consent["version_seq"] if consent else None,
                consent["scope_json"] if consent else None,
                consent["issued_at"] if consent else None,
                training["venue_id"] if training else None,
                training["achieved_at"] if training else None,
                training["expires_at"] if training else None,
                assignment["id"],
            ),
        )
        assert session is not None  # 场馆随场次固定，留作解释快照

    # ==================================================================
    # 取消 / 放弃：释放名额并立即在同一事务内递补
    # ==================================================================

    def cancel_assignment(
        self,
        assignment_id: str,
        *,
        reason: str = "",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        assignment = self._require_assignment_row(assignment_id)
        now_iso = self.clock.iso()
        session = self.identities.require_session(assignment["session_id"])
        self._expire_and_promote(session)
        promotions: list[dict[str, Any]] = []
        with self.db.write() as con:
            current = con.execute(
                "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
            if current["state"] == STATE_CANCELLED:
                raise DomainError.conflict(
                    "ASSIGNMENT_CANCELLED", "安排已经处于取消状态",
                    {"assignment_id": assignment_id},
                )
            self._cancel_row_locked(con, current, reason or "volunteer_cancelled", now_iso)
            self._sweep_session_locked(con, session["id"], now_iso)
            log_event(con, at=now_iso, actor_id=actor_id, action="assignment.cancelled",
                      assignment_id=assignment_id, session_id=current["session_id"],
                      volunteer_id=current["volunteer_id"],
                      details={"reason": reason or "volunteer_cancelled",
                               "previous_state": current["state"]})
            outcome = self._promote_locked(con, session, now_iso)
            promotions = outcome["promotions"]
            for p in promotions:
                self._log_promotion_locked(con, p, now_iso, actor_id)
        result = self.get_assignment(assignment_id)
        result["promotions"] = [
            {"waitlist_seq": p["seq"], "volunteer_id": p["volunteer_id"],
             "assignment_id": p["assignment_id"]}
            for p in promotions
        ]
        return result  # type: ignore[return-value]

    def cancel_waitlist_entry(
        self, entry_id: str, *, actor_id: str | None = None
    ) -> dict[str, Any]:
        entry = self._require_waitlist_row(entry_id)
        now_iso = self.clock.iso()
        with self.db.write() as con:
            current = con.execute(
                "SELECT * FROM waitlist_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if current["status"] != "waiting":
                raise DomainError.conflict(
                    "WAITLIST_NOT_WAITING",
                    f"候补记录已处于 {current['status']} 状态，不能取消",
                    {"waitlist_entry_id": entry_id, "status": current["status"]},
                )
            con.execute(
                "UPDATE waitlist_entries SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                (now_iso, entry_id),
            )
            log_event(con, at=now_iso, actor_id=actor_id, action="waitlist.cancelled",
                      session_id=current["session_id"], volunteer_id=current["volunteer_id"],
                      details={"waitlist_entry_id": entry_id, "seq": current["seq"]})
        return self.get_waitlist_entry(entry_id)  # type: ignore[return-value]

    # ==================================================================
    # 跨馆调班：原子地释放原场次、占用目标场次并重新固化授权
    # ==================================================================

    def transfer(
        self,
        assignment_id: str,
        target_session_id: str,
        *,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        assignment = self._require_assignment_row(assignment_id)
        target_session = self.identities.require_session(target_session_id)
        volunteer = self.identities.require_volunteer(assignment["volunteer_id"])
        now_iso = self.clock.iso()
        if assignment["state"] != STATE_CONFIRMED:
            raise DomainError.conflict(
                "TRANSFER_REQUIRES_CONFIRMED",
                "只有已确认的安排可以跨馆调班；暂占请先取消再重新报名",
                {"assignment_id": assignment_id, "state": assignment["state"]},
            )
        if assignment["session_id"] == target_session_id:
            raise DomainError.validation(
                "SAME_SESSION_TRANSFER", "目标场次与原场次相同，无需调班"
            )
        if target_session["status"] != "open":
            raise DomainError.conflict("SESSION_CLOSED", "目标场次已关闭")
        # 目标场次可能有到期暂占占着名额：先独立提交地清理并递补。
        self._expire_and_promote(target_session)
        promotions: list[dict[str, Any]] = []
        with self.db.write() as con:
            source = con.execute(
                "SELECT * FROM sessions WHERE id = ?", (assignment["session_id"],)
            ).fetchone()
            target_session = con.execute(
                "SELECT * FROM sessions WHERE id = ?", (target_session_id,)
            ).fetchone()
            if target_session["status"] != "open":
                raise DomainError.conflict("SESSION_CLOSED", "目标场次已关闭")
            dup = con.execute(
                "SELECT * FROM assignments WHERE session_id = ? AND volunteer_id = ? "
                "AND state != 'cancelled'",
                (target_session_id, volunteer["id"]),
            ).fetchone()
            if dup is not None:
                raise DomainError.conflict(
                    "TARGET_ALREADY_ASSIGNED",
                    "志愿者在目标场次已有有效安排",
                    {"assignment_id": dup["id"], "state": dup["state"]},
                )
            waiting_here = con.execute(
                "SELECT id FROM waitlist_entries WHERE session_id = ? AND volunteer_id = ? "
                "AND status = 'waiting'",
                (target_session_id, volunteer["id"]),
            ).fetchone()
            if waiting_here is not None:
                raise DomainError.conflict(
                    "TARGET_ALREADY_WAITLISTED",
                    "志愿者在目标场次的候补队列中，请先退出候补再调班",
                    {"waitlist_entry_id": waiting_here["id"]},
                )
            if self._free_capacity_locked(con, target_session) <= 0:
                raise DomainError.conflict(
                    "TARGET_FULL",
                    "目标场次名额已满，调班被拒绝（原安排保持有效）",
                    {"session_id": target_session_id},
                )
            self._check_session_time_locked(
                con, volunteer["id"], target_session, exclude_assignment=assignment_id
            )
            training, consent = self._check_eligibility_locked(
                con, volunteer, target_session["venue_id"], now_iso
            )
            new_id_ = new_id("asg")
            con.execute(
                """
                INSERT INTO assignments (id, session_id, volunteer_id, state,
                                         created_at, confirmed_at,
                                         consent_version_id, consent_seq,
                                         consent_scope_json, consent_issued_at,
                                         training_venue_id, training_achieved_at,
                                         training_expires_at, source,
                                         waitlist_entry_id, waitlist_seq,
                                         transferred_from_assignment_id)
                VALUES (?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'transfer',
                        ?, ?, ?)
                """,
                (new_id_, target_session_id, volunteer["id"], now_iso, now_iso,
                 consent["id"] if consent else None,
                 consent["version_seq"] if consent else None,
                 consent["scope_json"] if consent else None,
                 consent["issued_at"] if consent else None,
                 training["venue_id"] if training else None,
                 training["achieved_at"] if training else None,
                 training["expires_at"] if training else None,
                 assignment["waitlist_entry_id"], assignment["waitlist_seq"],
                 assignment_id),
            )
            con.execute(
                "UPDATE assignments SET state = 'cancelled', cancelled_at = ?, "
                "cancel_reason = ? WHERE id = ?",
                (now_iso, f"transferred_to:{target_session_id}", assignment_id),
            )
            log_event(con, at=now_iso, actor_id=actor_id, action="assignment.transferred",
                      assignment_id=new_id_, session_id=target_session_id,
                      volunteer_id=volunteer["id"],
                      details={"from_assignment_id": assignment_id,
                               "from_session_id": source["id"],
                               "consent_version_id": consent["id"] if consent else None})
            self._sweep_session_locked(con, source["id"], now_iso)
            outcome = self._promote_locked(con, source, now_iso)
            promotions = outcome["promotions"]
            for p in promotions:
                self._log_promotion_locked(con, p, now_iso, actor_id)
        result = self.get_assignment(new_id_)
        result["transferred_from_assignment_id"] = assignment_id
        result["promotions"] = [
            {"waitlist_seq": p["seq"], "volunteer_id": p["volunteer_id"],
             "assignment_id": p["assignment_id"]}
            for p in promotions
        ]
        return result  # type: ignore[return-value]

    # ==================================================================
    # 过期暂占清理：启动时与后台周期任务共用；重启后可继续清理
    # ==================================================================

    def sweep_expired_holds(self, *, actor_id: str | None = None) -> dict[str, Any]:
        now_iso = self.clock.iso()
        swept: list[dict[str, Any]] = []
        promotions: list[dict[str, Any]] = []
        with self.db.write() as con:
            expired = con.execute(
                "SELECT * FROM assignments WHERE state = 'held' AND hold_expires_at <= ?",
                (now_iso,),
            ).fetchall()
            affected_session_ids: list[str] = []
            for row in expired:
                self._cancel_row_locked(con, row, "hold_expired", now_iso)
                swept.append({"assignment_id": row["id"],
                              "session_id": row["session_id"],
                              "volunteer_id": row["volunteer_id"]})
                if row["session_id"] not in affected_session_ids:
                    affected_session_ids.append(row["session_id"])
            for session_id in affected_session_ids:
                session = con.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                outcome = self._promote_locked(con, session, now_iso)
                promotions.extend(outcome["promotions"])
                for p in outcome["promotions"]:
                    self._log_promotion_locked(con, p, now_iso, actor_id)
            if swept:
                log_event(con, at=now_iso, actor_id=actor_id, action="holds.swept",
                          details={"swept_count": len(swept),
                                   "promoted_count": len(promotions)})
        return {
            "swept_at": now_iso,
            "swept": swept,
            "promotions": [
                {"session_id": p["session_id"], "waitlist_seq": p["seq"],
                 "volunteer_id": p["volunteer_id"], "assignment_id": p["assignment_id"]}
                for p in promotions
            ],
        }

    def close_session(self, session_id: str, *, actor_id: str | None = None) -> dict[str, Any]:
        """关闭报名：取消全部暂占与候补（确认安排保留），此后不再递补。"""
        session = self.identities.require_session(session_id)
        now_iso = self.clock.iso()
        with self.db.write() as con:
            con.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (session_id,))
            held = con.execute(
                "SELECT * FROM assignments WHERE session_id = ? AND state = 'held'",
                (session_id,)).fetchall()
            for row in held:
                self._cancel_row_locked(con, row, "session_closed", now_iso)
            con.execute(
                "UPDATE waitlist_entries SET status = 'cancelled', cancelled_at = ? "
                "WHERE session_id = ? AND status = 'waiting'",
                (now_iso, session_id))
            log_event(con, at=now_iso, actor_id=actor_id, action="session.closed",
                      session_id=session_id,
                      details={"held_cancelled": len(held)})
        return self.session_status(session_id)

    def promote_now(self, session_id: str, *, actor_id: str | None = None) -> dict[str, Any]:
        """运营员在资格补齐等事件后手动触发一次递补尝试。"""
        session = self.identities.require_session(session_id)
        now_iso = self.clock.iso()
        with self.db.write() as con:
            self._sweep_session_locked(con, session_id, now_iso)
            outcome = self._promote_locked(con, session, now_iso)
            promotions = outcome["promotions"]
            blocked = outcome["blocked"]
            for p in promotions:
                self._log_promotion_locked(con, p, now_iso, actor_id)
        return {"session_id": session_id,
                "promotions": [{"waitlist_seq": p["seq"], "volunteer_id": p["volunteer_id"],
                                "assignment_id": p["assignment_id"]} for p in promotions],
                "blocked": blocked}

    # ==================================================================
    # 查询与解释
    # ==================================================================

    def get_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            """
            SELECT a.*, s.venue_id, s.title AS session_title, s.starts_at, s.ends_at,
                   s.capacity, v.name AS venue_name, vol.name AS volunteer_name,
                   vol.is_minor
              FROM assignments a
              JOIN sessions s ON s.id = a.session_id
              JOIN venues v ON v.id = s.venue_id
              JOIN volunteers vol ON vol.id = a.volunteer_id
             WHERE a.id = ?
            """,
            (assignment_id,),
        )
        return self._decorate_assignment(row) if row else None

    def require_assignment(self, assignment_id: str) -> dict[str, Any]:
        row = self.get_assignment(assignment_id)
        if row is None:
            raise DomainError.not_found("ASSIGNMENT_NOT_FOUND", f"安排不存在：{assignment_id}")
        return row

    def list_assignments(
        self, *, session_id: str | None = None, volunteer_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT a.*, s.venue_id, s.title AS session_title, s.starts_at, s.ends_at, "
            "s.capacity, v.name AS venue_name, vol.name AS volunteer_name, vol.is_minor "
            "FROM assignments a "
            "JOIN sessions s ON s.id = a.session_id "
            "JOIN venues v ON v.id = s.venue_id "
            "JOIN volunteers vol ON vol.id = a.volunteer_id WHERE 1=1"
        )
        params: list[Any] = []
        if session_id:
            sql += " AND a.session_id = ?"
            params.append(session_id)
        if volunteer_id:
            sql += " AND a.volunteer_id = ?"
            params.append(volunteer_id)
        sql += " ORDER BY a.created_at, a.id"
        rows = self.db.query_all(sql, params)
        return [self._decorate_assignment(r) for r in rows]

    def session_status(self, session_id: str) -> dict[str, Any]:
        session = self.identities.require_session(session_id)
        held = self.db.query_one(
            "SELECT COUNT(*) AS c FROM assignments WHERE session_id = ? AND state = 'held'",
            (session_id,),
        )["c"]
        confirmed = self.db.query_one(
            "SELECT COUNT(*) AS c FROM assignments WHERE session_id = ? AND state = 'confirmed'",
            (session_id,),
        )["c"]
        waiting_rows = self.db.query_all(
            "SELECT w.*, vol.name AS volunteer_name FROM waitlist_entries w "
            "JOIN volunteers vol ON vol.id = w.volunteer_id "
            "WHERE w.session_id = ? AND w.status = 'waiting' ORDER BY w.seq",
            (session_id,),
        )
        occupied = held + confirmed
        waitlist = [
            {"id": r["id"], "seq": r["seq"], "position": i + 1,
             "volunteer_id": r["volunteer_id"], "volunteer_name": r["volunteer_name"],
             "enqueued_at": r["enqueued_at"]}
            for i, r in enumerate(waiting_rows)
        ]
        # 有名额却未递补时，解释队头为何不能前进（只读诊断，不改状态）。
        head_blocker = None
        if waitlist and session["status"] == "open" and session["capacity"] - occupied > 0:
            head_blocker = self._diagnose_head_blocker(waiting_rows[0], session)
        if head_blocker is not None:
            waitlist[0]["block_reason"] = head_blocker
        return {
            "session": dict(session),
            "capacity": session["capacity"],
            "held": held,
            "confirmed": confirmed,
            "occupied": occupied,
            "free": session["capacity"] - occupied,
            "waitlist": waitlist,
            "head_blocker": head_blocker,
        }

    def _diagnose_head_blocker(
        self, head_row: sqlite3.Row, session: sqlite3.Row
    ) -> dict[str, Any] | None:
        now_iso = self.clock.iso()
        volunteer = self.identities.require_volunteer(head_row["volunteer_id"])
        training = self.identities.get_training(head_row["volunteer_id"], session["venue_id"])
        if training is None or training["status"] != "passed":
            return {"code": "TRAINING_MISSING", "message": "队头未取得该场馆培训合格资格"}
        if training["expires_at"] and training["expires_at"] <= now_iso:
            return {"code": "TRAINING_EXPIRED", "message": "队头培训资格已过期",
                    "expires_at": training["expires_at"]}
        if volunteer["is_minor"]:
            consent = self.consents.covering_consent_for(
                head_row["volunteer_id"], session["venue_id"])
            if consent is None:
                return {"code": "CONSENT_NOT_COVERING",
                        "message": "队头缺少覆盖该场馆的有效监护授权"}
        start = parse_dt(session["starts_at"])
        end = parse_dt(session["ends_at"])
        rows = self.db.query_all(
            "SELECT a.id, s.title, s.starts_at, s.ends_at FROM assignments a "
            "JOIN sessions s ON s.id = a.session_id "
            "WHERE a.volunteer_id = ? AND a.state = 'confirmed'",
            (head_row["volunteer_id"],),
        )
        for row in rows:
            if start < parse_dt(row["ends_at"]) and parse_dt(row["starts_at"]) < end:
                return {"code": "SESSION_TIME_OVERLAP",
                        "message": f"队头与已确认场次 {row['title']} 时间重叠",
                        "other_assignment_id": row["id"]}
        return None

    def get_waitlist_entry(self, entry_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM waitlist_entries WHERE id = ?", (entry_id,))
        return dict(row) if row else None

    def list_waitlist(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.query_all(
            "SELECT * FROM waitlist_entries WHERE session_id = ? ORDER BY seq",
            (session_id,),
        )
        return [dict(r) for r in rows]

    def explain_assignment(self, assignment_id: str) -> dict[str, Any]:
        """逐字段说明一个安排为何有效（运营解释视图）。"""
        a = self.require_assignment(assignment_id)
        session = self.identities.require_session(a["session_id"])
        checks: list[dict[str, Any]] = []

        # 名额
        counts = self.db.query_one(
            "SELECT COALESCE(SUM(state = 'confirmed'), 0) AS confirmed, "
            "COALESCE(SUM(state = 'held'), 0) AS held FROM assignments "
            "WHERE session_id = ? AND state != 'cancelled'",
            (a["session_id"],),
        )
        checks.append({
            "check": "capacity",
            "ok": counts["confirmed"] + counts["held"] <= session["capacity"],
            "detail": {
                "capacity": session["capacity"],
                "confirmed_now": counts["confirmed"],
                "held_now": counts["held"],
                "occupied_atomically": True,
            },
        })

        # 培训
        if a["state"] == STATE_CONFIRMED:
            checks.append({
                "check": "training",
                "ok": a["training_venue_id"] == session["venue_id"]
                       and a["training_achieved_at"] is not None
                       and (not a["training_expires_at"]
                            or a["training_expires_at"] > a["confirmed_at"]),
                "detail": {
                    "venue_id": a["training_venue_id"],
                    "achieved_at": a["training_achieved_at"],
                    "expires_at": a["training_expires_at"],
                    "snapshot_fixed_at": a["confirmed_at"],
                },
            })
            # 监护授权
            if a["consent_version_id"]:
                version = self.consents.require_version(a["consent_version_id"])
                covers = scope_covers(a["consent_scope"], session["venue_id"])
                checks.append({
                    "check": "guardian_consent",
                    "ok": covers and a["consent_issued_at"] <= a["confirmed_at"],
                    "detail": {
                        "required": True,
                        "version_id": a["consent_version_id"],
                        "seq": a["consent_seq"],
                        "issued_at": a["consent_issued_at"],
                        "scope_snapshot": a["consent_scope"],
                        "covers_service_venue": covers,
                        "service_venue_id": session["venue_id"],
                        "version_current_status": version["status"],
                        "note": ("授权范围与版本在确认时快照固定；事后撤回或改版"
                                 "不改变本安排的确认有效性"),
                    },
                })
            else:
                checks.append({
                    "check": "guardian_consent",
                    "ok": not a["is_minor"],
                    "detail": {"required": bool(a["is_minor"]),
                               "reason": "成年志愿者无需监护授权"},
                })
        else:
            checks.append({
                "check": "state",
                "ok": a["state"] == STATE_HELD,
                "detail": {"state": a["state"],
                           "hold_expires_at": a.get("hold_expires_at"),
                           "cancel_reason": a.get("cancel_reason"),
                           "note": "暂占只锁定名额，授权与培训在确认时校验并固化"},
            })

        source = {"type": a["source"]}
        if a["source"] == "waitlist":
            source["waitlist_seq"] = a["waitlist_seq"]
        elif a["source"] == "transfer":
            source["transferred_from_assignment_id"] = a["transferred_from_assignment_id"]

        return {
            "assignment": a,
            "valid": all(c["ok"] for c in checks) if a["state"] == STATE_CONFIRMED else None,
            "why": checks,
            "source": source,
        }

    def events(self, *, limit: int = 100, session_id: str | None = None,
               volunteer_id: str | None = None, assignment_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if volunteer_id:
            sql += " AND volunteer_id = ?"
            params.append(volunteer_id)
        if assignment_id:
            sql += " AND assignment_id = ?"
            params.append(assignment_id)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query_all(sql, params)
        import json as _json

        result = []
        for r in rows:
            d = dict(r)
            d["details"] = _json.loads(d.pop("details_json"))
            result.append(d)
        return result

    # ==================================================================
    # 事务内原语
    # ==================================================================

    def _expire_and_promote(self, session: sqlite3.Row,
                            actor_id: str | None = None) -> dict[str, Any]:
        """在独立事务中清理某场次过期暂占并递补；无论调用方后续成败都已提交。"""
        now_iso = self.clock.iso()
        with self.db.write() as con:
            self._sweep_session_locked(con, session["id"], now_iso)
            outcome = self._promote_locked(con, session, now_iso)
            for p in outcome["promotions"]:
                self._log_promotion_locked(con, p, now_iso, actor_id)
        return outcome

    def _cancel_row_locked(
        self, con: sqlite3.Connection, row: sqlite3.Row, reason: str, now_iso: str
    ) -> None:
        con.execute(
            "UPDATE assignments SET state = 'cancelled', cancelled_at = ?, "
            "cancel_reason = ? WHERE id = ?",
            (now_iso, reason, row["id"]),
        )

    def _promote_locked(
        self, con: sqlite3.Connection, session: sqlite3.Row, now_iso: str
    ) -> dict[str, Any]:
        """严格 FIFO 递补。

        返回 ``{"promotions": [...], "blocked": {...}|None}``：队头不满足资格
        或存在时间冲突时，本轮递补在队头确定停止（不跳过），并记录阻塞原因，
        供运营员解释候补为何没有前进。
        """
        promotions: list[dict[str, Any]] = []
        blocked: dict[str, Any] | None = None
        if session["status"] != "open":
            return {"promotions": promotions, "blocked": blocked}
        while True:
            head = con.execute(
                "SELECT * FROM waitlist_entries WHERE session_id = ? AND status = 'waiting' "
                "ORDER BY seq LIMIT 1",
                (session["id"],),
            ).fetchone()
            if head is None:
                break
            active = con.execute(
                "SELECT id FROM assignments WHERE session_id = ? AND volunteer_id = ? "
                "AND state != 'cancelled'",
                (session["id"], head["volunteer_id"]),
            ).fetchone()
            if active is not None:
                # 不应发生（唯一索引兜底）：确定性地作废弃条处理。
                con.execute(
                    "UPDATE waitlist_entries SET status = 'cancelled', cancelled_at = ? "
                    "WHERE id = ?",
                    (now_iso, head["id"]),
                )
                continue
            if self._free_capacity_locked(con, session) <= 0:
                break
            volunteer = con.execute(
                "SELECT * FROM volunteers WHERE id = ?", (head["volunteer_id"],)
            ).fetchone()
            try:
                training, consent = self._eligibility_locked(
                    con, volunteer, session["venue_id"], now_iso
                )
                self._check_session_time_locked(
                    con, volunteer["id"], session, exclude_assignment=None)
            except DomainError as exc:
                blocked = {
                    "waitlist_entry_id": head["id"], "waitlist_seq": head["seq"],
                    "volunteer_id": head["volunteer_id"], "reason_code": exc.code,
                    "reason": exc.message, "details": exc.details,
                }
                break
            expires = (parse_dt(now_iso)
                       + timedelta(seconds=self.default_hold_seconds)).isoformat()
            new_assignment = new_id("asg")
            con.execute(
                """
                INSERT INTO assignments (id, session_id, volunteer_id, state, created_at,
                                         held_at, hold_expires_at, source,
                                         waitlist_entry_id, waitlist_seq)
                VALUES (?, ?, ?, 'held', ?, ?, ?, 'waitlist', ?, ?)
                """,
                (new_assignment, session["id"], head["volunteer_id"], now_iso,
                 now_iso, expires, head["id"], head["seq"]),
            )
            con.execute(
                "UPDATE waitlist_entries SET status = 'promoted', promoted_at = ?, "
                "assignment_id = ? WHERE id = ?",
                (now_iso, new_assignment, head["id"]),
            )
            promotions.append({
                "session_id": session["id"], "seq": head["seq"],
                "volunteer_id": head["volunteer_id"], "assignment_id": new_assignment,
                "hold_expires_at": expires,
            })
        return {"promotions": promotions, "blocked": blocked}

    def _log_promotion_locked(
        self, con: sqlite3.Connection, promotion: dict[str, Any], now_iso: str,
        actor_id: str | None,
    ) -> None:
        log_event(con, at=now_iso, actor_id=actor_id, action="waitlist.promoted",
                  assignment_id=promotion["assignment_id"],
                  session_id=promotion["session_id"],
                  volunteer_id=promotion["volunteer_id"],
                  details={"waitlist_seq": promotion["seq"],
                           "hold_expires_at": promotion["hold_expires_at"]})

    def _sweep_session_locked(
        self, con: sqlite3.Connection, session_id: str, now_iso: str
    ) -> list[sqlite3.Row]:
        expired = con.execute(
            "SELECT * FROM assignments WHERE session_id = ? AND state = 'held' "
            "AND hold_expires_at <= ?",
            (session_id, now_iso),
        ).fetchall()
        for row in expired:
            self._cancel_row_locked(con, row, "hold_expired", now_iso)
        return expired

    def _free_capacity_locked(
        self, con: sqlite3.Connection, session: sqlite3.Row
    ) -> int:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE session_id = ? "
            "AND state != 'cancelled'",
            (session["id"],),
        ).fetchone()
        return session["capacity"] - row["c"]

    def _check_eligibility_locked(
        self,
        con: sqlite3.Connection,
        volunteer: sqlite3.Row,
        venue_id: str,
        now_iso: str,
    ) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        training, consent = self._eligibility_locked(con, volunteer, venue_id, now_iso)
        return training, consent

    def _eligibility_locked(
        self,
        con: sqlite3.Connection,
        volunteer: sqlite3.Row,
        venue_id: str,
        now_iso: str,
    ) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        training = con.execute(
            "SELECT * FROM training_records WHERE volunteer_id = ? AND venue_id = ?",
            (volunteer["id"], venue_id),
        ).fetchone()
        if training is None or training["status"] != "passed":
            raise DomainError.conflict(
                "TRAINING_MISSING",
                "志愿者未取得该场馆的培训合格资格",
                {"volunteer_id": volunteer["id"], "venue_id": venue_id},
            )
        if training["expires_at"] and training["expires_at"] <= now_iso:
            raise DomainError.conflict(
                "TRAINING_EXPIRED",
                "志愿者在该场馆的培训资格已过期",
                {"volunteer_id": volunteer["id"], "venue_id": venue_id,
                 "expires_at": training["expires_at"]},
            )
        consent = None
        if volunteer["is_minor"]:
            consent = ConsentService.find_covering_consent(con, volunteer["id"], venue_id)
            if consent is None:
                raise DomainError.conflict(
                    "CONSENT_NOT_COVERING",
                    "没有覆盖实际服务场馆的有效监护授权",
                    {"volunteer_id": volunteer["id"], "venue_id": venue_id},
                )
        return training, consent

    def _check_session_time_locked(
        self,
        con: sqlite3.Connection,
        volunteer_id: str,
        session: sqlite3.Row,
        *,
        exclude_assignment: str | None,
    ) -> None:
        start = parse_dt(session["starts_at"])
        end = parse_dt(session["ends_at"])
        rows = con.execute(
            "SELECT a.id, s.starts_at, s.ends_at, s.title FROM assignments a "
            "JOIN sessions s ON s.id = a.session_id "
            "WHERE a.volunteer_id = ? AND a.state = 'confirmed'",
            (volunteer_id,),
        ).fetchall()
        for row in rows:
            if exclude_assignment and row["id"] == exclude_assignment:
                continue
            if start < parse_dt(row["ends_at"]) and parse_dt(row["starts_at"]) < end:
                raise DomainError.conflict(
                    "SESSION_TIME_OVERLAP",
                    f"与已确认场次 {row['title']} 的服务时间重叠",
                    {"other_assignment_id": row["id"]},
                )

    def _waitlist_position(
        self, con: sqlite3.Connection, session_id: str, seq: int
    ) -> int:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM waitlist_entries WHERE session_id = ? "
            "AND status = 'waiting' AND seq <= ?",
            (session_id, seq),
        ).fetchone()
        return row["c"]

    def _check_hold_seconds(self, hold_seconds: int | None) -> int:
        if hold_seconds is None:
            return self.default_hold_seconds
        if not isinstance(hold_seconds, int) or not (1 <= hold_seconds <= MAX_HOLD_SECONDS):
            raise DomainError.validation(
                "INVALID_HOLD_SECONDS",
                f"暂占秒数必须是 1..{MAX_HOLD_SECONDS} 之间的整数",
            )
        return hold_seconds

    def _require_assignment_row(self, assignment_id: str) -> sqlite3.Row:
        row = self.db.query_one("SELECT * FROM assignments WHERE id = ?", (assignment_id,))
        if row is None:
            raise DomainError.not_found("ASSIGNMENT_NOT_FOUND", f"安排不存在：{assignment_id}")
        return row

    def _require_waitlist_row(self, entry_id: str) -> sqlite3.Row:
        row = self.db.query_one("SELECT * FROM waitlist_entries WHERE id = ?", (entry_id,))
        if row is None:
            raise DomainError.not_found("WAITLIST_NOT_FOUND", f"候补记录不存在：{entry_id}")
        return row

    def _decorate_assignment(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        raw_scope = data.pop("consent_scope_json", None)
        data["consent_scope"] = scope_from_json(raw_scope) if raw_scope else None
        data["is_minor"] = bool(data["is_minor"])
        return data


def _state_label(state: str) -> str:
    return {"held": "暂占", "confirmed": "已确认", "cancelled": "已取消"}.get(state, state)
