"""
server/app/envelope.py
统一响应信封（S6）：
  成功 {"ok": true, "data": ..., "data_version": ...}
  失败 {"ok": false, "error": {"code": ..., "message": ...}}

错误码（S4）：
  UNAUTHORIZED(401) / FORBIDDEN(403) / NOT_FOUND(404) /
  VALIDATION(400) / CONFLICT(409) / DEGRADED_WRITE_REJECTED(409) /
  INTERNAL(500)
"""
from __future__ import annotations

from typing import Any

# 错误码 → HTTP 状态
ERR_UNAUTHORIZED = "UNAUTHORIZED"
ERR_FORBIDDEN = "FORBIDDEN"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_VALIDATION = "VALIDATION"
ERR_CONFLICT = "CONFLICT"
ERR_DEGRADED_WRITE = "DEGRADED_WRITE_REJECTED"
ERR_INTERNAL = "INTERNAL"


class APIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def ok(data: Any = None, data_version: int | None = None) -> dict:
    env: dict[str, Any] = {"ok": True, "data": data}
    if data_version is not None:
        env["data_version"] = data_version
    return env


def error_body(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}
