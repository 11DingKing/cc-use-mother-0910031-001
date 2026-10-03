"""领域错误：稳定错误码贯穿服务层与接口层。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误，携带稳定错误码与建议 HTTP 状态码。

    同一类失败永远返回同一个 ``code``，调用方可以据此做确定性处理。
    """

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
