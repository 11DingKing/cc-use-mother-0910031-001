"""排班领域服务的确定性行为测试。"""
from __future__ import annotations

import threading
import unittest

from helpers import MANAGER, OP, ServiceTestCase

from volunteer_scheduling.errors import DomainError
from volunteer_scheduling.service import Actor


class ConfirmTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.g1 = self.svc.create_guardian(OP, "监护人一", guardian_id="g1")["id"]
        self.hall_a = self.svc.create_venue(OP, "甲展厅", venue_id="hall-a")["id"]
        self.hall_b = self.svc.create_venue(OP, "乙展厅", venue_id="hall-b")["id"]

    def volunteer_with_consent(self, name: str, scope: list[str],
                               birth: str = "2012-03-04") -> str:
        vid = self.svc.create_volunteer(OP, name, birth, self.g1)["id"]
        self.svc.publish_consent(OP, vid, scope)
        return vid

    def test_confirm_pins_consent_version_at_confirm_time(self) -> None:
        vid = self.volunteer_with_consent("小明", ["hall-a"])
        sid = self.session(self.hall_a)
        first = self.confirm(sid, vid, "k-1")["assignment"]
        v2 = self.svc.publish_consent(OP, vid, ["hall-a", "hall-b"])
        self.assertEqual(first["consent_version_id"], self.svc.list_consents(OP, vid)["consents"][0]["id"])
        self.assertNotEqual(first["consent_version_id"], v2["id"])
        explain = self.svc.explain_assignment(OP, first["id"])
        self.assertTrue(explain["valid"])
        self.assertIn("v1", explain["reasons"][0])

    def test_confirm_requires_consent_scope_for_venue(self) -> None:
        vid = self.volunteer_with_consent("小明", ["hall-a"])
        sid = self.session(self.hall_b)
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, vid, "k-1")
        self.assertEqual(ctx.exception.code, "consent_scope")

    def test_confirm_requires_active_consent(self) -> None:
        vid = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        sid = self.session(self.hall_a)
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, vid, "k-1")
        self.assertEqual(ctx.exception.code, "consent_missing")

    def test_confirm_requires_qualification(self) -> None:
        vid = self.volunteer_with_consent("小明", ["hall-a"])
        sid = self.session(self.hall_a, quals=["急救"])
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, vid, "k-1")
        self.assertEqual(ctx.exception.code, "qualification_missing")
        self.svc.add_training(OP, vid, "急救")
        ok = self.confirm(sid, vid, "k-1")
        self.assertEqual(ok["assignment"]["status"], "confirmed")

    def test_confirm_rejects_expired_qualification(self) -> None:
        vid = self.volunteer_with_consent("小明", ["hall-a"])
        self.svc.add_training(OP, vid, "急救", granted_at="2026-09-01T00:00:00+00:00",
                              expires_at="2026-10-01T00:00:00+00:00")
        sid = self.session(self.hall_a, quals=["急救"])
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, vid, "k-1")
        self.assertEqual(ctx.exception.code, "qualification_missing")

    def test_confirm_rejects_non_minor_at_session_start(self) -> None:
        # 报名时 17 岁，场次开始当天满 18 岁。
        vid = self.svc.create_volunteer(OP, "小明", "2008-10-05", self.g1)["id"]
        self.svc.publish_consent(OP, vid, ["hall-a"])
        sid = self.session(self.hall_a)
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, vid, "k-1")
        self.assertEqual(ctx.exception.code, "not_minor")

    def test_registration_rejects_adult(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.svc.create_volunteer(OP, "成年人", "2000-01-01", self.g1)
        self.assertEqual(ctx.exception.code, "not_minor")

    def test_duplicate_confirm_with_same_key_is_idempotent(self) -> None:
        vid = self.volunteer_with_consent("小明", ["hall-a"])
        sid = self.session(self.hall_a)
        first = self.confirm(sid, vid, "k-1")
        replay = self.confirm(sid, vid, "k-1")
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["assignment"]["id"], replay["assignment"]["id"])
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["usage"]["confirmed"], 1)

    def test_same_key_different_payload_conflicts(self) -> None:
        v1 = self.volunteer_with_consent("小明", ["hall-a"])
        v2 = self.volunteer_with_consent("小红", ["hall-a"])
        sid = self.session(self.hall_a, capacity=2)
        self.confirm(sid, v1, "k-1")
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, v2, "k-1")
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    def test_double_confirm_same_volunteer_rejected(self) -> None:
        vid = self.volunteer_with_consent("小明", ["hall-a"])
        sid = self.session(self.hall_a)
        self.confirm(sid, vid, "k-1")
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, vid, "k-2")
        self.assertEqual(ctx.exception.code, "already_confirmed")

    def test_session_full_rejects(self) -> None:
        v1 = self.volunteer_with_consent("小明", ["hall-a"])
        v2 = self.volunteer_with_consent("小红", ["hall-a"])
        sid = self.session(self.hall_a, capacity=1)
        self.confirm(sid, v1, "k-1")
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, v2, "k-2")
        self.assertEqual(ctx.exception.code, "session_full")

    def test_concurrent_confirm_only_one_wins_last_slot(self) -> None:
        volunteers = [self.volunteer_with_consent(f"志愿者{i}", ["hall-a"]) for i in range(6)]
        sid = self.session(self.hall_a, capacity=1)
        barrier = threading.Barrier(len(volunteers))
        results: list[tuple[str, str]] = []

        def work(vid: str, key: str) -> None:
            barrier.wait()
            try:
                self.confirm(sid, vid, key)
                results.append(("ok", vid))
            except DomainError as exc:
                results.append((exc.code, vid))

        threads = [threading.Thread(target=work, args=(vid, f"k-{i}"))
                   for i, vid in enumerate(volunteers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(sorted(r[0] for r in results if r[0] != "ok"),
                         ["session_full"] * (len(volunteers) - 1))
        self.assertEqual(self.svc.session_view(OP, sid)["usage"]["confirmed"], 1)


class HoldTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.g1 = self.svc.create_guardian(OP, "监护人一", guardian_id="g1")["id"]
        self.hall_a = self.svc.create_venue(OP, "甲展厅", venue_id="hall-a")["id"]

    def volunteer_with_consent(self, name: str) -> str:
        vid = self.svc.create_volunteer(OP, name, "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, vid, ["hall-a"])
        return vid

    def test_hold_reserves_capacity_and_confirm_consumes(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        v2 = self.volunteer_with_consent("小红")
        sid = self.session(self.hall_a, capacity=1)
        hold = self.svc.create_hold(OP, sid, v1, "h-1")["hold"]
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, v2, "k-2")
        self.assertEqual(ctx.exception.code, "session_full")
        result = self.confirm(sid, v1, "k-1", hold_id=hold["id"])
        self.assertEqual(result["assignment"]["hold_id"], hold["id"])
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["holds"][0]["status"], "consumed")

    def test_expired_hold_frees_capacity_lazily(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        v2 = self.volunteer_with_consent("小红")
        sid = self.session(self.hall_a, capacity=1)
        self.svc.create_hold(OP, sid, v1, "h-1", ttl_seconds=60)
        self.clock.advance(seconds=120)
        # 清理器尚未运行，过期暂占也不应继续占用名额。
        ok = self.confirm(sid, v2, "k-2")
        self.assertEqual(ok["assignment"]["status"], "confirmed")
        reap = self.svc.reap_expired_holds()
        self.assertEqual(len(reap["expired_hold_ids"]), 1)

    def test_confirm_with_expired_hold_rejected(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        sid = self.session(self.hall_a, capacity=1)
        hold = self.svc.create_hold(OP, sid, v1, "h-1", ttl_seconds=60)["hold"]
        self.clock.advance(seconds=120)
        with self.assertRaises(DomainError) as ctx:
            self.confirm(sid, v1, "k-1", hold_id=hold["id"])
        self.assertEqual(ctx.exception.code, "hold_invalid")

    def test_hold_idempotency_replay(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        sid = self.session(self.hall_a, capacity=1)
        first = self.svc.create_hold(OP, sid, v1, "h-1")
        replay = self.svc.create_hold(OP, sid, v1, "h-1")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["hold"]["id"], replay["hold"]["id"])

    def test_reaper_promotes_waitlist_after_hold_expires(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        v2 = self.volunteer_with_consent("小红")
        sid = self.session(self.hall_a, capacity=1)
        self.svc.create_hold(OP, sid, v1, "h-1", ttl_seconds=60)
        self.svc.join_waitlist(OP, sid, v2)
        self.clock.advance(seconds=120)
        reap = self.svc.reap_expired_holds()
        self.assertEqual(len(reap["expired_hold_ids"]), 1)
        self.assertEqual(len(reap["promotions"]), 1)
        self.assertEqual(reap["promotions"][0]["volunteer_id"], v2)
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["usage"]["confirmed"], 1)
        self.assertEqual(view["roster"][0]["volunteer_id"], v2)

    def test_restart_continues_reaping_expired_holds(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        v2 = self.volunteer_with_consent("小红")
        sid = self.session(self.hall_a, capacity=1)
        self.svc.create_hold(OP, sid, v1, "h-1", ttl_seconds=60)
        self.svc.join_waitlist(OP, sid, v2)
        self.clock.advance(seconds=120)
        # 模拟进程重启：同一数据库文件重新构建服务，构造时自动 recover。
        self.reopen()
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["holds"][0]["status"], "expired")
        self.assertEqual(view["usage"]["confirmed"], 1)
        self.assertEqual(view["roster"][0]["volunteer_id"], v2)


class WithdrawTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.g1 = self.svc.create_guardian(OP, "监护人一", guardian_id="g1")["id"]
        self.g2 = self.svc.create_guardian(OP, "监护人二", guardian_id="g2")["id"]
        self.hall_a = self.svc.create_venue(OP, "甲展厅", venue_id="hall-a")["id"]

    def test_withdraw_cancels_pinned_assignments_and_promotes(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        v2 = self.svc.create_volunteer(OP, "小红", "2011-06-07", self.g2)["id"]
        c1 = self.svc.publish_consent(OP, v1, ["hall-a"])
        self.svc.publish_consent(OP, v2, ["hall-a"])
        sid = self.session(self.hall_a, capacity=1)
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        self.svc.join_waitlist(OP, sid, v2)

        result = self.svc.withdraw_consent(OP, c1["id"])
        self.assertEqual(result["cancelled_assignment_ids"], [a1["id"]])
        self.assertEqual(len(result["promotions"]), 1)
        self.assertEqual(result["promotions"][0]["volunteer_id"], v2)

        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["usage"]["confirmed"], 1)
        self.assertEqual(view["roster"][0]["volunteer_id"], v2)
        explain = self.svc.explain_assignment(OP, a1["id"])
        self.assertFalse(explain["valid"])
        self.assertTrue(any("撤回" in issue for issue in explain["current_issues"]))

    def test_withdraw_is_idempotent(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        c1 = self.svc.publish_consent(OP, v1, ["hall-a"])
        sid = self.session(self.hall_a, capacity=1)
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        first = self.svc.withdraw_consent(OP, c1["id"])
        replay = self.svc.withdraw_consent(OP, c1["id"])
        self.assertFalse(first["already_withdrawn"])
        self.assertTrue(replay["already_withdrawn"])
        self.assertEqual(replay["cancelled_assignment_ids"], [a1["id"]])
        self.assertEqual(replay["promotions"], [])
        # 状态未发生二次变化
        self.assertEqual(self.svc.session_view(OP, sid)["usage"]["confirmed"], 0)

    def test_supersede_keeps_existing_assignments(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        c1 = self.svc.publish_consent(OP, v1, ["hall-a"])
        sid = self.session(self.hall_a, capacity=1)
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        self.svc.publish_consent(OP, v1, ["hall-a"])
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["usage"]["confirmed"], 1)
        explain = self.svc.explain_assignment(OP, a1["id"])
        self.assertTrue(explain["valid"])
        self.assertEqual(self.svc.list_consents(OP, v1)["consents"][0]["status"], "superseded")

    def test_guardian_can_withdraw_only_own_consent(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        c1 = self.svc.publish_consent(OP, v1, ["hall-a"])
        stranger = Actor("guardian", "g2")
        with self.assertRaises(DomainError) as ctx:
            self.svc.withdraw_consent(stranger, c1["id"])
        self.assertEqual(ctx.exception.code, "forbidden")
        owner = Actor("guardian", "g1")
        result = self.svc.withdraw_consent(owner, c1["id"])
        self.assertEqual(result["consent"]["status"], "withdrawn")


class WaitlistTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.g1 = self.svc.create_guardian(OP, "监护人一", guardian_id="g1")["id"]
        self.hall_a = self.svc.create_venue(OP, "甲展厅", venue_id="hall-a")["id"]

    def volunteer_with_consent(self, name: str) -> str:
        vid = self.svc.create_volunteer(OP, name, "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, vid, ["hall-a"])
        return vid

    def test_waitlist_order_is_sequential(self) -> None:
        sid = self.session(self.hall_a, capacity=1)
        v1 = self.volunteer_with_consent("小明")
        self.confirm(sid, v1, "k-1")
        seqs = []
        for name in ("小红", "小刚", "小丽"):
            vid = self.volunteer_with_consent(name)
            entry = self.svc.join_waitlist(OP, sid, vid)["entry"]
            seqs.append(entry["seq"])
        self.assertEqual(seqs, [1, 2, 3])

    def test_join_waitlist_twice_returns_existing(self) -> None:
        sid = self.session(self.hall_a, capacity=1)
        v1 = self.volunteer_with_consent("小明")
        first = self.svc.join_waitlist(OP, sid, v1)
        again = self.svc.join_waitlist(OP, sid, v1)
        self.assertFalse(first["already_waiting"])
        self.assertTrue(again["already_waiting"])
        self.assertEqual(first["entry"]["id"], again["entry"]["id"])

    def test_promotion_skips_ineligible_with_reason(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        sid = self.session(self.hall_a, capacity=1)
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        # 小红没有任何授权，小刚授权齐全。
        v2 = self.svc.create_volunteer(OP, "小红", "2012-03-04", self.g1)["id"]
        v3 = self.volunteer_with_consent("小刚")
        self.svc.join_waitlist(OP, sid, v2)
        self.svc.join_waitlist(OP, sid, v3)
        result = self.svc.cancel_assignment(OP, a1["id"])
        self.assertEqual(len(result["promotions"]), 1)
        self.assertEqual(result["promotions"][0]["volunteer_id"], v3)
        view = self.svc.session_view(OP, sid)
        entries = {e["volunteer_id"]: e for e in view["waitlist"]}
        self.assertEqual(entries[v2]["status"], "invalid")
        self.assertEqual(entries[v2]["note"], "consent_missing")
        self.assertEqual(entries[v3]["status"], "promoted")

    def test_promotion_uses_current_consent_version(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        v2 = self.volunteer_with_consent("小红")
        sid = self.session(self.hall_a, capacity=1)
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        self.svc.join_waitlist(OP, sid, v2)
        c2 = self.svc.publish_consent(OP, v2, ["hall-a"])  # 递补前发布了 v2
        self.svc.cancel_assignment(OP, a1["id"])
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["roster"][0]["consent_version_no"], c2["version_no"])

    def test_concurrent_cancels_promote_in_order_exactly_once(self) -> None:
        v1 = self.volunteer_with_consent("小明")
        v2 = self.volunteer_with_consent("小红")
        waiting = [self.volunteer_with_consent(n) for n in ("小刚", "小丽", "小华")]
        sid = self.session(self.hall_a, capacity=2)
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        a2 = self.confirm(sid, v2, "k-2")["assignment"]
        for vid in waiting:
            self.svc.join_waitlist(OP, sid, vid)
        barrier = threading.Barrier(2)
        outcomes: list[dict] = []
        errors: list[DomainError] = []

        def work(aid: str) -> None:
            barrier.wait()
            try:
                outcomes.append(self.svc.cancel_assignment(OP, aid))
            except DomainError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(a["id"],)) for a in (a1, a2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        promoted = sorted(p["volunteer_id"] for o in outcomes for p in o["promotions"])
        self.assertEqual(promoted, sorted(waiting[:2]))
        view = self.svc.session_view(OP, sid)
        self.assertEqual(view["usage"]["confirmed"], 2)
        remaining = [e for e in view["waitlist"] if e["status"] == "waiting"]
        self.assertEqual([e["volunteer_id"] for e in remaining], [waiting[2]])


class TransferTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.g1 = self.svc.create_guardian(OP, "监护人一", guardian_id="g1")["id"]
        self.hall_a = self.svc.create_venue(OP, "甲展厅", venue_id="hall-a")["id"]
        self.hall_b = self.svc.create_venue(OP, "乙展厅", venue_id="hall-b")["id"]

    def test_cross_venue_transfer_success(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        consent = self.svc.publish_consent(OP, v1, ["hall-a", "hall-b"])
        v2 = self.svc.create_volunteer(OP, "小红", "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, v2, ["hall-a"])
        s1 = self.session(self.hall_a, capacity=1)
        s2 = self.session(self.hall_b, capacity=1)
        a1 = self.confirm(s1, v1, "k-1")["assignment"]
        self.svc.join_waitlist(OP, s1, v2)

        result = self.svc.transfer(OP, a1["id"], s2, "t-1")
        new = result["assignment"]
        self.assertEqual(new["session_id"], s2)
        self.assertEqual(new["consent_version_id"], consent["id"])
        self.assertEqual(new["transferred_from"], a1["id"])
        self.assertEqual(result["previous_assignment_id"], a1["id"])
        # 原场次空出的名额立即递补给小红
        self.assertEqual(len(result["promotions"]), 1)
        self.assertEqual(result["promotions"][0]["volunteer_id"], v2)
        old_view = self.svc.session_view(OP, s1)
        self.assertEqual(old_view["roster"][0]["volunteer_id"], v2)
        explain = self.svc.explain_assignment(OP, new["id"])
        self.assertTrue(any("跨馆调班" in r for r in explain["reasons"]))

    def test_transfer_requires_scope_on_target_venue(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, v1, ["hall-a"])  # 只授权甲展厅
        s1 = self.session(self.hall_a, capacity=1)
        s2 = self.session(self.hall_b, capacity=1)
        a1 = self.confirm(s1, v1, "k-1")["assignment"]
        with self.assertRaises(DomainError) as ctx:
            self.svc.transfer(OP, a1["id"], s2, "t-1")
        self.assertEqual(ctx.exception.code, "consent_scope")
        # 原排班不受影响
        self.assertEqual(self.svc.session_view(OP, s1)["usage"]["confirmed"], 1)

    def test_transfer_full_target_keeps_original(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        v2 = self.svc.create_volunteer(OP, "小红", "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, v1, ["hall-a", "hall-b"])
        self.svc.publish_consent(OP, v2, ["hall-b"])
        s1 = self.session(self.hall_a, capacity=1)
        s2 = self.session(self.hall_b, capacity=1)
        a1 = self.confirm(s1, v1, "k-1")["assignment"]
        self.confirm(s2, v2, "k-2")
        with self.assertRaises(DomainError) as ctx:
            self.svc.transfer(OP, a1["id"], s2, "t-1")
        self.assertEqual(ctx.exception.code, "session_full")
        self.assertEqual(self.svc.session_view(OP, s1)["usage"]["confirmed"], 1)

    def test_transfer_idempotent_replay(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, v1, ["hall-a", "hall-b"])
        s1 = self.session(self.hall_a, capacity=1)
        s2 = self.session(self.hall_b, capacity=1)
        a1 = self.confirm(s1, v1, "k-1")["assignment"]
        first = self.svc.transfer(OP, a1["id"], s2, "t-1")
        replay = self.svc.transfer(OP, a1["id"], s2, "t-1")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["assignment"]["id"], replay["assignment"]["id"])
        self.assertEqual(self.svc.session_view(OP, s2)["usage"]["confirmed"], 1)


class ViewTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.g1 = self.svc.create_guardian(OP, "监护人一", guardian_id="g1")["id"]
        self.g2 = self.svc.create_guardian(OP, "监护人二", guardian_id="g2")["id"]
        self.hall_a = self.svc.create_venue(OP, "甲展厅", venue_id="hall-a")["id"]

    def test_guardian_view_scoped_to_own_records(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        v2 = self.svc.create_volunteer(OP, "小红", "2012-03-04", self.g2)["id"]
        self.svc.publish_consent(OP, v1, ["hall-a"])
        self.svc.publish_consent(OP, v2, ["hall-a"])
        sid = self.session(self.hall_a, capacity=2)
        self.confirm(sid, v1, "k-1")
        self.confirm(sid, v2, "k-2")

        own = self.svc.guardian_view(Actor("guardian", "g1"), "g1")
        self.assertEqual([v["id"] for v in own["volunteers"]], [v1])
        self.assertEqual(len(own["volunteers"][0]["assignments"]), 1)
        with self.assertRaises(DomainError) as ctx:
            self.svc.guardian_view(Actor("guardian", "g1"), "g2")
        self.assertEqual(ctx.exception.code, "forbidden")
        # 运营员可以查看任一监护人档案
        both = self.svc.guardian_view(OP, "g2")
        self.assertEqual([v["id"] for v in both["volunteers"]], [v2])

    def test_explain_assignment_lists_reasons(self) -> None:
        v1 = self.svc.create_volunteer(OP, "小明", "2012-03-04", self.g1)["id"]
        self.svc.publish_consent(OP, v1, ["hall-a"])
        self.svc.add_training(OP, v1, "急救")
        sid = self.session(self.hall_a, capacity=1, quals=["急救"])
        a1 = self.confirm(sid, v1, "k-1")["assignment"]
        explain = self.svc.explain_assignment(OP, a1["id"])
        self.assertTrue(explain["valid"])
        text = "\n".join(explain["reasons"])
        self.assertIn("v1", text)
        self.assertIn("急救", text)
        self.assertIn("名额", text)
        self.assertIn("岁", text)
        self.assertIn("运营员直接确认", text)
        # 场馆负责人（只读）也可以查看解释
        self.assertTrue(self.svc.explain_assignment(MANAGER, a1["id"])["valid"])
        # 无关监护人不可见
        with self.assertRaises(DomainError):
            self.svc.explain_assignment(Actor("guardian", "g2"), a1["id"])


if __name__ == "__main__":
    unittest.main()
