"""
api/__init__.py
=======================
Shared response helpers and exception classes used by all route modules.
"""

from typing import Any

from fastapi.responses import JSONResponse

INTERNAL_ERROR_MESSAGE = "Internal server error"
DELIVERY_ERROR_MESSAGE = "Delivery failed, please retry later or check the server logs"
TARGET_URL_UNAVAILABLE_MESSAGE = "Target URL unavailable"

# ── Response helpers ─────────────────────────────────────────────────────────


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
