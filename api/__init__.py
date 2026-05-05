"""
api/__init__.py
=======================
共享 dataclass 和响应工具，所有 route 模块共用。
"""

from fastapi.responses import JSONResponse

# dataclass 定义已移到 services/types.py，此处重新导出保持兼容
from services.types import (  # noqa: F401
    AnalysisResolution,
    ForwardDecision,
    NoiseReductionContext,
    PersistedEventContext,
    WebhookRequestContext,
)

# ── 异常类 ──────────────────────────────────────────────────────────────────


class WebhookRequestError(Exception):
    """基类：Webhook 请求解析错误。"""


class InvalidSignatureError(WebhookRequestError):
    """签名校验失败。"""


class InvalidJsonError(WebhookRequestError):
    """JSON 解析失败。"""


# ── 响应工具 ─────────────────────────────────────────────────────────────────


def _ok(data=None, http_status: int = 200, **extra) -> JSONResponse:
    """Build success JSON response."""
    payload = {"success": True, **(extra if extra else {"data": data})}
    if data is not None and "data" not in extra:
        payload["data"] = data
    return JSONResponse(content=payload, status_code=http_status)


def _fail(error: str, http_status: int = 400, **extra) -> JSONResponse:
    """Build error JSON response."""
    payload = {"success": False, "error": error, **extra}
    return JSONResponse(content=payload, status_code=http_status)
