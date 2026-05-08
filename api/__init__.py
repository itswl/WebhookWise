"""
api/__init__.py
=======================
共享响应工具和异常类，所有 route 模块共用。
"""

from typing import Any

from fastapi.responses import JSONResponse

# ── 异常类 ──────────────────────────────────────────────────────────────────


class InvalidSignatureError(Exception):
    """签名校验失败。"""


# ── 响应工具 ─────────────────────────────────────────────────────────────────


def _ok(data: Any = None, http_status: int = 200, **extra: Any) -> JSONResponse:
    """Build success JSON response."""
    payload: dict[str, Any] = {"success": True, **(extra if extra else {"data": data})}
    if data is not None and "data" not in extra:
        payload["data"] = data
    return JSONResponse(content=payload, status_code=http_status)


def _fail(error: str, http_status: int = 400, **extra: Any) -> JSONResponse:
    """Build error JSON response."""
    payload: dict[str, Any] = {"success": False, "error": error, **extra}
    return JSONResponse(content=payload, status_code=http_status)
