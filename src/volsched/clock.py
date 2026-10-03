"""时钟抽象：生产用 UTC，测试用可拨快的假时钟。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def iso(self) -> str:
        return self.now().isoformat()


class FakeClock:
    """测试时钟：时间只会被显式拨快，便于确定性验证暂占过期。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def iso(self) -> str:
        return self.current.isoformat()

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)
