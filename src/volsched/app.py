"""HTTP 服务：路由、令牌认证、角色鉴权与监护人数据隔离。

仅依赖标准库（``http.server``）。写操作最终都进入
``Database.write()`` 的 ``BEGIN IMMEDIATE`` 临界区，因此多线程并发
（``ThreadingHTTPServer``）下名额与候补结果仍然确定。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit, parse_qs

from .clock import Clock
from .consents import ConsentService
from .db import Database
from .errors import DomainError
from .identity import (
    IdentityService,
    ROLE_GUARDIAN,
    ROLE_OPERATOR,
    ROLE_VENUE_MANAGER,
    ROLE_VOLUNTEER,
)
from .scheduling import SchedulingService

Handler = Callable[["AppState", dict[str, Any], dict[str, Any], dict[str, list[str]]], Any]


class AppState:
    def __init__(self, db_path: str = ":memory:", clock: Clock | None = None,
                 hold_seconds: int = 120, sweep_interval: float = 30.0) -> None:
        self.clock = clock or Clock()
        self.db = Database(db_path)
        self.identities = IdentityService(self.db, self.clock)
        self.consents = ConsentService(self.db, self.identities, self.clock)
        self.scheduling = SchedulingService(
            self.db, self.identities, self.consents, self.clock,
            default_hold_seconds=hold_seconds,
        )
        self.tokens: dict[str, str] = {}  # token -> account_id
        self.sweep_interval = sweep_interval
        self._sweeper: threading.Thread | None = None
        self._stopping = threading.Event()

    # ---------- 令牌 ----------

    def issue_token(self, account_id: str) -> str:
        self.identities.require_account(account_id)
        token = secrets.token_urlsafe(24)
        self.tokens[token] = account_id
        return token

    def account_for_token(self, token: str) -> dict[str, Any] | None:
        account_id = self.tokens.get(token)
        return self.identities.get_account(account_id) if account_id else None

    # ---------- 过期暂占清理 ----------

    def startup_sweep(self) -> None:
        """服务启动即清理：重启后未提交事务不会残留，已到期暂占立即释放。"""
        self.scheduling.sweep_expired_holds(actor_id="system:startup")

    def start_background_sweeper(self) -> None:
        if self.sweep_interval <= 0:
            return

        def loop() -> None:
            while not self._stopping.wait(self.sweep_interval):
                try:
                    self.scheduling.sweep_expired_holds(actor_id="system:sweeper")
                except Exception:  # noqa: BLE001 - 后台任务不能因单次错误退出
                    pass

        self._sweeper = threading.Thread(target=loop, name="hold-sweeper", daemon=True)
        self._sweeper.start()

    def close(self) -> None:
        self._stopping.set()
        self.db.close()


# ----------------------------------------------------------------------
# 鉴权辅助
# ----------------------------------------------------------------------


def require_roles(account: dict[str, Any], *roles: str) -> None:
    if account["role"] not in roles:
        raise DomainError.forbidden(
            "FORBIDDEN", f"该操作允许的角色：{'、'.join(roles)}"
        )


def body_get(body: dict[str, Any], key: str, required: bool = True,
             default: Any = None) -> Any:
    if key not in body or body[key] in (None, ""):
        if required:
            raise DomainError.validation("MISSING_FIELD", f"缺少字段：{key}")
        return default
    return body[key]


def volunteer_or_operator(
    state: AppState, account: dict[str, Any], volunteer_id: str
) -> None:
    """志愿者账号只能操作绑定到自己的志愿者档案。"""
    if account["role"] == ROLE_OPERATOR:
        return
    if account["role"] == ROLE_VOLUNTEER:
        volunteer = state.identities.require_volunteer(volunteer_id)
        if volunteer["account_id"] != account["id"]:
            raise DomainError.forbidden("NOT_OWN_PROFILE", "只能操作本人志愿者档案")
        return
    raise DomainError.forbidden("FORBIDDEN", "运营员或志愿者本人才能执行该操作")


def ensure_guardianship(state: AppState, account: dict[str, Any], volunteer_id: str) -> None:
    state.identities.require_guardianship(volunteer_id, account["id"])


def manager_venue_check(
    state: AppState, account: dict[str, Any], venue_id: str
) -> None:
    if account["role"] == ROLE_OPERATOR:
        return
    if account["role"] == ROLE_VENUE_MANAGER:
        if venue_id not in state.identities.venues_managed_by(account["id"]):
            raise DomainError.forbidden("VENUE_OUT_OF_SCOPE", "场馆负责人只能管理所属场馆")
        return
    raise DomainError.forbidden("FORBIDDEN", "运营员或所属场馆负责人才能执行该操作")


# ----------------------------------------------------------------------
# 路由处理函数
# ----------------------------------------------------------------------


def h_bootstrap(state: AppState, account, body, query) -> Any:
    count = state.db.query_one("SELECT COUNT(*) AS c FROM accounts")["c"]
    if count > 0:
        raise DomainError.conflict("ALREADY_INITIALIZED", "系统已初始化，禁止再使用引导接口")
    acct = state.identities.create_account(
        ROLE_OPERATOR, body_get(body, "display_name"))
    token = state.issue_token(acct["id"])
    return {"account": acct, "token": token}


def h_auth_token(state: AppState, account, body, query) -> Any:
    account_id = body_get(body, "account_id")
    token = state.issue_token(account_id)
    return {"token": token, "account": state.identities.require_account(account_id)}


def h_create_account(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    return state.identities.create_account(
        body_get(body, "role"), body_get(body, "display_name"))


def h_create_venue(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    return state.identities.create_venue(body_get(body, "name"), actor_id=account["id"])


def h_assign_manager(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    state.identities.assign_venue_manager(
        body_get(body, "account_id"), body_get(body, "venue_id"))
    return {"ok": True}


def h_create_volunteer(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    return state.identities.create_volunteer(
        body_get(body, "name"),
        body_get(body, "birth_date"),
        account_id=body_get(body, "account_id", required=False),
    )


def h_add_guardianship(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    return state.identities.add_guardianship(
        body_get(body, "volunteer_id"),
        body_get(body, "guardian_account_id"),
        body_get(body, "relation"),
    )


def h_record_training(state: AppState, account, body, query) -> Any:
    venue_id = body_get(body, "venue_id")
    manager_venue_check(state, account, venue_id)
    return state.identities.record_training(
        body_get(body, "volunteer_id"),
        venue_id,
        body_get(body, "status"),
        expires_at=body_get(body, "expires_at", required=False),
    )


def h_create_session(state: AppState, account, body, query) -> Any:
    venue_id = body_get(body, "venue_id")
    manager_venue_check(state, account, venue_id)
    return state.identities.create_session(
        venue_id,
        body_get(body, "title"),
        body_get(body, "starts_at"),
        body_get(body, "ends_at"),
        int(body_get(body, "capacity")),
        actor_id=account["id"],
    )


def h_close_session(state: AppState, account, body, query) -> Any:
    session = state.identities.require_session(query["id"][0])
    manager_venue_check(state, account, session["venue_id"])
    return state.scheduling.close_session(query["id"][0], actor_id=account["id"])


def h_issue_consent(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_GUARDIAN, ROLE_OPERATOR)
    volunteer_id = body_get(body, "volunteer_id")
    if account["role"] == ROLE_GUARDIAN:
        ensure_guardianship(state, account, volunteer_id)
    venue_ids = body_get(body, "venue_ids")
    if not isinstance(venue_ids, list) or not venue_ids:
        raise DomainError.validation("INVALID_SCOPE", "venue_ids 必须是非空数组")
    return state.consents.issue(
        volunteer_id,
        account["id"] if account["role"] == ROLE_GUARDIAN
        else body_get(body, "guardian_account_id"),
        [str(v) for v in venue_ids],
        statement=body_get(body, "statement", required=False, default=""),
        actor_id=account["id"],
    )


def h_revoke_consent(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_GUARDIAN)
    volunteer_id = body_get(body, "volunteer_id")
    ensure_guardianship(state, account, volunteer_id)
    return state.consents.revoke(
        volunteer_id, account["id"],
        reason=body_get(body, "reason", required=False, default=""),
        actor_id=account["id"],
    )


def h_guardian_consents(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_GUARDIAN)
    volunteer_id = query.get("volunteer_id", [None])[0]
    result: dict[str, Any] = {}
    for vid in state.identities.volunteers_for_guardian(account["id"]):
        if volunteer_id and vid != volunteer_id:
            continue
        result[vid] = state.consents.list_for_guardian(vid, account["id"])
    return {"consents": result}


def h_request_slot(state: AppState, account, body, query) -> Any:
    volunteer_id = body_get(body, "volunteer_id")
    volunteer_or_operator(state, account, volunteer_id)
    hold = body_get(body, "hold_seconds", required=False)
    return state.scheduling.request_slot(
        body_get(body, "session_id"), volunteer_id,
        hold_seconds=int(hold) if hold is not None else None,
        actor_id=account["id"],
    )


def _load_assignment_authorized(
    state: AppState, account: dict[str, Any], assignment_id: str,
    *,
    actor_roles: tuple[str, ...] = (
        ROLE_OPERATOR, ROLE_VOLUNTEER, ROLE_GUARDIAN, ROLE_VENUE_MANAGER),
) -> dict[str, Any]:
    assignment = state.scheduling.require_assignment(assignment_id)
    if account["role"] not in actor_roles:
        raise DomainError.forbidden("FORBIDDEN", "当前角色无权操作该安排")
    if account["role"] == ROLE_OPERATOR:
        return assignment
    if account["role"] == ROLE_VOLUNTEER:
        volunteer_or_operator(state, account, assignment["volunteer_id"])
        return assignment
    if account["role"] == ROLE_GUARDIAN:
        ensure_guardianship(state, account, assignment["volunteer_id"])
        return assignment
    if account["role"] == ROLE_VENUE_MANAGER:
        session = state.identities.require_session(assignment["session_id"])
        manager_venue_check(state, account, session["venue_id"])
        return assignment
    raise DomainError.forbidden()


def h_confirm(state: AppState, account, body, query) -> Any:
    # 排班确认的动作方：运营员、志愿者本人或所属场馆负责人；监护人只读。
    assignment = _load_assignment_authorized(
        state, account, query["id"][0],
        actor_roles=(ROLE_OPERATOR, ROLE_VOLUNTEER, ROLE_VENUE_MANAGER))
    return state.scheduling.confirm_assignment(assignment["id"], actor_id=account["id"])


def h_cancel(state: AppState, account, body, query) -> Any:
    assignment = _load_assignment_authorized(
        state, account, query["id"][0],
        actor_roles=(ROLE_OPERATOR, ROLE_VOLUNTEER, ROLE_VENUE_MANAGER))
    return state.scheduling.cancel_assignment(
        assignment["id"],
        reason=body_get(body, "reason", required=False, default=""),
        actor_id=account["id"],
    )


def h_transfer(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    return state.scheduling.transfer(
        query["id"][0], body_get(body, "target_session_id"),
        actor_id=account["id"],
    )


def h_cancel_waitlist(state: AppState, account, body, query) -> Any:
    entry = state.scheduling.get_waitlist_entry(query["id"][0])
    if entry is None:
        raise DomainError.not_found("WAITLIST_NOT_FOUND", "候补记录不存在")
    if account["role"] == ROLE_VOLUNTEER:
        volunteer_or_operator(state, account, entry["volunteer_id"])
    elif account["role"] == ROLE_GUARDIAN:
        ensure_guardianship(state, account, entry["volunteer_id"])
    elif account["role"] != ROLE_OPERATOR:
        raise DomainError.forbidden()
    return state.scheduling.cancel_waitlist_entry(entry["id"], actor_id=account["id"])


def h_promote(state: AppState, account, body, query) -> Any:
    session = state.identities.require_session(query["id"][0])
    manager_venue_check(state, account, session["venue_id"])
    return state.scheduling.promote_now(session["id"], actor_id=account["id"])


def h_sweep(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR)
    return state.scheduling.sweep_expired_holds(actor_id=account["id"])


def h_session_status(state: AppState, account, body, query) -> Any:
    session_id = query["id"][0]
    session = state.identities.require_session(session_id)
    if account["role"] == ROLE_VENUE_MANAGER:
        manager_venue_check(state, account, session["venue_id"])
    data = state.scheduling.session_status(session_id)
    # 候补名单含志愿者身份信息：监护人和志愿者只能看到与自己相关的条目。
    if account["role"] == ROLE_GUARDIAN:
        allowed = set(state.identities.volunteers_for_guardian(account["id"]))
        data["waitlist"] = [w for w in data["waitlist"]
                            if w["volunteer_id"] in allowed]
        data["waitlist_note"] = "仅展示本人关联志愿者"
        data.pop("head_blocker", None)
    elif account["role"] == ROLE_VOLUNTEER:
        me = state.db.query_one(
            "SELECT id FROM volunteers WHERE account_id = ?", (account["id"],))
        mine = {me["id"]} if me else set()
        data["waitlist"] = [w for w in data["waitlist"]
                            if w["volunteer_id"] in mine]
        data["waitlist_note"] = "仅展示本人记录"
        data.pop("head_blocker", None)
    return data


def h_get_assignment(state: AppState, account, body, query) -> Any:
    return _load_assignment_authorized(state, account, query["id"][0])


def h_explain(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR, ROLE_VENUE_MANAGER)
    assignment = state.scheduling.require_assignment(query["id"][0])
    if account["role"] == ROLE_VENUE_MANAGER:
        session = state.identities.require_session(assignment["session_id"])
        manager_venue_check(state, account, session["venue_id"])
    return state.scheduling.explain_assignment(query["id"][0])


def h_list_assignments(state: AppState, account, body, query) -> Any:
    session_id = query.get("session_id", [None])[0]
    volunteer_id = query.get("volunteer_id", [None])[0]
    if account["role"] == ROLE_GUARDIAN:
        # 监护人只能看到与本人关联的志愿者
        allowed = set(state.identities.volunteers_for_guardian(account["id"]))
        if volunteer_id:
            if volunteer_id not in allowed:
                raise DomainError.forbidden(
                    "NOT_GUARDIAN", "只能查看本人关联志愿者的安排")
        rows = state.scheduling.list_assignments(
            session_id=session_id, volunteer_id=volunteer_id)
        return {"assignments": [r for r in rows if r["volunteer_id"] in allowed]}
    if account["role"] == ROLE_VOLUNTEER:
        if volunteer_id:
            volunteer_or_operator(state, account, volunteer_id)
        else:
            me = state.db.query_one(
                "SELECT id FROM volunteers WHERE account_id = ?", (account["id"],))
            if me is None:
                raise DomainError.not_found("VOLUNTEER_PROFILE_NOT_FOUND",
                                            "该账号未绑定志愿者档案")
            volunteer_id = me["id"]
        return {"assignments": state.scheduling.list_assignments(
            session_id=session_id, volunteer_id=volunteer_id)}
    if account["role"] == ROLE_VENUE_MANAGER:
        managed = set(state.identities.venues_managed_by(account["id"]))
        rows = state.scheduling.list_assignments(
            session_id=session_id, volunteer_id=volunteer_id)
        return {"assignments": [r for r in rows if r["venue_id"] in managed]}
    return {"assignments": state.scheduling.list_assignments(
        session_id=session_id, volunteer_id=volunteer_id)}


def h_guardian_waitlist(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_GUARDIAN)
    allowed = set(state.identities.volunteers_for_guardian(account["id"]))
    out: list[dict[str, Any]] = []
    for vid in allowed:
        for row in state.db.query_all(
            "SELECT w.*, s.venue_id, s.title AS session_title FROM waitlist_entries w "
            "JOIN sessions s ON s.id = w.session_id "
            "WHERE w.volunteer_id = ? AND w.status = 'waiting' ORDER BY w.seq",
            (vid,),
        ):
            out.append(dict(row))
    return {"waitlist": out}


def h_events(state: AppState, account, body, query) -> Any:
    require_roles(account, ROLE_OPERATOR, ROLE_VENUE_MANAGER)
    session_id = query.get("session_id", [None])[0]
    volunteer_id = query.get("volunteer_id", [None])[0]
    assignment_id = query.get("assignment_id", [None])[0]
    rows = state.scheduling.events(
        limit=int(query.get("limit", ["100"])[0]),
        session_id=session_id, volunteer_id=volunteer_id, assignment_id=assignment_id)
    if account["role"] == ROLE_VENUE_MANAGER:
        managed = set(state.identities.venues_managed_by(account["id"]))

        def in_managed(row: dict[str, Any]) -> bool:
            if not row["session_id"]:
                return False
            session = state.identities.get_session(row["session_id"])
            return session is not None and session["venue_id"] in managed

        rows = [r for r in rows if in_managed(r)]
    return {"events": rows}


# (method, regex, function)
ROUTES: list[tuple[str, re.Pattern[str], Handler, bool]] = [
    ("POST", re.compile(r"^/api/setup/bootstrap$"), h_bootstrap, False),
    ("POST", re.compile(r"^/api/auth/token$"), h_auth_token, False),
    ("POST", re.compile(r"^/api/accounts$"), h_create_account, True),
    ("POST", re.compile(r"^/api/venues$"), h_create_venue, True),
    ("POST", re.compile(r"^/api/venue-managers$"), h_assign_manager, True),
    ("POST", re.compile(r"^/api/volunteers$"), h_create_volunteer, True),
    ("POST", re.compile(r"^/api/guardianships$"), h_add_guardianship, True),
    ("POST", re.compile(r"^/api/training$"), h_record_training, True),
    ("POST", re.compile(r"^/api/sessions$"), h_create_session, True),
    ("POST", re.compile(r"^/api/sessions/(?P<id>[^/]+)/close$"), h_close_session, True),
    ("POST", re.compile(r"^/api/consents$"), h_issue_consent, True),
    ("POST", re.compile(r"^/api/consents/revoke$"), h_revoke_consent, True),
    ("GET", re.compile(r"^/api/guardian/consents$"), h_guardian_consents, True),
    ("POST", re.compile(r"^/api/slots$"), h_request_slot, True),
    ("POST", re.compile(r"^/api/assignments/(?P<id>[^/]+)/confirm$"), h_confirm, True),
    ("POST", re.compile(r"^/api/assignments/(?P<id>[^/]+)/cancel$"), h_cancel, True),
    ("POST", re.compile(r"^/api/assignments/(?P<id>[^/]+)/transfer$"), h_transfer, True),
    ("GET", re.compile(r"^/api/assignments/(?P<id>[^/]+)$"), h_get_assignment, True),
    ("GET", re.compile(r"^/api/assignments/(?P<id>[^/]+)/explain$"), h_explain, True),
    ("GET", re.compile(r"^/api/assignments$"), h_list_assignments, True),
    ("POST", re.compile(r"^/api/waitlist/(?P<id>[^/]+)/cancel$"), h_cancel_waitlist, True),
    ("GET", re.compile(r"^/api/guardian/waitlist$"), h_guardian_waitlist, True),
    ("POST", re.compile(r"^/api/sessions/(?P<id>[^/]+)/promote$"), h_promote, True),
    ("GET", re.compile(r"^/api/sessions/(?P<id>[^/]+)/status$"), h_session_status, True),
    ("POST", re.compile(r"^/api/maintenance/sweep$"), h_sweep, True),
    ("GET", re.compile(r"^/api/events$"), h_events, True),
]


class ApiHandler(BaseHTTPRequestHandler):
    state: AppState  # 由工厂函数注入到类上

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        pass

    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authenticate(self) -> dict[str, Any] | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return self.state.account_for_token(header[len("Bearer "):].strip())

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        match_handler: Handler | None = None
        for route_method, pattern, fn, auth_required in ROUTES:
            m = pattern.match(path)
            if route_method == method and m:
                match_handler = fn
                query.update({k: [v] for k, v in m.groupdict().items()})
                self._auth_required = auth_required  # type: ignore[attr-defined]
                break
        if match_handler is None:
            self._send_json(404, {"error": {"code": "NOT_FOUND", "message": f"无此接口：{method} {path}"}})
            return
        try:
            account = None
            if self._auth_required:  # type: ignore[attr-defined]
                account = self._authenticate()
                if account is None:
                    raise DomainError.unauthorized()
            body: dict[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise DomainError.validation("INVALID_JSON", "请求体不是合法 JSON") from exc
                if not isinstance(body, dict):
                    raise DomainError.validation("INVALID_BODY", "请求体必须是 JSON 对象")
            result = match_handler(self.state, account, body, query)
            self._send_json(200, {"ok": True, "data": result})
        except DomainError as exc:
            self._send_json(exc.http_status, exc.to_body())

    def do_GET(self) -> None:  # noqa: N802
        self._auth_required = False  # type: ignore[attr-defined]
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._auth_required = False  # type: ignore[attr-defined]
        self._handle("POST")


def build_server(host: str, port: int, state: AppState) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
