"""未成年志愿者授权排班核心服务。

确定性设计要点：

- 所有写操作在 ``BEGIN IMMEDIATE`` 事务中串行执行，并发写结果确定；
- 确认排班与候补递补在同一事务内原子完成名额占用，并把当时的
  监护授权版本固定（pin）到排班记录上，同时保存校验快照；
- 创建类操作使用幂等键，重复提交返回首次结果，不产生二次占用；
- 候补递补严格按场次内 seq 顺序，逐个校验资格，失败者标记原因后跳过；
- 过期暂占持久化在 SQLite 中，重启后由 ``recover()`` 继续清理并触发递补。
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator

from .clock import SystemClock
from .errors import DomainError
from .storage import Database

MINOR_AGE_LIMIT = 18
DEFAULT_HOLD_TTL_SECONDS = 900

ROLE_OPERATOR = "operator"
ROLE_GUARDIAN = "guardian"
ROLE_MANAGER = "manager"
ROLE_SYSTEM = "system"

CANCEL_REASON_TEXT = {
    "consent_withdrawn": "监护授权已撤回",
    "transferred": "已调班至其他场次",
    "operator_cancelled": "运营员取消",
}


@dataclass(frozen=True)
class Actor:
    """调用方身份；角色决定可见范围与可执行操作。"""

    role: str
    id: str


SYSTEM_ACTOR = Actor(ROLE_SYSTEM, "system")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DomainError("validation", f"{field} 必须是 ISO8601 时间字符串", 400)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DomainError("validation", f"{field} 不是合法的时间格式", 400) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise DomainError("validation", f"{field} 必须是 YYYY-MM-DD 日期字符串", 400)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DomainError("validation", f"{field} 不是合法的日期格式（YYYY-MM-DD）", 400) from None


def _age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


class SchedulingService:
    """排班领域服务；所有公开方法先做角色校验，再在事务内执行规则。"""

    def __init__(
        self,
        db_path: str,
        *,
        clock: Any = None,
        hold_ttl_seconds: int = DEFAULT_HOLD_TTL_SECONDS,
        recover: bool = True,
    ) -> None:
        self.db = Database(db_path)
        self.clock = clock or SystemClock()
        self.hold_ttl_seconds = hold_ttl_seconds
        if recover:
            # 重启恢复：继续清理上次运行遗留的过期暂占，并触发候补递补。
            self.recover()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[Any]:
        conn = self.db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now(self) -> datetime:
        return self.clock.now()

    @staticmethod
    def _require(actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise DomainError("forbidden", "当前角色无权执行该操作", 403)

    def _get(self, conn: Any, table: str, rid: str, label: str) -> Any:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise DomainError("not_found", f"未找到{label}：{rid}", 404)
        return row

    def _require_guardian_scope(self, conn: Any, actor: Actor, volunteer_id: str) -> None:
        """运营员放行；监护人只能触及名下志愿者的记录。"""
        if actor.role == ROLE_OPERATOR:
            return
        if actor.role == ROLE_GUARDIAN:
            volunteer = self._get(conn, "volunteers", volunteer_id, "志愿者")
            if volunteer["guardian_id"] == actor.id:
                return
        raise DomainError("forbidden", "只能操作本人关联的未成年志愿者记录", 403)

    def _event(self, conn: Any, actor: Actor, action: str, entity_type: str, entity_id: str, **detail: Any) -> None:
        conn.execute(
            "INSERT INTO events (ts, actor_role, actor_id, action, entity_type, entity_id, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_iso(self._now()), actor.role, actor.id, action, entity_type, entity_id,
             json.dumps(detail, ensure_ascii=False, sort_keys=True)),
        )

    # ------------------------------------------------------------------
    # 字典化
    # ------------------------------------------------------------------
    @staticmethod
    def _consent_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "volunteer_id": row["volunteer_id"],
            "version_no": row["version_no"],
            "venue_scope": json.loads(row["venue_scope"]),
            "status": row["status"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "created_at": row["created_at"],
            "withdrawn_at": row["withdrawn_at"],
        }

    @staticmethod
    def _assignment_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "volunteer_id": row["volunteer_id"],
            "consent_version_id": row["consent_version_id"],
            "hold_id": row["hold_id"],
            "idempotency_key": row["idempotency_key"],
            "source": row["source"],
            "status": row["status"],
            "cancel_reason": row["cancel_reason"],
            "transferred_from": row["transferred_from"],
            "created_at": row["created_at"],
            "cancelled_at": row["cancelled_at"],
        }

    @staticmethod
    def _hold_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "volunteer_id": row["volunteer_id"],
            "idempotency_key": row["idempotency_key"],
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _session_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "venue_id": row["venue_id"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "capacity": row["capacity"],
            "required_qualifications": json.loads(row["required_qualifications"]),
            "status": row["status"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _waitlist_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "volunteer_id": row["volunteer_id"],
            "seq": row["seq"],
            "status": row["status"],
            "note": row["note"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    @staticmethod
    def _volunteer_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "birth_date": row["birth_date"],
            "guardian_id": row["guardian_id"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _training_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "volunteer_id": row["volunteer_id"],
            "qualification": row["qualification"],
            "granted_at": row["granted_at"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _event_dict(row: Any) -> dict:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "actor_role": row["actor_role"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "detail": json.loads(row["detail"]),
        }

    # ------------------------------------------------------------------
    # 档案登记
    # ------------------------------------------------------------------
    def create_guardian(self, actor: Actor, name: Any, guardian_id: Any = None) -> dict:
        self._require(actor, ROLE_OPERATOR)
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name:
            raise DomainError("validation", "监护人姓名不能为空", 400)
        gid = guardian_id if isinstance(guardian_id, str) and guardian_id.strip() else _new_id("grd")
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM guardians WHERE id = ?", (gid,)).fetchone():
                raise DomainError("validation", f"监护人编号已存在：{gid}", 400)
            conn.execute("INSERT INTO guardians (id, name, created_at) VALUES (?, ?, ?)",
                         (gid, name, _iso(self._now())))
            self._event(conn, actor, "guardian_created", "guardian", gid)
            return dict(self._get(conn, "guardians", gid, "监护人"))

    def create_volunteer(self, actor: Actor, name: Any, birth_date: Any, guardian_id: Any) -> dict:
        self._require(actor, ROLE_OPERATOR)
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name:
            raise DomainError("validation", "志愿者姓名不能为空", 400)
        birth = _parse_date(birth_date, "birth_date")
        if _age_on(birth, self._now().date()) >= MINOR_AGE_LIMIT:
            raise DomainError("not_minor", "报名者已成年，不属于未成年志愿项目", 409)
        with self._tx() as conn:
            if not isinstance(guardian_id, str) or not conn.execute(
                    "SELECT 1 FROM guardians WHERE id = ?", (guardian_id,)).fetchone():
                raise DomainError("not_found", f"未找到监护人：{guardian_id}", 404)
            vid = _new_id("vol")
            conn.execute(
                "INSERT INTO volunteers (id, name, birth_date, guardian_id, status, created_at)"
                " VALUES (?, ?, ?, ?, 'active', ?)",
                (vid, name, birth.isoformat(), guardian_id, _iso(self._now())))
            self._event(conn, actor, "volunteer_created", "volunteer", vid, guardian_id=guardian_id)
            return self._volunteer_dict(self._get(conn, "volunteers", vid, "志愿者"))

    def create_venue(self, actor: Actor, name: Any, venue_id: Any = None) -> dict:
        self._require(actor, ROLE_OPERATOR)
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name:
            raise DomainError("validation", "场馆名称不能为空", 400)
        vid = venue_id if isinstance(venue_id, str) and venue_id.strip() else _new_id("ven")
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM venues WHERE id = ?", (vid,)).fetchone():
                raise DomainError("validation", f"场馆编号已存在：{vid}", 400)
            conn.execute("INSERT INTO venues (id, name, created_at) VALUES (?, ?, ?)",
                         (vid, name, _iso(self._now())))
            self._event(conn, actor, "venue_created", "venue", vid)
            return dict(self._get(conn, "venues", vid, "场馆"))

    def create_session(
        self,
        actor: Actor,
        venue_id: Any,
        starts_at: Any,
        ends_at: Any,
        capacity: Any,
        required_qualifications: Any = None,
    ) -> dict:
        self._require(actor, ROLE_OPERATOR)
        starts = _parse_ts(starts_at, "starts_at")
        ends = _parse_ts(ends_at, "ends_at")
        if ends <= starts:
            raise DomainError("validation", "场次结束时间必须晚于开始时间", 400)
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise DomainError("validation", "场次容量必须是正整数", 400)
        quals = required_qualifications or []
        if not isinstance(quals, (list, tuple)) or any(not isinstance(q, str) or not q.strip() for q in quals):
            raise DomainError("validation", "培训资格要求必须是非空字符串列表", 400)
        quals = sorted({q.strip() for q in quals})
        with self._tx() as conn:
            self._get(conn, "venues", venue_id if isinstance(venue_id, str) else "", "场馆")
            sid = _new_id("ses")
            conn.execute(
                "INSERT INTO sessions (id, venue_id, starts_at, ends_at, capacity,"
                " required_qualifications, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                (sid, venue_id, _iso(starts), _iso(ends), capacity,
                 json.dumps(quals, ensure_ascii=False), _iso(self._now())))
            self._event(conn, actor, "session_created", "session", sid,
                        session_id=sid, venue_id=venue_id, capacity=capacity)
            return self._session_dict(self._get(conn, "sessions", sid, "场次"))

    def close_session(self, actor: Actor, session_id: str) -> dict:
        self._require(actor, ROLE_OPERATOR)
        with self._tx() as conn:
            session = self._get(conn, "sessions", session_id, "场次")
            if session["status"] == "closed":
                return {"session": self._session_dict(session), "already_closed": True}
            conn.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (session_id,))
            self._event(conn, actor, "session_closed", "session", session_id, session_id=session_id)
            return {"session": self._session_dict(self._get(conn, "sessions", session_id, "场次")),
                    "already_closed": False}

    # ------------------------------------------------------------------
    # 监护授权与培训资格
    # ------------------------------------------------------------------
    def publish_consent(
        self,
        actor: Actor,
        volunteer_id: str,
        venue_scope: Any,
        valid_from: Any = None,
        valid_until: Any = None,
    ) -> dict:
        """发布新授权版本；旧的 active 版本转为 superseded（已固定的排班不受影响）。"""
        self._require(actor, ROLE_OPERATOR)
        if not isinstance(venue_scope, (list, tuple)) or not venue_scope:
            raise DomainError("validation", "授权范围必须是非空场馆列表", 400)
        scope = list(dict.fromkeys(venue_scope))
        if any(not isinstance(v, str) or not v.strip() for v in scope):
            raise DomainError("validation", "授权范围内的场馆编号必须是非空字符串", 400)
        now = self._now()
        valid_from_dt = _parse_ts(valid_from, "valid_from") if valid_from else now
        valid_until_dt = _parse_ts(valid_until, "valid_until") if valid_until else None
        if valid_until_dt is not None and valid_until_dt <= valid_from_dt:
            raise DomainError("validation", "授权截止时间必须晚于生效时间", 400)
        with self._tx() as conn:
            self._get(conn, "volunteers", volunteer_id, "志愿者")
            for venue_id in scope:
                self._get(conn, "venues", venue_id, "场馆")
            previous = conn.execute(
                "SELECT * FROM consent_versions WHERE volunteer_id = ? AND status = 'active'",
                (volunteer_id,)).fetchone()
            version_no = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM consent_versions WHERE volunteer_id = ?",
                (volunteer_id,)).fetchone()[0]
            if previous is not None:
                conn.execute("UPDATE consent_versions SET status = 'superseded' WHERE id = ?",
                             (previous["id"],))
            cid = _new_id("con")
            conn.execute(
                "INSERT INTO consent_versions (id, volunteer_id, version_no, venue_scope, status,"
                " valid_from, valid_until, created_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (cid, volunteer_id, version_no, json.dumps(scope, ensure_ascii=False),
                 _iso(valid_from_dt), _iso(valid_until_dt) if valid_until_dt else None, _iso(now)))
            self._event(conn, actor, "consent_published", "consent", cid,
                        volunteer_id=volunteer_id, version_no=version_no, venue_scope=scope)
            return self._consent_dict(self._get(conn, "consent_versions", cid, "监护授权"))

    def withdraw_consent(self, actor: Actor, consent_id: str) -> dict:
        """撤回授权版本：级联取消固定该版本的生效排班，并在同事务触发候补递补。

        重复撤回返回首次取消的排班清单（由数据库状态推导），不产生二次效果。
        """
        now = self._now()
        with self._tx() as conn:
            consent = self._get(conn, "consent_versions", consent_id, "监护授权")
            self._require_guardian_scope(conn, actor, consent["volunteer_id"])
            if consent["status"] == "withdrawn":
                cancelled = [r["id"] for r in conn.execute(
                    "SELECT id FROM assignments WHERE consent_version_id = ?"
                    " AND cancel_reason = 'consent_withdrawn' ORDER BY rowid", (consent_id,))]
                return {"consent": self._consent_dict(consent),
                        "cancelled_assignment_ids": cancelled,
                        "promotions": [], "already_withdrawn": True}
            if consent["status"] not in ("active", "superseded"):
                raise DomainError("invalid_state", "授权版本当前状态不可撤回", 409)
            conn.execute("UPDATE consent_versions SET status = 'withdrawn', withdrawn_at = ?"
                         " WHERE id = ?", (_iso(now), consent_id))
            self._event(conn, actor, "consent_withdrawn", "consent", consent_id,
                        volunteer_id=consent["volunteer_id"], version_no=consent["version_no"])
            cancelled: list[str] = []
            promotions: list[dict] = []
            rows = conn.execute(
                "SELECT * FROM assignments WHERE consent_version_id = ? AND status = 'confirmed'"
                " ORDER BY rowid", (consent_id,)).fetchall()
            for assignment in rows:
                conn.execute(
                    "UPDATE assignments SET status = 'cancelled', cancel_reason = 'consent_withdrawn',"
                    " cancelled_at = ? WHERE id = ?", (_iso(now), assignment["id"]))
                self._event(conn, actor, "assignment_cancelled", "assignment", assignment["id"],
                            session_id=assignment["session_id"], reason="consent_withdrawn",
                            volunteer_id=assignment["volunteer_id"])
                cancelled.append(assignment["id"])
                promotions.extend(self._promote_waitlist(conn, assignment["session_id"], actor, now))
            fresh = self._get(conn, "consent_versions", consent_id, "监护授权")
            return {"consent": self._consent_dict(fresh),
                    "cancelled_assignment_ids": cancelled,
                    "promotions": promotions, "already_withdrawn": False}

    def add_training(
        self,
        actor: Actor,
        volunteer_id: str,
        qualification: Any,
        granted_at: Any = None,
        expires_at: Any = None,
    ) -> dict:
        self._require(actor, ROLE_OPERATOR)
        if not isinstance(qualification, str) or not qualification.strip():
            raise DomainError("validation", "培训资格名称不能为空", 400)
        granted = _parse_ts(granted_at, "granted_at") if granted_at else self._now()
        expires = _parse_ts(expires_at, "expires_at") if expires_at else None
        if expires is not None and expires <= granted:
            raise DomainError("validation", "资格失效时间必须晚于获得时间", 400)
        with self._tx() as conn:
            self._get(conn, "volunteers", volunteer_id, "志愿者")
            tid = _new_id("trn")
            conn.execute(
                "INSERT INTO trainings (id, volunteer_id, qualification, granted_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (tid, volunteer_id, qualification.strip(), _iso(granted),
                 _iso(expires) if expires else None))
            self._event(conn, actor, "training_added", "training", tid,
                        volunteer_id=volunteer_id, qualification=qualification.strip())
            return self._training_dict(self._get(conn, "trainings", tid, "培训记录"))

    # ------------------------------------------------------------------
    # 资格校验（确认与候补递补共用，保证判定口径一致）
    # ------------------------------------------------------------------
    def _check_eligibility(self, conn: Any, session: Any, volunteer: Any, now: datetime) -> Any:
        """按固定顺序校验，首个失败即抛出稳定错误码；通过时返回生效授权版本。"""
        if volunteer["status"] != "active":
            raise DomainError("volunteer_inactive", "志愿者状态不可用", 409)
        birth = _parse_date(volunteer["birth_date"], "birth_date")
        starts = _parse_ts(session["starts_at"], "starts_at")
        if _age_on(birth, starts.date()) >= MINOR_AGE_LIMIT:
            raise DomainError("not_minor", "志愿者在场次开始时已成年，不符合未成年志愿项目要求", 409)
        now_iso = _iso(now)
        consent = conn.execute(
            "SELECT * FROM consent_versions WHERE volunteer_id = ? AND status = 'active'"
            " AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)"
            " ORDER BY version_no DESC LIMIT 1",
            (volunteer["id"], now_iso, now_iso)).fetchone()
        if consent is None:
            raise DomainError("consent_missing", "当前没有生效的监护授权", 409)
        if session["venue_id"] not in json.loads(consent["venue_scope"]):
            raise DomainError(
                "consent_scope", f"监护授权范围不覆盖场馆 {session['venue_id']}", 409)
        required = set(json.loads(session["required_qualifications"]))
        if required:
            held = {r["qualification"] for r in conn.execute(
                "SELECT qualification FROM trainings WHERE volunteer_id = ?"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (volunteer["id"], session["starts_at"])).fetchall()}
            missing = sorted(required - held)
            if missing:
                raise DomainError("qualification_missing", "缺少培训资格：" + "、".join(missing), 409)
        return consent

    @staticmethod
    def _capacity_usage(conn: Any, session_id: str, now_iso: str) -> tuple[int, int]:
        confirmed = conn.execute(
            "SELECT COUNT(*) FROM assignments WHERE session_id = ? AND status = 'confirmed'",
            (session_id,)).fetchone()[0]
        holds = conn.execute(
            "SELECT COUNT(*) FROM holds WHERE session_id = ? AND status = 'active' AND expires_at > ?",
            (session_id, now_iso)).fetchone()[0]
        return confirmed, holds

    # ------------------------------------------------------------------
    # 暂占与确认
    # ------------------------------------------------------------------
    def create_hold(
        self,
        actor: Actor,
        session_id: str,
        volunteer_id: str,
        idempotency_key: Any,
        ttl_seconds: Any = None,
    ) -> dict:
        """创建暂占：临时占用一个名额，确认前过期自动释放。"""
        self._require(actor, ROLE_OPERATOR)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise DomainError("validation", "缺少幂等键 idempotency_key", 400)
        ttl = ttl_seconds if ttl_seconds is not None else self.hold_ttl_seconds
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            raise DomainError("validation", "暂占有效期必须是正数秒", 400)
        now = self._now()
        now_iso = _iso(now)
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM holds WHERE idempotency_key = ?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if existing["session_id"] != session_id or existing["volunteer_id"] != volunteer_id:
                    raise DomainError("idempotency_conflict", "幂等键已被其他暂占请求使用", 409)
                return {"hold": self._hold_dict(existing), "idempotent_replay": True}
            session = self._get(conn, "sessions", session_id, "场次")
            if session["status"] != "open":
                raise DomainError("session_closed", "场次已关闭，无法创建暂占", 409)
            volunteer = self._get(conn, "volunteers", volunteer_id, "志愿者")
            # 惰性清理该志愿者在场次内已过期的暂占，避免唯一索引挡住新暂占。
            conn.execute(
                "UPDATE holds SET status = 'expired' WHERE session_id = ? AND volunteer_id = ?"
                " AND status = 'active' AND expires_at <= ?", (session_id, volunteer_id, now_iso))
            if conn.execute(
                    "SELECT 1 FROM holds WHERE session_id = ? AND volunteer_id = ? AND status = 'active'",
                    (session_id, volunteer_id)).fetchone():
                raise DomainError("already_held", "该志愿者在此场次已有生效暂占", 409)
            if conn.execute(
                    "SELECT 1 FROM assignments WHERE session_id = ? AND volunteer_id = ?"
                    " AND status = 'confirmed'", (session_id, volunteer_id)).fetchone():
                raise DomainError("already_confirmed", "该志愿者在此场次已有生效排班", 409)
            self._check_eligibility(conn, session, volunteer, now)
            confirmed, active_holds = self._capacity_usage(conn, session_id, now_iso)
            if confirmed + active_holds >= session["capacity"]:
                raise DomainError("session_full", "场次名额已满", 409)
            hid = _new_id("hld")
            expires = now + timedelta(seconds=ttl)
            conn.execute(
                "INSERT INTO holds (id, session_id, volunteer_id, idempotency_key, status,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (hid, session_id, volunteer_id, idempotency_key, now_iso, _iso(expires)))
            self._event(conn, actor, "hold_created", "hold", hid,
                        session_id=session_id, volunteer_id=volunteer_id, expires_at=_iso(expires))
            return {"hold": self._hold_dict(self._get(conn, "holds", hid, "暂占")),
                    "idempotent_replay": False}

    def confirm(
        self,
        actor: Actor,
        session_id: str,
        volunteer_id: str,
        idempotency_key: Any,
        hold_id: Any = None,
    ) -> dict:
        """确认排班：原子占用名额，并把当时的生效授权版本固定到排班上。"""
        self._require(actor, ROLE_OPERATOR)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise DomainError("validation", "缺少幂等键 idempotency_key", 400)
        now = self._now()
        now_iso = _iso(now)
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM assignments WHERE idempotency_key = ?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if existing["session_id"] != session_id or existing["volunteer_id"] != volunteer_id:
                    raise DomainError("idempotency_conflict", "幂等键已被其他确认请求使用", 409)
                return {"assignment": self._assignment_dict(existing), "idempotent_replay": True}
            session = self._get(conn, "sessions", session_id, "场次")
            if session["status"] != "open":
                raise DomainError("session_closed", "场次已关闭，无法确认排班", 409)
            volunteer = self._get(conn, "volunteers", volunteer_id, "志愿者")
            if conn.execute(
                    "SELECT 1 FROM assignments WHERE session_id = ? AND volunteer_id = ?"
                    " AND status = 'confirmed'", (session_id, volunteer_id)).fetchone():
                raise DomainError("already_confirmed", "该志愿者在此场次已有生效排班", 409)
            hold = None
            if hold_id is not None:
                hold = self._get(conn, "holds", hold_id, "暂占")
                if hold["session_id"] != session_id or hold["volunteer_id"] != volunteer_id:
                    raise DomainError("hold_invalid", "暂占记录与本次确认不匹配", 409)
                if hold["status"] != "active" or hold["expires_at"] <= now_iso:
                    raise DomainError("hold_invalid", "暂占已失效，请重新创建", 409)
            consent = self._check_eligibility(conn, session, volunteer, now)
            confirmed, active_holds = self._capacity_usage(conn, session_id, now_iso)
            reserved = 1 if hold is not None else 0
            if confirmed + active_holds - reserved >= session["capacity"]:
                raise DomainError("session_full", "场次名额已满", 409)
            assignment = self._create_assignment(
                conn, session=session, volunteer=volunteer, consent=consent, now=now,
                source={"kind": "manual", "hold_id": hold_id},
                idempotency_key=idempotency_key, actor=actor, hold_id=hold_id)
            if hold is not None:
                conn.execute("UPDATE holds SET status = 'consumed' WHERE id = ?", (hold_id,))
                self._event(conn, actor, "hold_consumed", "hold", hold_id,
                            session_id=session_id, assignment_id=assignment["id"])
            self._event(conn, actor, "assignment_confirmed", "assignment", assignment["id"],
                        session_id=session_id, volunteer_id=volunteer_id,
                        consent_version_id=consent["id"])
            return {"assignment": assignment, "idempotent_replay": False}

    def _create_assignment(
        self,
        conn: Any,
        *,
        session: Any,
        volunteer: Any,
        consent: Any,
        now: datetime,
        source: dict,
        idempotency_key: str,
        actor: Actor,
        hold_id: Any = None,
        transferred_from: Any = None,
    ) -> dict:
        """在事务内写入排班并保存确认瞬间的校验快照。"""
        confirmed, active_holds = self._capacity_usage(conn, session["id"], _iso(now))
        required = json.loads(session["required_qualifications"])
        held = sorted({r["qualification"] for r in conn.execute(
            "SELECT qualification FROM trainings WHERE volunteer_id = ?"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (volunteer["id"], session["starts_at"])).fetchall()})
        starts = _parse_ts(session["starts_at"], "starts_at")
        birth = _parse_date(volunteer["birth_date"], "birth_date")
        snapshot = {
            "confirmed_at": _iso(now),
            "actor": {"role": actor.role, "id": actor.id},
            "session": {
                "id": session["id"], "venue_id": session["venue_id"],
                "starts_at": session["starts_at"], "ends_at": session["ends_at"],
                "capacity": session["capacity"],
            },
            "consent_version": {
                "id": consent["id"], "version_no": consent["version_no"],
                "venue_scope": json.loads(consent["venue_scope"]),
                "valid_from": consent["valid_from"], "valid_until": consent["valid_until"],
            },
            "qualifications": {"required": required, "held": held},
            "capacity_before": {"confirmed": confirmed, "active_holds": active_holds},
            "volunteer": {
                "id": volunteer["id"], "birth_date": volunteer["birth_date"],
                "age_at_session_start": _age_on(birth, starts.date()),
            },
            "source": source,
        }
        aid = _new_id("asg")
        conn.execute(
            "INSERT INTO assignments (id, session_id, volunteer_id, consent_version_id, hold_id,"
            " idempotency_key, source, status, snapshot, transferred_from, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?)",
            (aid, session["id"], volunteer["id"], consent["id"], hold_id, idempotency_key,
             source["kind"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
             transferred_from, _iso(now)))
        return self._assignment_dict(self._get(conn, "assignments", aid, "排班"))

    def cancel_assignment(self, actor: Actor, assignment_id: str, reason: Any = None) -> dict:
        """取消排班并原子触发候补递补；重复取消返回相同状态。"""
        self._require(actor, ROLE_OPERATOR)
        reason = reason if isinstance(reason, str) and reason.strip() else "operator_cancelled"
        now = self._now()
        with self._tx() as conn:
            assignment = self._get(conn, "assignments", assignment_id, "排班")
            if assignment["status"] == "cancelled":
                return {"assignment": self._assignment_dict(assignment),
                        "promotions": [], "already_cancelled": True}
            conn.execute(
                "UPDATE assignments SET status = 'cancelled', cancel_reason = ?, cancelled_at = ?"
                " WHERE id = ?", (reason, _iso(now), assignment_id))
            self._event(conn, actor, "assignment_cancelled", "assignment", assignment_id,
                        session_id=assignment["session_id"], reason=reason,
                        volunteer_id=assignment["volunteer_id"])
            promotions = self._promote_waitlist(conn, assignment["session_id"], actor, now)
            fresh = self._get(conn, "assignments", assignment_id, "排班")
            return {"assignment": self._assignment_dict(fresh),
                    "promotions": promotions, "already_cancelled": False}

    def transfer(self, actor: Actor, assignment_id: str, target_session_id: Any, idempotency_key: Any) -> dict:
        """跨馆调班：原排班取消与新排班确认在同一事务内完成，失败整体回滚。"""
        self._require(actor, ROLE_OPERATOR)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise DomainError("validation", "缺少幂等键 idempotency_key", 400)
        now = self._now()
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM assignments WHERE idempotency_key = ?",
                                    (idempotency_key,)).fetchone()
            if existing is not None:
                if existing["transferred_from"] != assignment_id or existing["session_id"] != target_session_id:
                    raise DomainError("idempotency_conflict", "幂等键已被其他调班请求使用", 409)
                return {"assignment": self._assignment_dict(existing),
                        "previous_assignment_id": assignment_id,
                        "promotions": [], "idempotent_replay": True}
            old = self._get(conn, "assignments", assignment_id, "排班")
            if old["status"] != "confirmed":
                raise DomainError("invalid_state", "只有生效中的排班可以调班", 409)
            if old["session_id"] == target_session_id:
                raise DomainError("validation", "目标场次与当前场次相同", 400)
            target = self._get(conn, "sessions", target_session_id, "场次")
            if target["status"] != "open":
                raise DomainError("session_closed", "目标场次已关闭，无法调入", 409)
            volunteer = self._get(conn, "volunteers", old["volunteer_id"], "志愿者")
            if conn.execute(
                    "SELECT 1 FROM assignments WHERE session_id = ? AND volunteer_id = ?"
                    " AND status = 'confirmed'", (target_session_id, volunteer["id"])).fetchone():
                raise DomainError("already_confirmed", "该志愿者在目标场次已有生效排班", 409)
            consent = self._check_eligibility(conn, target, volunteer, now)
            confirmed, active_holds = self._capacity_usage(conn, target_session_id, _iso(now))
            if confirmed + active_holds >= target["capacity"]:
                raise DomainError("session_full", "目标场次名额已满", 409)
            conn.execute(
                "UPDATE assignments SET status = 'cancelled', cancel_reason = 'transferred',"
                " cancelled_at = ? WHERE id = ?", (_iso(now), assignment_id))
            self._event(conn, actor, "assignment_transferred_out", "assignment", assignment_id,
                        session_id=old["session_id"], target_session_id=target_session_id,
                        volunteer_id=volunteer["id"])
            new_assignment = self._create_assignment(
                conn, session=target, volunteer=volunteer, consent=consent, now=now,
                source={"kind": "transfer", "transferred_from": assignment_id},
                idempotency_key=idempotency_key, actor=actor, transferred_from=assignment_id)
            self._event(conn, actor, "assignment_confirmed", "assignment", new_assignment["id"],
                        session_id=target_session_id, volunteer_id=volunteer["id"],
                        consent_version_id=consent["id"], transferred_from=assignment_id)
            promotions = self._promote_waitlist(conn, old["session_id"], actor, now)
            return {"assignment": new_assignment,
                    "previous_assignment_id": assignment_id,
                    "promotions": promotions, "idempotent_replay": False}

    # ------------------------------------------------------------------
    # 候补
    # ------------------------------------------------------------------
    def join_waitlist(self, actor: Actor, session_id: str, volunteer_id: str) -> dict:
        self._require(actor, ROLE_OPERATOR)
        with self._tx() as conn:
            session = self._get(conn, "sessions", session_id, "场次")
            if session["status"] != "open":
                raise DomainError("session_closed", "场次已关闭，无法加入候补", 409)
            self._get(conn, "volunteers", volunteer_id, "志愿者")
            if conn.execute(
                    "SELECT 1 FROM assignments WHERE session_id = ? AND volunteer_id = ?"
                    " AND status = 'confirmed'", (session_id, volunteer_id)).fetchone():
                raise DomainError("already_confirmed", "该志愿者在此场次已有生效排班", 409)
            existing = conn.execute(
                "SELECT * FROM waitlist_entries WHERE session_id = ? AND volunteer_id = ?"
                " AND status = 'waiting'", (session_id, volunteer_id)).fetchone()
            if existing is not None:
                return {"entry": self._waitlist_dict(existing), "already_waiting": True}
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM waitlist_entries WHERE session_id = ?",
                (session_id,)).fetchone()[0]
            eid = _new_id("wle")
            conn.execute(
                "INSERT INTO waitlist_entries (id, session_id, volunteer_id, seq, status, created_at)"
                " VALUES (?, ?, ?, ?, 'waiting', ?)",
                (eid, session_id, volunteer_id, seq, _iso(self._now())))
            self._event(conn, actor, "waitlist_joined", "waitlist_entry", eid,
                        session_id=session_id, volunteer_id=volunteer_id, seq=seq)
            return {"entry": self._waitlist_dict(self._get(conn, "waitlist_entries", eid, "候补记录")),
                    "already_waiting": False}

    def cancel_waitlist(self, actor: Actor, entry_id: str) -> dict:
        self._require(actor, ROLE_OPERATOR)
        with self._tx() as conn:
            entry = self._get(conn, "waitlist_entries", entry_id, "候补记录")
            if entry["status"] == "cancelled":
                return {"entry": self._waitlist_dict(entry), "already_cancelled": True}
            if entry["status"] != "waiting":
                raise DomainError("invalid_state", "候补记录已处理，无法取消", 409)
            conn.execute(
                "UPDATE waitlist_entries SET status = 'cancelled', resolved_at = ? WHERE id = ?",
                (_iso(self._now()), entry_id))
            self._event(conn, actor, "waitlist_cancelled", "waitlist_entry", entry_id,
                        session_id=entry["session_id"], volunteer_id=entry["volunteer_id"])
            return {"entry": self._waitlist_dict(self._get(conn, "waitlist_entries", entry_id, "候补记录")),
                    "already_cancelled": False}

    def _promote_waitlist(self, conn: Any, session_id: str, actor: Actor, now: datetime) -> list[dict]:
        """按 seq 顺序递补：逐条校验资格，不合格者标记原因跳过，直到名额占满或队列清空。"""
        session = self._get(conn, "sessions", session_id, "场次")
        promotions: list[dict] = []
        if session["status"] != "open":
            return promotions
        while True:
            confirmed, active_holds = self._capacity_usage(conn, session_id, _iso(now))
            if confirmed + active_holds >= session["capacity"]:
                return promotions
            entry = conn.execute(
                "SELECT * FROM waitlist_entries WHERE session_id = ? AND status = 'waiting'"
                " ORDER BY seq LIMIT 1", (session_id,)).fetchone()
            if entry is None:
                return promotions
            volunteer = conn.execute("SELECT * FROM volunteers WHERE id = ?",
                                     (entry["volunteer_id"],)).fetchone()
            try:
                if volunteer is None:
                    raise DomainError("volunteer_inactive", "志愿者状态不可用", 409)
                consent = self._check_eligibility(conn, session, volunteer, now)
                if conn.execute(
                        "SELECT 1 FROM assignments WHERE session_id = ? AND volunteer_id = ?"
                        " AND status = 'confirmed'", (session_id, entry["volunteer_id"])).fetchone():
                    raise DomainError("already_confirmed", "该志愿者在此场次已有生效排班", 409)
            except DomainError as exc:
                conn.execute(
                    "UPDATE waitlist_entries SET status = 'invalid', note = ?, resolved_at = ?"
                    " WHERE id = ?", (exc.code, _iso(now), entry["id"]))
                self._event(conn, actor, "waitlist_invalidated", "waitlist_entry", entry["id"],
                            session_id=session_id, volunteer_id=entry["volunteer_id"],
                            reason=exc.code)
                continue
            assignment = self._create_assignment(
                conn, session=session, volunteer=volunteer, consent=consent, now=now,
                source={"kind": "waitlist", "waitlist_seq": entry["seq"]},
                idempotency_key=f"waitlist:{entry['id']}", actor=actor)
            conn.execute(
                "UPDATE waitlist_entries SET status = 'promoted', resolved_at = ? WHERE id = ?",
                (_iso(now), entry["id"]))
            self._event(conn, actor, "waitlist_promoted", "waitlist_entry", entry["id"],
                        session_id=session_id, volunteer_id=entry["volunteer_id"],
                        assignment_id=assignment["id"])
            promotions.append({"waitlist_entry_id": entry["id"],
                               "volunteer_id": entry["volunteer_id"],
                               "assignment_id": assignment["id"]})

    # ------------------------------------------------------------------
    # 过期暂占清理（启动恢复 + 定时清理共用）
    # ------------------------------------------------------------------
    def reap_expired_holds(self, actor: Actor = SYSTEM_ACTOR) -> dict:
        """把过期暂占置为 expired 并释放名额，释放出的名额立即按序递补。"""
        self._require(actor, ROLE_OPERATOR, ROLE_SYSTEM)
        now = self._now()
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM holds WHERE status = 'active' AND expires_at <= ? ORDER BY rowid",
                (_iso(now),)).fetchall()
            expired: list[str] = []
            promotions: list[dict] = []
            session_ids: list[str] = []
            for hold in rows:
                conn.execute("UPDATE holds SET status = 'expired' WHERE id = ?", (hold["id"],))
                self._event(conn, actor, "hold_expired", "hold", hold["id"],
                            session_id=hold["session_id"], volunteer_id=hold["volunteer_id"])
                expired.append(hold["id"])
                if hold["session_id"] not in session_ids:
                    session_ids.append(hold["session_id"])
            for session_id in session_ids:
                promotions.extend(self._promote_waitlist(conn, session_id, actor, now))
            return {"expired_hold_ids": expired, "promotions": promotions}

    def recover(self) -> dict:
        """服务启动时调用：继续清理上次运行遗留的过期暂占。"""
        return self.reap_expired_holds(SYSTEM_ACTOR)

    # ------------------------------------------------------------------
    # 查询与解释
    # ------------------------------------------------------------------
    def get_volunteer(self, actor: Actor, volunteer_id: str) -> dict:
        conn = self.db.connect()
        try:
            volunteer = self._get(conn, "volunteers", volunteer_id, "志愿者")
            if actor.role == ROLE_GUARDIAN:
                if volunteer["guardian_id"] != actor.id:
                    raise DomainError("forbidden", "只能查看本人关联的未成年志愿者记录", 403)
            elif actor.role not in (ROLE_OPERATOR, ROLE_MANAGER):
                raise DomainError("forbidden", "当前角色无权查看志愿者档案", 403)
            return self._volunteer_dict(volunteer)
        finally:
            conn.close()

    def list_consents(self, actor: Actor, volunteer_id: str) -> dict:
        conn = self.db.connect()
        try:
            self._get(conn, "volunteers", volunteer_id, "志愿者")
            if actor.role == ROLE_GUARDIAN:
                self._require_guardian_scope(conn, actor, volunteer_id)
            else:
                self._require(actor, ROLE_OPERATOR, ROLE_MANAGER)
            rows = conn.execute(
                "SELECT * FROM consent_versions WHERE volunteer_id = ? ORDER BY version_no",
                (volunteer_id,)).fetchall()
            return {"consents": [self._consent_dict(r) for r in rows]}
        finally:
            conn.close()

    def guardian_view(self, actor: Actor, guardian_id: str) -> dict:
        """监护人视角：只返回该监护人名下志愿者的授权、排班、候补与暂占。"""
        if actor.role == ROLE_GUARDIAN and actor.id != guardian_id:
            raise DomainError("forbidden", "监护人只能查看本人关联的记录", 403)
        if actor.role not in (ROLE_GUARDIAN, ROLE_OPERATOR):
            raise DomainError("forbidden", "当前角色无权查看监护人档案", 403)
        conn = self.db.connect()
        try:
            guardian = self._get(conn, "guardians", guardian_id, "监护人")
            volunteers = []
            for v in conn.execute(
                    "SELECT * FROM volunteers WHERE guardian_id = ? ORDER BY rowid",
                    (guardian_id,)).fetchall():
                assignments = []
                for a in conn.execute(
                        "SELECT * FROM assignments WHERE volunteer_id = ? ORDER BY rowid",
                        (v["id"],)).fetchall():
                    item = self._assignment_dict(a)
                    s = self._get(conn, "sessions", a["session_id"], "场次")
                    item["session"] = {"id": s["id"], "venue_id": s["venue_id"],
                                       "starts_at": s["starts_at"], "ends_at": s["ends_at"]}
                    assignments.append(item)
                volunteers.append({
                    **self._volunteer_dict(v),
                    "consents": [self._consent_dict(r) for r in conn.execute(
                        "SELECT * FROM consent_versions WHERE volunteer_id = ? ORDER BY version_no",
                        (v["id"],)).fetchall()],
                    "trainings": [self._training_dict(r) for r in conn.execute(
                        "SELECT * FROM trainings WHERE volunteer_id = ? ORDER BY rowid",
                        (v["id"],)).fetchall()],
                    "assignments": assignments,
                    "waitlist": [self._waitlist_dict(r) for r in conn.execute(
                        "SELECT * FROM waitlist_entries WHERE volunteer_id = ? ORDER BY rowid",
                        (v["id"],)).fetchall()],
                    "holds": [self._hold_dict(r) for r in conn.execute(
                        "SELECT * FROM holds WHERE volunteer_id = ? ORDER BY rowid",
                        (v["id"],)).fetchall()],
                })
            return {"guardian": dict(guardian), "volunteers": volunteers}
        finally:
            conn.close()

    def session_view(self, actor: Actor, session_id: str) -> dict:
        """运营视角：名额、名单、暂占、候补队列与近期事件。"""
        self._require(actor, ROLE_OPERATOR, ROLE_MANAGER)
        conn = self.db.connect()
        try:
            session = self._get(conn, "sessions", session_id, "场次")
            confirmed, active_holds = self._capacity_usage(conn, session_id, _iso(self._now()))
            roster = []
            for a in conn.execute(
                    "SELECT * FROM assignments WHERE session_id = ? AND status = 'confirmed'"
                    " ORDER BY rowid", (session_id,)).fetchall():
                volunteer = self._get(conn, "volunteers", a["volunteer_id"], "志愿者")
                consent = self._get(conn, "consent_versions", a["consent_version_id"], "监护授权")
                roster.append({
                    "assignment_id": a["id"],
                    "volunteer_id": a["volunteer_id"],
                    "volunteer_name": volunteer["name"],
                    "consent_version_no": consent["version_no"],
                    "source": a["source"],
                    "created_at": a["created_at"],
                })
            waiting = conn.execute(
                "SELECT COUNT(*) FROM waitlist_entries WHERE session_id = ? AND status = 'waiting'",
                (session_id,)).fetchone()[0]
            waitlist = []
            for entry in conn.execute(
                    "SELECT * FROM waitlist_entries WHERE session_id = ? ORDER BY seq",
                    (session_id,)).fetchall():
                item = self._waitlist_dict(entry)
                volunteer = conn.execute("SELECT name FROM volunteers WHERE id = ?",
                                         (entry["volunteer_id"],)).fetchone()
                item["volunteer_name"] = volunteer["name"] if volunteer else None
                waitlist.append(item)
            events = conn.execute(
                "SELECT * FROM events WHERE (entity_type = 'session' AND entity_id = ?)"
                " OR json_extract(detail, '$.session_id') = ? ORDER BY id DESC LIMIT 20",
                (session_id, session_id)).fetchall()
            return {
                "session": self._session_dict(session),
                "usage": {"capacity": session["capacity"], "confirmed": confirmed,
                          "active_holds": active_holds, "waiting": waiting},
                "roster": roster,
                "holds": [self._hold_dict(r) for r in conn.execute(
                    "SELECT * FROM holds WHERE session_id = ? ORDER BY rowid",
                    (session_id,)).fetchall()],
                "waitlist": waitlist,
                "recent_events": [self._event_dict(r) for r in events],
            }
        finally:
            conn.close()

    def explain_assignment(self, actor: Actor, assignment_id: str) -> dict:
        """解释一条排班为何有效：依据确认时保存的快照逐条给出理由。"""
        conn = self.db.connect()
        try:
            assignment = self._get(conn, "assignments", assignment_id, "排班")
            if actor.role == ROLE_GUARDIAN:
                self._require_guardian_scope(conn, actor, assignment["volunteer_id"])
            elif actor.role not in (ROLE_OPERATOR, ROLE_MANAGER):
                raise DomainError("forbidden", "当前角色无权查看排班解释", 403)
            snap = json.loads(assignment["snapshot"])
            consent_snap = snap["consent_version"]
            reasons = [
                f"确认时间 {snap['confirmed_at']} 固定监护授权版本 v{consent_snap['version_no']}"
                f"（{consent_snap['id']}），当时处于生效状态，授权范围 "
                f"{'、'.join(consent_snap['venue_scope'])} 覆盖场次场馆 {snap['session']['venue_id']}",
            ]
            required = snap["qualifications"]["required"]
            if required:
                reasons.append(
                    f"场次要求培训资格 {'、'.join(required)}，志愿者确认时已持有 "
                    f"{'、'.join(snap['qualifications']['held']) or '（无）'}")
            else:
                reasons.append("本场次无额外培训资格要求")
            cap = snap["capacity_before"]
            reasons.append(
                f"确认前名额占用：容量 {snap['session']['capacity']}，已确认 {cap['confirmed']}，"
                f"有效暂占 {cap['active_holds']}，名额充足")
            reasons.append(
                f"志愿者出生于 {snap['volunteer']['birth_date']}，场次开始时 "
                f"{snap['volunteer']['age_at_session_start']} 岁，符合未成年人要求")
            source = snap["source"]
            if source["kind"] == "manual":
                text = "来源：运营员直接确认"
                if source.get("hold_id"):
                    text += f"（使用暂占 {source['hold_id']}）"
                reasons.append(text)
            elif source["kind"] == "waitlist":
                reasons.append(f"来源：候补递补（队列序号 {source['waitlist_seq']}）")
            elif source["kind"] == "transfer":
                reasons.append(f"来源：跨馆调班（原排班 {source['transferred_from']}）")
            issues = []
            consent_now = self._get(conn, "consent_versions",
                                    assignment["consent_version_id"], "监护授权")
            if consent_now["status"] == "withdrawn":
                issues.append(
                    f"固定的授权版本 v{consent_now['version_no']} 已于 "
                    f"{consent_now['withdrawn_at']} 被撤回")
            elif consent_now["status"] == "superseded":
                issues.append(
                    f"固定的授权版本 v{consent_now['version_no']} 已被更新版本替代"
                    "（不影响本次确认在当时的有效性）")
            if assignment["status"] == "cancelled":
                reason_text = CANCEL_REASON_TEXT.get(
                    assignment["cancel_reason"], assignment["cancel_reason"] or "未知原因")
                issues.append(f"排班已取消：{reason_text}")
            return {
                "assignment_id": assignment_id,
                "status": assignment["status"],
                "valid": assignment["status"] == "confirmed",
                "reasons": reasons,
                "current_issues": issues,
                "snapshot": snap,
            }
        finally:
            conn.close()
