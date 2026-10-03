"""可注入时钟：测试与重启恢复场景下的时间确定性来源。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    """生产时钟，返回 UTC 当前时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用手动时钟，可显式推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now
