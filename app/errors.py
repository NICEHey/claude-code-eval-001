"""业务异常。

所有 ValidationError / ConflictError / NotFoundError 都会在 HTTP 层被
捕获并转换为对应的 4xx JSON 响应；其余异常视为内部错误（500）。
"""

from __future__ import annotations


class ServiceError(Exception):
    """业务错误基类。"""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details

    def to_dict(self) -> dict:
        payload = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class ValidationError(ServiceError):
    """输入校验失败（400）。"""

    status_code = 400
    code = "invalid_request"


class ConflictError(ServiceError):
    """资源冲突（409）：站号已存在、批次内容冲突、观测水位冲突等。"""

    status_code = 409
    code = "conflict"


class NotFoundError(ServiceError):
    """资源不存在（404）。"""

    status_code = 404
    code = "not_found"
