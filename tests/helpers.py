"""排班服务测试的公共构造。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from volunteer_scheduling.clock import ManualClock  # noqa: E402
from volunteer_scheduling.service import Actor, SchedulingService  # noqa: E402

OP = Actor("operator", "op-1")
MANAGER = Actor("manager", "mgr-1")
START = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)

SESSION_START = "2026-10-05T09:00:00+00:00"
SESSION_END = "2026-10-05T12:00:00+00:00"


class ServiceTestCase(unittest.TestCase):
    """每个用例一个独立数据库文件；时钟固定在 2026-10-01。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.clock = ManualClock(START)
        self.svc = SchedulingService(self.db_path, clock=self.clock, hold_ttl_seconds=60)

    def reopen(self) -> SchedulingService:
        """模拟重启：同一数据库文件重新构建服务（构造时自动 recover）。"""
        self.svc = SchedulingService(self.db_path, clock=self.clock, hold_ttl_seconds=60)
        return self.svc

    # ---- 快捷构造 ----
    def guardian(self, gid: str) -> str:
        return self.svc.create_guardian(OP, f"监护人{gid}", guardian_id=gid)["id"]

    def volunteer(self, name: str, guardian_id: str, birth: str = "2012-03-04") -> str:
        return self.svc.create_volunteer(OP, name, birth, guardian_id)["id"]

    def venue(self, vid: str) -> str:
        return self.svc.create_venue(OP, f"展厅{vid}", venue_id=vid)["id"]

    def session(self, venue_id: str, capacity: int = 1, quals: list[str] | None = None,
                starts: str = SESSION_START, ends: str = SESSION_END) -> str:
        return self.svc.create_session(OP, venue_id, starts, ends, capacity, quals or [])["id"]

    def consent(self, volunteer_id: str, scope: list[str], **kwargs) -> str:
        return self.svc.publish_consent(OP, volunteer_id, scope, **kwargs)["id"]

    def confirm(self, session_id: str, volunteer_id: str, key: str, **kwargs) -> dict:
        return self.svc.confirm(OP, session_id, volunteer_id, key, **kwargs)

    def seed_volunteer(self, name: str, guardian_id: str, scope: list[str],
                       birth: str = "2012-03-04") -> tuple[str, str]:
        """建志愿者并发布授权，返回 (volunteer_id, consent_id)。"""
        vid = self.volunteer(name, guardian_id, birth)
        cid = self.consent(vid, scope)
        return vid, cid
