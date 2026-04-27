"""
api/__init__.py
=======================
共享 dataclass 和响应工具，所有 route 模块共用。
"""
from dataclasses import dataclass, field

from fastapi.responses import JSONResponse

# ── 异常类 ──────────────────────────────────────────────────────────────────

class WebhookRequestError(Exception):
    """基类：Webhook 请求解析错误。"""


class InvalidSignatureError(WebhookRequestError):
    """签名校验失败。"""


class InvalidJsonError(WebhookRequestError):
    """JSON 解析失败。"""


# ── Dataclass ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AnalysisResolution:
    analysis_result: dict
    reanalyzed: bool
    is_duplicate: bool
    original_event: object | None  # WebhookEvent
    beyond_window: bool


@dataclass(frozen=True)
class WebhookRequestContext:
    client_ip: str
    source: str
    payload: bytes
    parsed_data: dict
    webhook_full_data: dict
    headers: dict = field(default_factory=dict)


@dataclass
class ForwardDecision:
    should_forward: bool
    skip_reason: str | None
    is_periodic_reminder: bool
    matched_rules: list = field(default_factory=list)


@dataclass(frozen=True)
class NoiseReductionContext:
    relation: str
    root_cause_event_id: int | None
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: list[int]


@dataclass(frozen=True)
class PersistedEventContext:
    save_result: object   # SaveWebhookResult
    noise_context: NoiseReductionContext


# ── 响应工具 ─────────────────────────────────────────────────────────────────

def _ok(data=None, http_status: int = 200, **extra) -> JSONResponse:
    """Build success JSON response."""
    payload = {'success': True, **(extra if extra else {'data': data})}
    if data is not None and 'data' not in extra:
        payload['data'] = data
    return JSONResponse(content=payload, status_code=http_status)


def _fail(error: str, http_status: int = 400, **extra) -> JSONResponse:
    """Build error JSON response."""
    payload = {'success': False, 'error': error, **extra}
    return JSONResponse(content=payload, status_code=http_status)
