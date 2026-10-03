"""HTTP 接口层测试：身份隔离、幂等重放与重启恢复。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from helpers import START  # noqa: E402

from volunteer_scheduling.api import make_server  # noqa: E402
from volunteer_scheduling.clock import ManualClock  # noqa: E402
from volunteer_scheduling.service import SchedulingService  # noqa: E402

OP_HEADERS = {"X-Actor-Role": "operator", "X-Actor-Id": "op-1"}


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "api.db")
        self.clock = ManualClock(START)
        self.start_server()

    def tearDown(self) -> None:
        self.stop_server()

    def start_server(self) -> None:
        self.service = SchedulingService(self.db_path, clock=self.clock, hold_ttl_seconds=60)
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body or {}).encode("utf-8")
        all_headers = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, payload, all_headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def op(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        return self.call(method, path, body, OP_HEADERS)

    def seed_confirmed_assignment(self) -> tuple[str, str, str]:
        """造一名志愿者 + 授权 + 场次 + 确认，返回 (volunteer_id, session_id, assignment_id)。"""
        self.op("POST", "/venues", {"name": "甲展厅", "venue_id": "hall-a"})
        self.op("POST", "/guardians", {"name": "监护人一", "guardian_id": "g1"})
        _, vol = self.op("POST", "/volunteers",
                         {"name": "小明", "birth_date": "2012-03-04", "guardian_id": "g1"})
        vid = vol["volunteer"]["id"]
        self.op("POST", f"/volunteers/{vid}/consents", {"venue_scope": ["hall-a"]})
        _, ses = self.op("POST", "/sessions", {
            "venue_id": "hall-a", "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T12:00:00+00:00", "capacity": 1})
        sid = ses["session"]["id"]
        _, conf = self.op("POST", f"/sessions/{sid}/confirmations",
                          {"volunteer_id": vid, "idempotency_key": "k-1"})
        return vid, sid, conf["assignment"]["id"]


class ApiFlowTests(ApiTestCase):
    def test_end_to_end_flow(self) -> None:
        vid, sid, aid = self.seed_confirmed_assignment()
        status, view = self.op("GET", f"/sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(view["usage"]["confirmed"], 1)
        self.assertEqual(view["roster"][0]["volunteer_id"], vid)
        status, explain = self.op("GET", f"/assignments/{aid}/explain")
        self.assertEqual(status, 200)
        self.assertTrue(explain["valid"])
        self.assertTrue(any("v1" in r for r in explain["reasons"]))

    def test_health_needs_no_auth(self) -> None:
        status, data = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_missing_identity_headers_rejected(self) -> None:
        status, data = self.call("GET", "/sessions/whatever")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "unauthorized")

    def test_unknown_role_rejected(self) -> None:
        status, data = self.call("GET", "/sessions/whatever",
                                 headers={"X-Actor-Role": "admin", "X-Actor-Id": "x"})
        self.assertEqual(status, 401)

    def test_guardian_cannot_mutate(self) -> None:
        status, data = self.call("POST", "/venues", {"name": "丙展厅"},
                                 {"X-Actor-Role": "guardian", "X-Actor-Id": "g1"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "forbidden")

    def test_guardian_view_is_scoped(self) -> None:
        self.op("POST", "/venues", {"name": "甲展厅", "venue_id": "hall-a"})
        self.op("POST", "/guardians", {"name": "监护人一", "guardian_id": "g1"})
        self.op("POST", "/guardians", {"name": "监护人二", "guardian_id": "g2"})
        _, v1 = self.op("POST", "/volunteers",
                        {"name": "小明", "birth_date": "2012-03-04", "guardian_id": "g1"})
        _, v2 = self.op("POST", "/volunteers",
                        {"name": "小红", "birth_date": "2012-03-04", "guardian_id": "g2"})
        g1_headers = {"X-Actor-Role": "guardian", "X-Actor-Id": "g1"}
        status, own = self.call("GET", "/guardians/g1/view", headers=g1_headers)
        self.assertEqual(status, 200)
        self.assertEqual([v["id"] for v in own["volunteers"]], [v1["volunteer"]["id"]])
        status, data = self.call("GET", "/guardians/g2/view", headers=g1_headers)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "forbidden")

    def test_duplicate_confirm_replay_over_http(self) -> None:
        self.op("POST", "/venues", {"name": "甲展厅", "venue_id": "hall-a"})
        self.op("POST", "/guardians", {"name": "监护人一", "guardian_id": "g1"})
        _, vol = self.op("POST", "/volunteers",
                         {"name": "小明", "birth_date": "2012-03-04", "guardian_id": "g1"})
        vid = vol["volunteer"]["id"]
        self.op("POST", f"/volunteers/{vid}/consents", {"venue_scope": ["hall-a"]})
        _, ses = self.op("POST", "/sessions", {
            "venue_id": "hall-a", "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T12:00:00+00:00", "capacity": 1})
        sid = ses["session"]["id"]
        body = {"volunteer_id": vid, "idempotency_key": "k-1"}
        status1, first = self.op("POST", f"/sessions/{sid}/confirmations", body)
        status2, replay = self.op("POST", f"/sessions/{sid}/confirmations", body)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["assignment"]["id"], replay["assignment"]["id"])

    def test_error_shape_for_session_full(self) -> None:
        self.op("POST", "/venues", {"name": "甲展厅", "venue_id": "hall-a"})
        self.op("POST", "/guardians", {"name": "监护人一", "guardian_id": "g1"})
        ids = []
        for name in ("小明", "小红"):
            _, vol = self.op("POST", "/volunteers",
                             {"name": name, "birth_date": "2012-03-04", "guardian_id": "g1"})
            vid = vol["volunteer"]["id"]
            self.op("POST", f"/volunteers/{vid}/consents", {"venue_scope": ["hall-a"]})
            ids.append(vid)
        _, ses = self.op("POST", "/sessions", {
            "venue_id": "hall-a", "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T12:00:00+00:00", "capacity": 1})
        sid = ses["session"]["id"]
        self.op("POST", f"/sessions/{sid}/confirmations",
                {"volunteer_id": ids[0], "idempotency_key": "k-1"})
        status, data = self.op("POST", f"/sessions/{sid}/confirmations",
                               {"volunteer_id": ids[1], "idempotency_key": "k-2"})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "session_full")

    def test_withdraw_by_guardian_over_http(self) -> None:
        self.op("POST", "/venues", {"name": "甲展厅", "venue_id": "hall-a"})
        self.op("POST", "/guardians", {"name": "监护人一", "guardian_id": "g1"})
        self.op("POST", "/guardians", {"name": "监护人二", "guardian_id": "g2"})
        _, vol = self.op("POST", "/volunteers",
                         {"name": "小明", "birth_date": "2012-03-04", "guardian_id": "g1"})
        vid = vol["volunteer"]["id"]
        _, consent = self.op("POST", f"/volunteers/{vid}/consents", {"venue_scope": ["hall-a"]})
        cid = consent["consent"]["id"]
        stranger = {"X-Actor-Role": "guardian", "X-Actor-Id": "g2"}
        status, _ = self.call("POST", f"/consents/{cid}/withdraw", headers=stranger)
        self.assertEqual(status, 403)
        owner = {"X-Actor-Role": "guardian", "X-Actor-Id": "g1"}
        status, data = self.call("POST", f"/consents/{cid}/withdraw", headers=owner)
        self.assertEqual(status, 200)
        self.assertEqual(data["consent"]["status"], "withdrawn")

    def test_restart_keeps_state_and_reaps_expired_holds(self) -> None:
        self.op("POST", "/venues", {"name": "甲展厅", "venue_id": "hall-a"})
        self.op("POST", "/guardians", {"name": "监护人一", "guardian_id": "g1"})
        _, vol = self.op("POST", "/volunteers",
                         {"name": "小明", "birth_date": "2012-03-04", "guardian_id": "g1"})
        vid = vol["volunteer"]["id"]
        self.op("POST", f"/volunteers/{vid}/consents", {"venue_scope": ["hall-a"]})
        _, ses = self.op("POST", "/sessions", {
            "venue_id": "hall-a", "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T12:00:00+00:00", "capacity": 1})
        sid = ses["session"]["id"]
        status, hold = self.op("POST", f"/sessions/{sid}/holds",
                               {"volunteer_id": vid, "idempotency_key": "h-1",
                                "ttl_seconds": 60})
        self.assertEqual(status, 201)
        self.clock.advance(seconds=120)
        # 重启：关闭服务，用同一数据库文件重建。
        self.stop_server()
        self.start_server()
        status, view = self.op("GET", f"/sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(view["holds"][0]["status"], "expired")
        # 名额已释放，可以直接确认。
        status, conf = self.op("POST", f"/sessions/{sid}/confirmations",
                               {"volunteer_id": vid, "idempotency_key": "k-1"})
        self.assertEqual(status, 201)
        self.assertEqual(conf["assignment"]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
