"""HTTP 接口端到端测试：鉴权、监护人隔离、并发确定性、重启清理。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from volsched.app import AppState, build_server
from volsched.clock import FakeClock


_UNSET = object()


class ApiClient:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base = base
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None,
             token: str | None = _UNSET):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        tok = self.token if token is _UNSET else token
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path: str, **kw):
        return self.call("GET", path, None, **kw)

    def post(self, path: str, body=None, **kw):
        return self.call("POST", path, body or {}, **kw)


class ServerHarness:
    def __init__(self, tmp_path: str | None = None, hold_seconds: int = 60):
        self.clock = FakeClock()
        self.state = AppState(db_path=tmp_path or ":memory:", clock=self.clock,
                              hold_seconds=hold_seconds, sweep_interval=0)
        self.state.startup_sweep()
        self.httpd = build_server("127.0.0.1", 0, self.state)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.state.close()


class HttpLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ServerHarness()
        self.c = ApiClient(self.h.base)

    def tearDown(self) -> None:
        self.h.stop()

    def _seed(self):
        status, payload = self.c.post("/api/setup/bootstrap", {"display_name": "运营甲"})
        self.assertEqual(status, 200)
        op = payload["data"]["token"]
        status, payload = self.c.post("/api/venues", {"name": "陶瓷馆"}, token=op)
        venue = payload["data"]
        status, payload = self.c.post("/api/venues", {"name": "青铜馆"}, token=op)
        venue_b = payload["data"]
        status, payload = self.c.post("/api/accounts",
                                      {"role": "guardian", "display_name": "王监护"}, token=op)
        guardian_acct = payload["data"]
        gtoken = self.c.post("/api/auth/token",
                             {"account_id": guardian_acct["id"]})[1]["data"]["token"]
        status, payload = self.c.post("/api/volunteers",
                                      {"name": "小明", "birth_date": "2012-05-01"}, token=op)
        minor = payload["data"]
        self.c.post("/api/guardianships",
                    {"volunteer_id": minor["id"],
                     "guardian_account_id": guardian_acct["id"],
                     "relation": "父亲"}, token=op)
        self.c.post("/api/training",
                    {"volunteer_id": minor["id"], "venue_id": venue["id"],
                     "status": "passed"}, token=op)
        return op, gtoken, guardian_acct, minor, venue, venue_b

    def test_full_lifecycle_and_explanation(self) -> None:
        op, gtoken, guardian_acct, minor, venue, venue_b = self._seed()
        # 监护人签发只覆盖陶瓷馆的授权
        status, payload = self.c.post("/api/consents",
                                      {"volunteer_id": minor["id"], "venue_ids": [venue["id"]]},
                                      token=gtoken)
        self.assertEqual(status, 200)
        consent_v1 = payload["data"]["id"]

        status, payload = self.c.post("/api/sessions", {
            "venue_id": venue["id"], "title": "陶瓷讲解",
            "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T11:00:00+00:00", "capacity": 1}, token=op)
        sess = payload["data"]

        status, payload = self.c.post("/api/slots",
                                      {"session_id": sess["id"],
                                       "volunteer_id": minor["id"]}, token=gtoken)
        # 监护人不能替孩子报名
        self.assertEqual(status, 403)
        status, payload = self.c.post("/api/slots",
                                      {"session_id": sess["id"],
                                       "volunteer_id": minor["id"]}, token=op)
        self.assertEqual(status, 200, payload)
        held = payload["data"]
        self.assertEqual(held["state"], "held")
        # 监护人也不能执行确认（排班动作方仅限运营/志愿者本人/场馆负责人）
        self.assertEqual(
            self.c.post(f"/api/assignments/{held['id']}/confirm", token=gtoken)[0], 403)

        status, payload = self.c.post(f"/api/assignments/{held['id']}/confirm", {}, token=op)
        self.assertEqual(status, 200)
        confirmed = payload["data"]
        self.assertEqual(confirmed["consent_version_id"], consent_v1)

        # 重复确认：确定 409
        status, payload = self.c.post(f"/api/assignments/{held['id']}/confirm", {}, token=op)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "ALREADY_CONFIRMED")

        # 运营员解释视图
        status, payload = self.c.get(f"/api/assignments/{held['id']}/explain", token=op)
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["valid"])
        checks = {c["check"]: c for c in payload["data"]["why"]}
        self.assertTrue(checks["guardian_consent"]["detail"]["covers_service_venue"])

        # 撤回授权：已确认安排仍有效；监护人仍能看到该关联记录
        status, payload = self.c.post("/api/consents/revoke",
                                      {"volunteer_id": minor["id"], "reason": "改主意"},
                                      token=gtoken)
        self.assertEqual(status, 200)
        status, payload = self.c.post("/api/consents/revoke",
                                      {"volunteer_id": minor["id"]}, token=gtoken)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "NO_ACTIVE_CONSENT")
        status, payload = self.c.get(f"/api/assignments/{held['id']}/explain", token=op)
        self.assertTrue(payload["data"]["valid"])

    def test_guardian_sees_only_related_records(self) -> None:
        op, gtoken, guardian_acct, minor, venue, venue_b = self._seed()
        # 另一位监护人 + 另一名未成年志愿者
        other_g_acct = self.c.post("/api/accounts",
                                   {"role": "guardian", "display_name": "李监护"},
                                   token=op)[1]["data"]
        other_gtoken = self.c.post("/api/auth/token",
                                   {"account_id": other_g_acct["id"]})[1]["data"]["token"]
        other_minor = self.c.post("/api/volunteers",
                                  {"name": "小红", "birth_date": "2013-03-03"},
                                  token=op)[1]["data"]
        self.c.post("/api/guardianships",
                    {"volunteer_id": other_minor["id"],
                     "guardian_account_id": other_g_acct["id"], "relation": "母亲"},
                    token=op)
        self.c.post("/api/consents",
                    {"volunteer_id": minor["id"], "venue_ids": [venue["id"]]}, token=gtoken)
        self.c.post("/api/training",
                    {"volunteer_id": minor["id"], "venue_id": venue["id"],
                     "status": "passed"}, token=op)
        sess = self.c.post("/api/sessions", {
            "venue_id": venue["id"], "title": "场次",
            "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T11:00:00+00:00", "capacity": 5}, token=op)[1]["data"]
        a = self.c.post("/api/slots",
                        {"session_id": sess["id"], "volunteer_id": minor["id"]},
                        token=op)[1]["data"]
        self.c.post(f"/api/assignments/{a['id']}/confirm", token=op)

        # 王监护能看到小明
        status, payload = self.c.get("/api/assignments", token=gtoken)
        self.assertEqual(status, 200)
        self.assertEqual({x["volunteer_id"] for x in payload["data"]["assignments"]},
                         {minor["id"]})
        # 王监护直接查小红的记录被拒
        status, payload = self.c.get(
            f"/api/assignments?volunteer_id={other_minor['id']}", token=gtoken)
        self.assertEqual(status, 403)
        # 王监护直接访问小红的安排 ID 被拒
        # 先让小红也产生一条安排（运营员建资格）
        self.c.post("/api/training",
                    {"volunteer_id": other_minor["id"], "venue_id": venue["id"],
                     "status": "passed"}, token=op)
        self.c.post("/api/consents",
                    {"volunteer_id": other_minor["id"], "venue_ids": [venue["id"]]},
                    token=other_gtoken)
        other_a = self.c.post("/api/slots",
                              {"session_id": sess["id"],
                               "volunteer_id": other_minor["id"]}, token=op)[1]["data"]
        status, payload = self.c.get(f"/api/assignments/{other_a['id']}", token=gtoken)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "NOT_GUARDIAN")
        # 王监护的授权列表里只有小明
        status, payload = self.c.get("/api/guardian/consents", token=gtoken)
        self.assertEqual(set(payload["data"]["consents"].keys()), {minor["id"]})
        # 王监护不能替小红撤回
        status, payload = self.c.post("/api/consents/revoke",
                                      {"volunteer_id": other_minor["id"]}, token=gtoken)
        self.assertEqual(status, 403)

    def test_cross_venue_transfer_requires_scope_and_is_atomic(self) -> None:
        op, gtoken, guardian_acct, minor, venue_a, venue_b = self._seed()
        self.c.post("/api/training",
                    {"volunteer_id": minor["id"], "venue_id": venue_b["id"],
                     "status": "passed"}, token=op)
        self.c.post("/api/consents",
                    {"volunteer_id": minor["id"], "venue_ids": [venue_a["id"]]},
                    token=gtoken)
        sess_a = self.c.post("/api/sessions", {
            "venue_id": venue_a["id"], "title": "A场",
            "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T11:00:00+00:00", "capacity": 1}, token=op)[1]["data"]
        sess_b = self.c.post("/api/sessions", {
            "venue_id": venue_b["id"], "title": "B场",
            "starts_at": "2026-10-06T09:00:00+00:00",
            "ends_at": "2026-10-06T11:00:00+00:00", "capacity": 1}, token=op)[1]["data"]
        a = self.c.post("/api/slots",
                        {"session_id": sess_a["id"], "volunteer_id": minor["id"]},
                        token=op)[1]["data"]
        self.c.post(f"/api/assignments/{a['id']}/confirm", token=op)
        # 监护人不能调班（仅运营员）
        self.assertEqual(
            self.c.post(f"/api/assignments/{a['id']}/transfer",
                        {"target_session_id": sess_b["id"]}, token=gtoken)[0],
            403)
        # 授权不覆盖 B 馆
        status, payload = self.c.post(f"/api/assignments/{a['id']}/transfer",
                                      {"target_session_id": sess_b["id"]}, token=op)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "CONSENT_NOT_COVERING")
        # 扩展授权后调班成功，新安排快照 seq=2
        self.c.post("/api/consents",
                    {"volunteer_id": minor["id"],
                     "venue_ids": [venue_a["id"], venue_b["id"]]}, token=gtoken)
        status, payload = self.c.post(f"/api/assignments/{a['id']}/transfer",
                                      {"target_session_id": sess_b["id"]}, token=op)
        self.assertEqual(status, 200, payload)
        moved = payload["data"]
        self.assertEqual(moved["venue_id"], venue_b["id"])
        self.assertEqual(moved["consent_seq"], 2)
        self.assertEqual(moved["source"], "transfer")
        # 原安排已取消
        self.assertEqual(self.c.get(f"/api/assignments/{a['id']}", token=op)
                         [1]["data"]["state"], "cancelled")
        # 再调一次原安排：确定失败
        status, payload = self.c.post(f"/api/assignments/{a['id']}/transfer",
                                      {"target_session_id": sess_b["id"]}, token=op)
        self.assertEqual(status, 409)

    def test_concurrent_confirm_never_double_books(self) -> None:
        op, gtoken, guardian_acct, minor, venue, _ = self._seed()
        # 两个有资格的未成年人
        v2_acct_guardian = guardian_acct  # 同一监护人名下两个孩子
        minor2 = self.c.post("/api/volunteers",
                             {"name": "小红", "birth_date": "2013-03-03"},
                             token=op)[1]["data"]
        self.c.post("/api/guardianships",
                    {"volunteer_id": minor2["id"],
                     "guardian_account_id": guardian_acct["id"], "relation": "母亲"},
                    token=op)
        self.c.post("/api/training",
                    {"volunteer_id": minor2["id"], "venue_id": venue["id"],
                     "status": "passed"}, token=op)
        self.c.post("/api/consents",
                    {"volunteer_id": minor["id"], "venue_ids": [venue["id"]]},
                    token=gtoken)
        self.c.post("/api/consents",
                    {"volunteer_id": minor2["id"], "venue_ids": [venue["id"]]},
                    token=gtoken)
        sess = self.c.post("/api/sessions", {
            "venue_id": venue["id"], "title": "争抢场",
            "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T11:00:00+00:00", "capacity": 1}, token=op)[1]["data"]
        results: list[tuple] = []
        barrier = threading.Barrier(2)

        def request(volunteer_id: str) -> None:
            barrier.wait()
            results.append(self.c.post("/api/slots",
                                       {"session_id": sess["id"],
                                        "volunteer_id": volunteer_id}, token=op))

        t1 = threading.Thread(target=request, args=(minor["id"],))
        t2 = threading.Thread(target=request, args=(minor2["id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        outcomes = sorted(
            (r[1]["data"].get("outcome") if r[0] == 200 else f"err:{r[1]['error']['code']}")
            for r in results)
        self.assertEqual(outcomes, ["held", "waitlisted"])
        status = self.c.get(f"/api/sessions/{sess['id']}/status", token=op)[1]["data"]
        self.assertEqual(status["occupied"], 1)
        self.assertEqual(len(status["waitlist"]), 1)

        # 持名额者确认后取消，候补被递补为 held
        held_id = next(r[1]["data"]["id"] for r in results if r[1]["data"].get("state") == "held")
        self.assertEqual(self.c.post(f"/api/assignments/{held_id}/confirm",
                                     token=op)[0], 200)
        cancel = self.c.post(f"/api/assignments/{held_id}/cancel",
                             {"reason": "临时有事"}, token=op)[1]["data"]
        self.assertEqual(len(cancel["promotions"]), 1)
        promoted_id = cancel["promotions"][0]["assignment_id"]
        # 并发确认同一个递补安排：恰好一个成功
        confirm_results: list[int] = []
        barrier2 = threading.Barrier(2)

        def confirm() -> None:
            barrier2.wait()
            confirm_results.append(
                self.c.post(f"/api/assignments/{promoted_id}/confirm", token=op)[0])

        t1 = threading.Thread(target=confirm)
        t2 = threading.Thread(target=confirm)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(confirm_results), [200, 409])

    def test_auth_required(self) -> None:
        self.assertEqual(self.c.get("/api/assignments")[0], 401)
        self.assertEqual(self.c.post("/api/venues", {"name": "x"})[0], 401)
        bad = ApiClient(self.h.base)
        self.assertEqual(bad.get("/api/assignments", token="not-a-token")[0], 401)

    def test_bootstrap_allowed_once(self) -> None:
        self.c.post("/api/setup/bootstrap", {"display_name": "首个运营"})
        status, payload = self.c.post("/api/setup/bootstrap", {"display_name": "再试"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "ALREADY_INITIALIZED")


class RestartSweepTest(unittest.TestCase):
    def test_expired_hold_swept_on_restart(self) -> None:
        import tempfile

        tmp = tempfile.mkdtemp()
        path = f"{tmp}/v.db"
        h1 = ServerHarness(path)
        c = ApiClient(h1.base)
        c.post("/api/setup/bootstrap", {"display_name": "运营"})
        op = c.post("/api/auth/token",
                    {"account_id": h1.state.db.query_one(
                        "SELECT id FROM accounts LIMIT 1")["id"]})[1]["data"]["token"]
        venue = c.post("/api/venues", {"name": "馆"}, token=op)[1]["data"]
        guardian_acct = c.post("/api/accounts",
                               {"role": "guardian", "display_name": "G"}, token=op)[1]["data"]
        gtoken = c.post("/api/auth/token",
                        {"account_id": guardian_acct["id"]})[1]["data"]["token"]
        minor = c.post("/api/volunteers",
                       {"name": "小", "birth_date": "2012-06-01"}, token=op)[1]["data"]
        c.post("/api/guardianships",
               {"volunteer_id": minor["id"], "guardian_account_id": guardian_acct["id"],
                "relation": "父"}, token=op)
        c.post("/api/training",
               {"volunteer_id": minor["id"], "venue_id": venue["id"],
                "status": "passed"}, token=op)
        c.post("/api/consents",
               {"volunteer_id": minor["id"], "venue_ids": [venue["id"]]}, token=gtoken)
        sess = c.post("/api/sessions", {
            "venue_id": venue["id"], "title": "场",
            "starts_at": "2026-10-05T09:00:00+00:00",
            "ends_at": "2026-10-05T11:00:00+00:00", "capacity": 1}, token=op)[1]["data"]
        held = c.post("/api/slots",
                      {"session_id": sess["id"], "volunteer_id": minor["id"],
                       "hold_seconds": 60}, token=op)[1]["data"]
        h1.stop()

        # 重启：时钟前进 61 秒；新 AppState 启动清理应当释放暂占
        h2 = ServerHarness(path)
        h2.clock.advance(61)
        h2.state.startup_sweep()
        c2 = ApiClient(h2.base)
        op2 = c2.post("/api/auth/token",
                      {"account_id": h2.state.db.query_one(
                          "SELECT id FROM accounts LIMIT 1")["id"]})[1]["data"]["token"]
        status_data = c2.get(f"/api/sessions/{sess['id']}/status", token=op2)[1]["data"]
        self.assertEqual(status_data["free"], 1)
        self.assertEqual(status_data["held"], 0)
        assignment = c2.get(f"/api/assignments/{held['id']}", token=op2)[1]["data"]
        self.assertEqual(assignment["state"], "cancelled")
        self.assertEqual(assignment["cancel_reason"], "hold_expired")
        h2.stop()


if __name__ == "__main__":
    unittest.main()
