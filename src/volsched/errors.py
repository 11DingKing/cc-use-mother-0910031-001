"""领域错误与 HTTP 状态映射。"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有可预期的业务失败都抛此异常，结果确定、错误码稳定。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}

    @classmethod
    def validation(cls, code: str, message: str) -> "DomainError":
        return cls(code, message, 400)

    @classmethod
    def unauthorized(cls, code: str = "UNAUTHENTICATED", message: str = "缺少或无效的访问令牌") -> "DomainError":
        return cls(code, message, 401)

    @classmethod
    def forbidden(cls, code: str = "FORBIDDEN", message: str = "当前角色无权执行该操作") -> "DomainError":
        return cls(code, message, 403)

    @classmethod
    def not_found(cls, code: str, message: str) -> "DomainError":
        return cls(code, message, 404)

    @classmethod
    def conflict(cls, code: str, message: str, details: dict[str, Any] | None = None) -> "DomainError":
        return cls(code, message, 409, details)

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body
