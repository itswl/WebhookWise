"""
api/__init__.py
=======================
共享响应工具和异常类，所有 route 模块共用。
"""

from typing import Any

from fastapi.responses import JSONResponse

INTERNAL_ERROR_MESSAGE = "内部服务错误"

# ── 异常类 ──────────────────────────────────────────────────────────────────


class InvalidSignatureError(Exception):
    """签名校验失败。"""


# ── 响应工具 ─────────────────────────────────────────────────────────────────


def ok_response(data: Any = None, http_status: int = 200, **extra: Any) -> JSONResponse:
    """Build success JSON response."""
    payload: dict[str, Any] = {"success": True, **(extra if extra else {"data": data})}
    if data is not None and "data" not in extra:
        payload["data"] = data
    return JSONResponse(content=payload, status_code=http_status)


def fail_response(error: str, http_status: int = 400, **extra: Any) -> JSONResponse:
    """Build error JSON response."""
    payload: dict[str, Any] = {"success": False, "error": error, **extra}
    return JSONResponse(content=payload, status_code=http_status)


def internal_error_response(**extra: Any) -> JSONResponse:
    """Build a sanitized 500 response; route handlers log the original exception."""
    return fail_response(INTERNAL_ERROR_MESSAGE, 500, **extra)
