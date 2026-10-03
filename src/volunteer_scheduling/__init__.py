"""未成年志愿者授权排班服务端。"""
from __future__ import annotations

from .errors import DomainError
from .service import Actor, SchedulingService

__all__ = ["Actor", "DomainError", "SchedulingService"]
__version__ = "0.2.0"
