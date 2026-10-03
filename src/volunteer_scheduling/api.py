"""HTTP JSON 接口（仅标准库）。

身份通过请求头传递：``X-Actor-Role``（operator / guardian / manager）与
``X-Actor-Id``。授权规则在服务层强制执行，接口层只负责解析与转发。
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import DomainError
from .service import ROLE_GUARDIAN, ROLE_MANAGER, ROLE_OPERATOR, Actor, SchedulingService

Handler = Callable[..., tuple[dict, int]]


def _actor_from_headers(headers: Any) -> Actor:
    role = headers.get("X-Actor-Role")
    actor_id = headers.get("X-Actor-Id")
    if not role or not actor_id:
        raise DomainError("unauthorized", "缺少身份头 X-Actor-Role / X-Actor-Id", 401)
    if role not in (ROLE_OPERATOR, ROLE_GUARDIAN, ROLE_MANAGER):
        raise DomainError("unauthorized", f"未知角色：{role}", 401)
    return Actor(role, actor_id)


# ---------------------------------------------------------------------------
# 路由处理函数：(service, actor, body, **path_params) -> (payload, status)
# ---------------------------------------------------------------------------
def _health(service: SchedulingService, actor: Actor, body: dict) -> tuple[dict, int]:
    return {"status": "ok"}, 200


def _create_guardian(service, actor, body):
    return {"guardian": service.create_guardian(actor, body.get("name"), body.get("guardian_id"))}, 201


def _create_volunteer(service, actor, body):
    return {"volunteer": service.create_volunteer(
        actor, body.get("name"), body.get("birth_date"), body.get("guardian_id"))}, 201


def _get_volunteer(service, actor, body, volunteer_id):
    return {"volunteer": service.get_volunteer(actor, volunteer_id)}, 200


def _publish_consent(service, actor, body, volunteer_id):
    return {"consent": service.publish_consent(
        actor, volunteer_id, body.get("venue_scope"),
        body.get("valid_from"), body.get("valid_until"))}, 201


def _list_consents(service, actor, body, volunteer_id):
    return service.list_consents(actor, volunteer_id), 200


def _withdraw_consent(service, actor, body, consent_id):
    return service.withdraw_consent(actor, consent_id), 200


def _add_training(service, actor, body, volunteer_id):
    return {"training": service.add_training(
        actor, volunteer_id, body.get("qualification"),
        body.get("granted_at"), body.get("expires_at"))}, 201


def _create_venue(service, actor, body):
    return {"venue": service.create_venue(actor, body.get("name"), body.get("venue_id"))}, 201


def _create_session(service, actor, body):
    return {"session": service.create_session(
        actor, body.get("venue_id"), body.get("starts_at"), body.get("ends_at"),
        body.get("capacity"), body.get("required_qualifications"))}, 201


def _session_view(service, actor, body, session_id):
    return service.session_view(actor, session_id), 200


def _close_session(service, actor, body, session_id):
    return service.close_session(actor, session_id), 200


def _create_hold(service, actor, body, session_id):
    result = service.create_hold(
        actor, session_id, body.get("volunteer_id"),
        body.get("idempotency_key"), body.get("ttl_seconds"))
    return result, 200 if result.get("idempotent_replay") else 201


def _confirm(service, actor, body, session_id):
    result = service.confirm(
        actor, session_id, body.get("volunteer_id"),
        body.get("idempotency_key"), body.get("hold_id"))
    return result, 200 if result.get("idempotent_replay") else 201


def _join_waitlist(service, actor, body, session_id):
    return service.join_waitlist(actor, session_id, body.get("volunteer_id")), 201


def _cancel_waitlist(service, actor, body, entry_id):
    return service.cancel_waitlist(actor, entry_id), 200


def _cancel_assignment(service, actor, body, assignment_id):
    return service.cancel_assignment(actor, assignment_id, body.get("reason")), 200


def _transfer(service, actor, body, assignment_id):
    result = service.transfer(
        actor, assignment_id, body.get("target_session_id"), body.get("idempotency_key"))
    return result, 200 if result.get("idempotent_replay") else 201


def _explain(service, actor, body, assignment_id):
    return service.explain_assignment(actor, assignment_id), 200


def _guardian_view(service, actor, body, guardian_id):
    return service.guardian_view(actor, guardian_id), 200


def _reap(service, actor, body):
    return service.reap_expired_holds(actor), 200


ROUTES: list[tuple[str, re.Pattern, bool, Handler]] = [
    (method, re.compile(pattern), auth, handler)
    for method, pattern, auth, handler in [
        ("GET", r"/health", False, _health),
        ("POST", r"/guardians", True, _create_guardian),
        ("POST", r"/volunteers", True, _create_volunteer),
        ("GET", r"/volunteers/(?P<volunteer_id>[^/]+)", True, _get_volunteer),
        ("POST", r"/volunteers/(?P<volunteer_id>[^/]+)/consents", True, _publish_consent),
        ("GET", r"/volunteers/(?P<volunteer_id>[^/]+)/consents", True, _list_consents),
        ("POST", r"/volunteers/(?P<volunteer_id>[^/]+)/trainings", True, _add_training),
        ("POST", r"/consents/(?P<consent_id>[^/]+)/withdraw", True, _withdraw_consent),
        ("POST", r"/venues", True, _create_venue),
        ("POST", r"/sessions", True, _create_session),
        ("GET", r"/sessions/(?P<session_id>[^/]+)", True, _session_view),
        ("POST", r"/sessions/(?P<session_id>[^/]+)/close", True, _close_session),
        ("POST", r"/sessions/(?P<session_id>[^/]+)/holds", True, _create_hold),
        ("POST", r"/sessions/(?P<session_id>[^/]+)/confirmations", True, _confirm),
        ("POST", r"/sessions/(?P<session_id>[^/]+)/waitlist", True, _join_waitlist),
        ("POST", r"/waitlist/(?P<entry_id>[^/]+)/cancel", True, _cancel_waitlist),
        ("POST", r"/assignments/(?P<assignment_id>[^/]+)/cancel", True, _cancel_assignment),
        ("POST", r"/assignments/(?P<assignment_id>[^/]+)/transfer", True, _transfer),
        ("GET", r"/assignments/(?P<assignment_id>[^/]+)/explain", True, _explain),
        ("GET", r"/guardians/(?P<guardian_id>[^/]+)/view", True, _guardian_view),
        ("POST", r"/maintenance/reap", True, _reap),
    ]
]


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "VolunteerScheduling/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # 保持测试输出安静
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = self.path.split("?", 1)[0]
            for route_method, pattern, auth_required, handler in ROUTES:
                if route_method != method:
                    continue
                match = pattern.fullmatch(path)
                if not match:
                    continue
                actor = _actor_from_headers(self.headers) if auth_required else None
                body = self._read_json() if method == "POST" else {}
                payload, status = handler(self.server.service, actor, body, **match.groupdict())
                self._send_json(payload, status)
                return
            self._send_json({"error": {"code": "not_found", "message": "路由不存在"}}, 404)
        except DomainError as exc:
            self._send_json({"error": {"code": exc.code, "message": str(exc)}}, exc.status)
        except Exception as exc:  # 兜底：不暴露堆栈，但保留可诊断信息
            self._send_json({"error": {"code": "internal", "message": f"服务内部错误：{exc}"}}, 500)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("validation", "请求体不是合法 JSON", 400) from None
        if not isinstance(value, dict):
            raise DomainError("validation", "请求体必须是 JSON 对象", 400)
        return value

    def _send_json(self, payload: dict, status: int) -> None:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(service: SchedulingService, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _RequestHandler)
    server.daemon_threads = True
    server.service = service  # type: ignore[attr-defined]
    return server


def start_sweeper(service: SchedulingService, interval_seconds: float) -> threading.Thread:
    """后台定时清理过期暂占；崩溃恢复由启动时的 recover() 兜底。"""

    def _loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                service.reap_expired_holds()
            except Exception:
                pass

    thread = threading.Thread(target=_loop, name="hold-sweeper", daemon=True)
    thread.start()
    return thread
