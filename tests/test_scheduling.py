"""排班领域核心的确定性测试（不经过 HTTP）。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from volsched.clock import FakeClock
from volsched.consents import ConsentService
from volsched.db import Database
from volsched.errors import DomainError
from volsched.identity import IdentityService
from volsched.scheduling import SchedulingService


def env(hold_seconds: int = 60):
    clock = FakeClock()
    db = Database(":memory:")
    identities = IdentityService(db, clock)
    consents = ConsentService(db, identities, clock)
    scheduling = SchedulingService(
        db, identities, consents, clock, default_hold_seconds=hold_seconds)
    return clock, db, identities, consents, scheduling


def make_world():
    clock, db, ids, consents, sched = env()
    va = ids.create_venue("A馆")
    vb = ids.create_venue("B馆")
    guardian = ids.create_account("guardian", "王监护")
    minor = ids.create_volunteer("小明", "2012-05-01")
    ids.add_guardianship(minor["id"], guardian["id"], "父亲")
    adult = ids.create_volunteer("成年张", "2000-01-01")
    ids.record_training(minor["id"], va["id"], "passed")
    ids.record_training(minor["id"], vb["id"], "passed")
    ids.record_training(adult["id"], va["id"], "passed")
    ids.record_training(adult["id"], vb["id"], "passed")
    consents.issue(minor["id"], guardian["id"], [va["id"]])
    return clock, db, ids, consents, sched, va, vb, guardian, minor, adult


def session(ids, venue, cap, *, start="2026-10-05T09:00:00+00:00",
            end="2026-10-05T11:00:00+00:00"):
    return ids.create_session(venue["id"], "假期讲解", start, end, cap)


class ConsentVersioningTest(unittest.TestCase):
    def test_issue_supersedes_and_revoke_is_deterministic(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        v2 = consents.issue(minor["id"], guardian["id"], [va["id"], vb["id"]])
        self.assertEqual(v2["version_seq"], 2)
        versions = consents.list_versions(minor["id"])
        self.assertEqual([v["status"] for v in versions], ["superseded", "active"])
        self.assertEqual(versions[0]["superseded_by_id"], v2["id"])
        revoked = consents.revoke(minor["id"], guardian["id"], reason="家庭安排变化")
        self.assertEqual(revoked["status"], "revoked")
        self.assertIsNotNone(revoked["revoked_at"])
        # 重复撤回：确定的 CONFLICT，不会制造新状态
        with self.assertRaises(DomainError) as ctx:
            consents.revoke(minor["id"], guardian["id"])
        self.assertEqual(ctx.exception.code, "NO_ACTIVE_CONSENT")

    def test_non_guardian_cannot_issue_or_revoke(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        other = ids.create_account("guardian", "李监护")
        with self.assertRaises(DomainError) as ctx:
            consents.issue(minor["id"], other["id"], [va["id"]])
        self.assertEqual(ctx.exception.http_status, 403)
        with self.assertRaises(DomainError) as ctx:
            consents.revoke(minor["id"], other["id"])
        self.assertEqual(ctx.exception.http_status, 403)


class EligibilityTest(unittest.TestCase):
    def test_request_requires_eligibility_but_revoke_during_hold_blocks_confirm(self) -> None:
        # 暂占时预检资格；监护人在暂占窗口内撤回，确认复检必须失败。
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        held = sched.request_slot(sess["id"], minor["id"])
        self.assertEqual(held["state"], "held")
        consents.revoke(minor["id"], guardian["id"])
        with self.assertRaises(DomainError) as ctx:
            sched.confirm_assignment(held["id"])
        self.assertEqual(ctx.exception.code, "CONSENT_NOT_COVERING")
        # 该暂占到期后被清理，名额不会被无效确认占用
        clock.advance(61)
        sweep = sched.sweep_expired_holds()
        self.assertEqual([s["assignment_id"] for s in sweep["swept"]], [held["id"]])

    def test_request_without_consent_is_rejected(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        no_consent = ids.create_volunteer("无授权少年", "2013-09-09")
        ids.add_guardianship(no_consent["id"], guardian["id"], "母亲")
        ids.record_training(no_consent["id"], va["id"], "passed")
        sess = session(ids, va, 2)
        with self.assertRaises(DomainError) as ctx:
            sched.request_slot(sess["id"], no_consent["id"])
        self.assertEqual(ctx.exception.code, "CONSENT_NOT_COVERING")

    def test_consent_scope_must_cover_actual_venue(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess_b = session(ids, vb, 1)
        # 授权只覆盖 A 馆：请求 B 馆即被拒
        with self.assertRaises(DomainError) as ctx:
            sched.request_slot(sess_b["id"], minor["id"])
        self.assertEqual(ctx.exception.code, "CONSENT_NOT_COVERING")
        # 补齐覆盖 B 馆的新版本后再报名确认
        consents.issue(minor["id"], guardian["id"], [va["id"], vb["id"]])
        confirmed = sched.confirm_assignment(sched.request_slot(sess_b["id"], minor["id"])["id"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["consent_seq"], 2)
        self.assertEqual(set(confirmed["consent_scope"]["venues"]), {va["id"], vb["id"]})

    def test_training_expired_during_hold_blocks_confirmation(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        # 培训资格 30 秒后到期：t=0 成功暂占，确认复检时已过期
        ids.record_training(minor["id"], va["id"], "passed",
                            expires_at="2026-10-01T09:00:30+00:00")
        sess = session(ids, va, 1)
        held = sched.request_slot(sess["id"], minor["id"], hold_seconds=300)
        clock.advance(31)
        with self.assertRaises(DomainError) as ctx:
            sched.confirm_assignment(held["id"])
        self.assertEqual(ctx.exception.code, "TRAINING_EXPIRED")

    def test_adult_needs_no_consent(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        held = sched.request_slot(sess["id"], adult["id"])
        confirmed = sched.confirm_assignment(held["id"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertIsNone(confirmed["consent_version_id"])


class ConfirmationSnapshotTest(unittest.TestCase):
    def test_consent_is_fixed_at_confirmation_and_survives_revoke(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 2)
        held = sched.request_slot(sess["id"], minor["id"])
        confirmed = sched.confirm_assignment(held["id"])
        snap_version = confirmed["consent_version_id"]
        snap_at = confirmed["confirmed_at"]
        # 事后撤回 + 改版：已确认安排的快照不变，解释仍然有效
        consents.revoke(minor["id"], guardian["id"])
        consents.issue(minor["id"], guardian["id"], [vb["id"]])
        again = sched.get_assignment(confirmed["id"])
        self.assertEqual(again["consent_version_id"], snap_version)
        explanation = sched.explain_assignment(confirmed["id"])
        self.assertTrue(explanation["valid"])
        consent_check = next(c for c in explanation["why"] if c["check"] == "guardian_consent")
        self.assertTrue(consent_check["ok"])
        self.assertTrue(consent_check["detail"]["covers_service_venue"])
        # 版本当前状态已变为 revoked，但快照固定在确认时刻
        self.assertEqual(consent_check["detail"]["version_current_status"], "revoked")
        self.assertLessEqual(consent_check["detail"]["issued_at"], snap_at)

    def test_duplicate_confirmation_is_idempotent_conflict(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        held = sched.request_slot(sess["id"], minor["id"])
        first = sched.confirm_assignment(held["id"])
        with self.assertRaises(DomainError) as ctx:
            sched.confirm_assignment(held["id"])
        err = ctx.exception
        self.assertEqual(err.code, "ALREADY_CONFIRMED")
        self.assertEqual(err.details["confirmed_at"], first["confirmed_at"])
        self.assertEqual(err.details["consent_version_id"], first["consent_version_id"])
        # 容量没有被消耗两次
        status = sched.session_status(sess["id"])
        self.assertEqual(status["occupied"], 1)


class CapacityAtomicityTest(unittest.TestCase):
    def test_concurrent_requests_for_last_slot(self) -> None:
        for _ in range(5):
            self._once()

    def _once(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        minor2 = ids.create_volunteer("小红", "2013-03-03")
        ids.add_guardianship(minor2["id"], guardian["id"], "母亲")
        ids.record_training(minor2["id"], va["id"], "passed")
        consents.issue(minor2["id"], guardian["id"], [va["id"]])

        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def request(volunteer_id: str) -> None:
            barrier.wait()
            try:
                result = sched.request_slot(sess["id"], volunteer_id)
                outcomes.append(result["outcome"])
            except DomainError as exc:
                outcomes.append(f"error:{exc.code}")

        t1 = threading.Thread(target=request, args=(minor["id"],))
        t2 = threading.Thread(target=request, args=(minor2["id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes), ["held", "waitlisted"])
        status = sched.session_status(sess["id"])
        self.assertEqual(status["occupied"], 1)
        self.assertEqual(len(status["waitlist"]), 1)

    def test_capacity_never_exceeded_through_allocation(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 3)
        # 3 个占满，2 个候补
        assignments = []
        for i in range(3):
            v = minor if i == 0 else ids.create_volunteer(f"少年{i}", "2012-01-01")
            if i != 0:
                ids.add_guardianship(v["id"], guardian["id"], "亲属")
                ids.record_training(v["id"], va["id"], "passed")
                consents.issue(v["id"], guardian["id"], [va["id"]])
            a = sched.request_slot(sess["id"], v["id"])
            assignments.append(sched.confirm_assignment(a["id"])["id"])
        waiters = []
        for i in range(2):
            v = ids.create_volunteer(f"候补{i}", "2012-06-01")
            ids.add_guardianship(v["id"], guardian["id"], "亲属")
            ids.record_training(v["id"], va["id"], "passed")
            consents.issue(v["id"], guardian["id"], [va["id"]])
            entry = sched.request_slot(sess["id"], v["id"])
            self.assertEqual(entry["outcome"], "waitlisted")
            waiters.append(v["id"])

        # 并发取消 3 个已确认安排：3 次释放，2 个候补严格 FIFO 各拿到一次
        barrier = threading.Barrier(3)

        def cancel(aid: str) -> None:
            barrier.wait()
            sched.cancel_assignment(aid)

        threads = [threading.Thread(target=cancel, args=(a,)) for a in assignments]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        promoted = [dict(r) for r in db.query_all(
            "SELECT * FROM waitlist_entries WHERE session_id = ? AND status = 'promoted'",
            (sess["id"],))]
        self.assertEqual(len(promoted), 2)
        self.assertEqual([p["volunteer_id"] for p in promoted], waiters)
        self.assertEqual(len({p["assignment_id"] for p in promoted}), 2)
        status = sched.session_status(sess["id"])
        self.assertLessEqual(status["occupied"], 3)
        self.assertEqual(status["occupied"], 2)
        self.assertEqual(status["free"], 1)

    def test_waitlist_head_blocks_promotion_of_later_entries(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        first = sched.confirm_assignment(sched.request_slot(sess["id"], minor["id"])["id"])
        # 队头：无培训资格；队尾：资格齐全
        head_v = ids.create_volunteer("无培训少年", "2012-01-01")
        ids.add_guardianship(head_v["id"], guardian["id"], "亲属")
        consents.issue(head_v["id"], guardian["id"], [va["id"]])
        tail_v = ids.create_volunteer("合格少年", "2012-02-01")
        ids.add_guardianship(tail_v["id"], guardian["id"], "亲属")
        ids.record_training(tail_v["id"], va["id"], "passed")
        consents.issue(tail_v["id"], guardian["id"], [va["id"]])
        head_entry = sched.request_slot(sess["id"], head_v["id"])
        tail_entry = sched.request_slot(sess["id"], tail_v["id"])
        result = sched.cancel_assignment(first["id"])
        # 队头不被跳过，队尾也不能越位
        self.assertEqual(result["promotions"], [])
        status = sched.session_status(sess["id"])
        self.assertEqual([w["volunteer_id"] for w in status["waitlist"]],
                         [head_v["id"], tail_v["id"]])
        self.assertEqual(status["free"], 1)
        # 运营员可解释候补为何没有前进
        self.assertEqual(status["head_blocker"]["code"], "TRAINING_MISSING")
        self.assertEqual(status["waitlist"][0]["block_reason"]["code"], "TRAINING_MISSING")
        promoted = sched.promote_now(sess["id"])
        self.assertEqual(promoted["promotions"], [])
        self.assertEqual(promoted["blocked"]["reason_code"], "TRAINING_MISSING")
        # 队头补齐资格后手动触发递补，严格按序
        ids.record_training(head_v["id"], va["id"], "passed")
        promoted = sched.promote_now(sess["id"])
        self.assertEqual(len(promoted["promotions"]), 1)
        self.assertEqual(promoted["promotions"][0]["volunteer_id"], head_v["id"])


class HoldExpiryTest(unittest.TestCase):
    def test_expired_hold_is_swept_and_waitlist_promoted(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        held = sched.request_slot(sess["id"], minor["id"], hold_seconds=60)
        waiter_v = ids.create_volunteer("候补合格", "2012-07-07")
        ids.add_guardianship(waiter_v["id"], guardian["id"], "亲属")
        ids.record_training(waiter_v["id"], va["id"], "passed")
        consents.issue(waiter_v["id"], guardian["id"], [va["id"]])
        entry = sched.request_slot(sess["id"], waiter_v["id"])
        self.assertEqual(entry["outcome"], "waitlisted")
        # 未到期：无动作
        clock.advance(59)
        self.assertEqual(sched.sweep_expired_holds()["swept"], [])
        # 到期：原暂占取消，候补队头得到 held
        clock.advance(2)
        sweep = sched.sweep_expired_holds()
        self.assertEqual([s["assignment_id"] for s in sweep["swept"]], [held["id"]])
        self.assertEqual(len(sweep["promotions"]), 1)
        self.assertEqual(sweep["promotions"][0]["volunteer_id"], waiter_v["id"])
        promoted_a = sched.get_assignment(sweep["promotions"][0]["assignment_id"])
        self.assertEqual(promoted_a["state"], "held")
        self.assertEqual(promoted_a["source"], "waitlist")

    def test_sweep_continues_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "volsched.db")
            clock = FakeClock()
            db = Database(path)
            ids = IdentityService(db, clock)
            consents = ConsentService(db, ids, clock)
            sched = SchedulingService(db, ids, consents, clock, default_hold_seconds=60)
            va = ids.create_venue("A馆")
            guardian = ids.create_account("guardian", "王监护")
            minor = ids.create_volunteer("小明", "2012-01-01")
            ids.add_guardianship(minor["id"], guardian["id"], "父亲")
            ids.record_training(minor["id"], va["id"], "passed")
            consents.issue(minor["id"], guardian["id"], [va["id"]])
            sess = ids.create_session(va["id"], "讲解",
                                      "2026-10-05T09:00:00+00:00",
                                      "2026-10-05T11:00:00+00:00", 1)
            held = sched.request_slot(sess["id"], minor["id"], hold_seconds=60)
            db.close()

            # “重启”：新进程、时钟已走过暂占期限，启动清理立即释放
            clock2 = FakeClock(clock.current)
            clock2.advance(61)
            db2 = Database(path)
            ids2 = IdentityService(db2, clock2)
            consents2 = ConsentService(db2, ids2, clock2)
            sched2 = SchedulingService(db2, ids2, consents2, clock2)
            sweep = sched2.sweep_expired_holds(actor_id="system:startup")
            self.assertEqual([s["assignment_id"] for s in sweep["swept"]], [held["id"]])
            db2.close()


class TransferTest(unittest.TestCase):
    def test_cross_venue_transfer_rechecks_and_resnapshots_consent(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess_a = session(ids, va, 1)
        sess_b = session(ids, vb, 1, start="2026-10-06T09:00:00+00:00",
                         end="2026-10-06T11:00:00+00:00")
        a = sched.confirm_assignment(sched.request_slot(sess_a["id"], minor["id"])["id"])
        # 授权只覆盖 A 馆：跨馆调班被确定拒绝，原安排保持有效
        with self.assertRaises(DomainError) as ctx:
            sched.transfer(a["id"], sess_b["id"])
        self.assertEqual(ctx.exception.code, "CONSENT_NOT_COVERING")
        self.assertEqual(sched.get_assignment(a["id"])["state"], "confirmed")

        # A 馆有候补；调班成功后应把释放名额原子递补给候补
        waiter_v = ids.create_volunteer("候位少年", "2012-08-08")
        ids.add_guardianship(waiter_v["id"], guardian["id"], "亲属")
        ids.record_training(waiter_v["id"], va["id"], "passed")
        consents.issue(waiter_v["id"], guardian["id"], [va["id"]])
        sched.request_slot(sess_a["id"], waiter_v["id"])

        consents.issue(minor["id"], guardian["id"], [va["id"], vb["id"]])
        moved = sched.transfer(a["id"], sess_b["id"])
        self.assertEqual(moved["state"], "confirmed")
        self.assertEqual(moved["venue_id"], vb["id"])
        self.assertEqual(moved["source"], "transfer")
        self.assertEqual(moved["transferred_from_assignment_id"], a["id"])
        self.assertEqual(moved["consent_seq"], 2)
        # 原安排取消且候补已在同一事务内递补
        self.assertEqual(sched.get_assignment(a["id"])["state"], "cancelled")
        status_a = sched.session_status(sess_a["id"])
        self.assertEqual(status_a["occupied"], 1)
        promoted = db.query_one(
            "SELECT * FROM waitlist_entries WHERE volunteer_id = ? AND status = 'promoted'",
            (waiter_v["id"],))
        self.assertIsNotNone(promoted)

    def test_transfer_into_full_session_leaves_original_intact(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess_a = session(ids, va, 1)
        sess_b = session(ids, vb, 1, start="2026-10-06T09:00:00+00:00",
                         end="2026-10-06T11:00:00+00:00")
        consents.issue(minor["id"], guardian["id"], [va["id"], vb["id"]])
        a = sched.confirm_assignment(sched.request_slot(sess_a["id"], minor["id"])["id"])
        # B 馆由成年志愿者占满
        sched.confirm_assignment(sched.request_slot(sess_b["id"], adult["id"])["id"])
        with self.assertRaises(DomainError) as ctx:
            sched.transfer(a["id"], sess_b["id"])
        self.assertEqual(ctx.exception.code, "TARGET_FULL")
        self.assertEqual(sched.get_assignment(a["id"])["state"], "confirmed")

    def test_unconfirmed_assignment_cannot_transfer(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess_a = session(ids, va, 1)
        sess_b = session(ids, vb, 1, start="2026-10-06T09:00:00+00:00",
                         end="2026-10-06T11:00:00+00:00")
        held = sched.request_slot(sess_a["id"], minor["id"])
        with self.assertRaises(DomainError) as ctx:
            sched.transfer(held["id"], sess_b["id"])
        self.assertEqual(ctx.exception.code, "TRANSFER_REQUIRES_CONFIRMED")


class SessionCloseTest(unittest.TestCase):
    def test_close_cancels_holds_and_waitlist_keeps_confirmed(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 2)
        a = sched.confirm_assignment(sched.request_slot(sess["id"], minor["id"])["id"])
        waiter = ids.create_volunteer("候位", "2012-09-09")
        ids.add_guardianship(waiter["id"], guardian["id"], "亲属")
        ids.record_training(waiter["id"], va["id"], "passed")
        consents.issue(waiter["id"], guardian["id"], [va["id"]])
        sched.request_slot(sess["id"], waiter["id"])
        status = sched.close_session(sess["id"])
        self.assertEqual(status["session"]["status"], "closed")
        self.assertEqual(status["confirmed"], 1)
        self.assertEqual(status["held"], 0)
        self.assertEqual(len(status["waitlist"]), 0)
        # 已确认安排保留
        self.assertEqual(sched.get_assignment(a["id"])["state"], "confirmed")
        # 关闭后无法再报名，手动递补也不产生名额
        with self.assertRaises(DomainError) as ctx:
            sched.request_slot(sess["id"], adult["id"])
        self.assertEqual(ctx.exception.code, "SESSION_CLOSED")
        self.assertEqual(sched.promote_now(sess["id"])["promotions"], [])


class ExplanationTest(unittest.TestCase):
    def test_explain_lists_every_validity_basis(self) -> None:
        clock, db, ids, consents, sched, va, vb, guardian, minor, adult = make_world()
        sess = session(ids, va, 1)
        a = sched.confirm_assignment(sched.request_slot(sess["id"], minor["id"])["id"])
        explanation = sched.explain_assignment(a["id"])
        names = {c["check"] for c in explanation["why"]}
        self.assertEqual(names, {"capacity", "training", "guardian_consent"})
        self.assertTrue(all(c["ok"] for c in explanation["why"]))


if __name__ == "__main__":
    unittest.main()
