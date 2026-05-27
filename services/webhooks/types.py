"""业务流程数据结构 — 供 pipeline 和其他 service 层使用，不依赖 api 层。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal, NotRequired, Required, TypedDict, cast

JsonObject = dict[str, Any]


class AnalysisMetaKey(StrEnum):
    ROUTE_TYPE = "_route_type"
    DEGRADED = "_degraded"
    DEGRADED_REASON = "_degraded_reason"
    CACHE_HIT = "_cache_hit"
    CACHE_HIT_COUNT = "_cache_hit_count"
    PENDING = "_pending"
    EMBEDDING = "_embedding"


class ForwardMetaKey(StrEnum):
    PENDING = "_pending"
    OPENCLAW_RUN_ID = "_openclaw_run_id"
    OPENCLAW_SESSION_KEY = "_openclaw_session_key"
    DEGRADED = "_degraded"
    DEGRADED_REASON = "_degraded_reason"


class OpenClawMetaKey(StrEnum):
    TEXT = "_openclaw_text"
    NEED_SUCCESS_NOTIFY = "_need_success_notify"
    MANUAL_RETRY_STARTED_AT = "_manual_retry_started_at"


class WebhookPayloadMetaKey(StrEnum):
    ADAPTER = "_adapter"


ANALYSIS_ROUTE_TYPE: Final = AnalysisMetaKey.ROUTE_TYPE.value
ANALYSIS_DEGRADED: Final = AnalysisMetaKey.DEGRADED.value
ANALYSIS_DEGRADED_REASON: Final = AnalysisMetaKey.DEGRADED_REASON.value
ANALYSIS_CACHE_HIT: Final = AnalysisMetaKey.CACHE_HIT.value
ANALYSIS_CACHE_HIT_COUNT: Final = AnalysisMetaKey.CACHE_HIT_COUNT.value
ANALYSIS_PENDING: Final = AnalysisMetaKey.PENDING.value
ANALYSIS_EMBEDDING: Final = AnalysisMetaKey.EMBEDDING.value
FORWARD_PENDING: Final = ForwardMetaKey.PENDING.value
OPENCLAW_RUN_ID: Final = ForwardMetaKey.OPENCLAW_RUN_ID.value
OPENCLAW_SESSION_KEY: Final = ForwardMetaKey.OPENCLAW_SESSION_KEY.value
FORWARD_DEGRADED: Final = ForwardMetaKey.DEGRADED.value
FORWARD_DEGRADED_REASON: Final = ForwardMetaKey.DEGRADED_REASON.value
OPENCLAW_TEXT: Final = OpenClawMetaKey.TEXT.value
OPENCLAW_NEED_SUCCESS_NOTIFY: Final = OpenClawMetaKey.NEED_SUCCESS_NOTIFY.value
MANUAL_RETRY_STARTED_AT: Final = OpenClawMetaKey.MANUAL_RETRY_STARTED_AT.value
WEBHOOK_ADAPTER: Final = WebhookPayloadMetaKey.ADAPTER.value

AnalysisRouteType = Literal["ai", "cache", "rule", "redis_reuse", "db_reuse", "rechain"]
ALLOWED_ANALYSIS_ROUTE_TYPES: Final = frozenset({"ai", "cache", "rule", "redis_reuse", "db_reuse", "rechain"})


class AnalysisResult(TypedDict):
    """AI/rule analysis contract shared by cache, noise reduction and persistence."""

    importance: Required[str]
    summary: Required[str]
    source: NotRequired[str]
    event_type: NotRequired[str]
    impact_scope: NotRequired[str | None]
    actions: NotRequired[list[str]]
    risks: NotRequired[list[str]]
    monitoring_suggestions: NotRequired[list[str]]
    noise_reduction: NotRequired[JsonObject]
    root_cause: NotRequired[str]
    impact: NotRequired[str]
    confidence: NotRequired[float]
    _route_type: NotRequired[AnalysisRouteType]
    _degraded: NotRequired[bool]
    _degraded_reason: NotRequired[str]
    _cache_hit: NotRequired[bool]
    _cache_hit_count: NotRequired[int]
    _pending: NotRequired[bool]
    _embedding: NotRequired[list[float]]


class ForwardResult(TypedDict):
    """Result shape returned by forwarding integrations."""

    status: Required[str]
    reason: NotRequired[str]
    message: NotRequired[str]
    status_code: NotRequired[int]
    outbox_id: NotRequired[int]
    outbox_ids: NotRequired[list[int]]
    _pending: NotRequired[bool]
    _openclaw_run_id: NotRequired[str]
    _openclaw_session_key: NotRequired[str]
    _degraded: NotRequired[bool]
    _degraded_reason: NotRequired[str]


class WebhookData(TypedDict, total=False):
    """Normalized webhook payload that moves through service-layer workflows.

    Raw webhook bodies stay intentionally loose at the ingress edge. Once a
    payload enters adapters, pipeline, analysis or forwarding, the common fields
    and project-owned metadata keys are declared here instead of being an
    unbounded ``dict[str, Any]`` contract.
    """

    source: str
    headers: JsonObject
    parsed_data: JsonObject
    body: JsonObject
    timestamp: str
    Type: str
    RuleName: str
    AlertName: str
    MetricName: str
    Namespace: str
    Level: str
    Severity: str
    Resources: list[JsonObject]
    event: str
    event_type: str
    alert_id: str
    alert_name: str
    service: str
    summary: str
    msg_type: str
    card: JsonObject
    labels: JsonObject
    annotations: JsonObject
    alerts: list[JsonObject]
    id: int
    action: str
    status: str
    analysis_result: JsonObject
    duration_seconds: float
    created_at: Any
    webhook_event_id: int
    openclaw_session_key: str
    openclaw_run_id: str
    engine: str
    poll_attempts: int
    next_poll_at: Any
    last_polled_at: Any
    _adapter: str
    _alert_identity: dict[str, str]
    _need_success_notify: bool
    _embedding: list[float]


def is_analysis_degraded(result: Mapping[str, Any] | None) -> bool:
    return bool(result and result.get(ANALYSIS_DEGRADED))


def analysis_degraded_reason(result: Mapping[str, Any] | None) -> str:
    return str((result or {}).get(ANALYSIS_DEGRADED_REASON, ""))


def analysis_route(result: Mapping[str, Any] | None, default: AnalysisRouteType = "ai") -> str:
    return str((result or {}).get(ANALYSIS_ROUTE_TYPE, default))


def set_analysis_route(result: AnalysisResult, route: str) -> AnalysisResult:
    if route not in ALLOWED_ANALYSIS_ROUTE_TYPES:
        raise ValueError(f"unsupported analysis route: {route}")
    result[ANALYSIS_ROUTE_TYPE] = cast(AnalysisRouteType, route)
    return result


def mark_analysis_degraded(result: AnalysisResult, reason: str, *, route: AnalysisRouteType | None = None) -> AnalysisResult:
    result[ANALYSIS_DEGRADED] = True
    result[ANALYSIS_DEGRADED_REASON] = reason
    if route is not None:
        result[ANALYSIS_ROUTE_TYPE] = route
    return result


def cache_hit_count(result: Mapping[str, Any] | None, default: int = 1) -> int:
    raw = (result or {}).get(ANALYSIS_CACHE_HIT_COUNT, default)
    return raw if isinstance(raw, int) else default


def mark_cache_hit(result: AnalysisResult, count: int) -> AnalysisResult:
    result[ANALYSIS_CACHE_HIT] = True
    result[ANALYSIS_CACHE_HIT_COUNT] = count
    return result


def is_pending_result(result: Mapping[str, Any] | None) -> bool:
    return bool(result and result.get(FORWARD_PENDING))


def openclaw_run_id(result: Mapping[str, Any] | None) -> str:
    return str((result or {}).get(OPENCLAW_RUN_ID, ""))


def openclaw_session_key(result: Mapping[str, Any] | None) -> str:
    return str((result or {}).get(OPENCLAW_SESSION_KEY, ""))


def pending_forward_result(run_id: str, session_key: str) -> ForwardResult:
    return {
        "status": "pending",
        FORWARD_PENDING: True,
        OPENCLAW_RUN_ID: run_id,
        OPENCLAW_SESSION_KEY: session_key,
    }


def degraded_forward_result(reason: str) -> ForwardResult:
    return {"status": "degraded", FORWARD_DEGRADED: True, FORWARD_DEGRADED_REASON: reason}


def pending_dedup_placeholder() -> JsonObject:
    return {ANALYSIS_DEGRADED: True, ANALYSIS_PENDING: True}


def unknown_analysis_result() -> AnalysisResult:
    return {"importance": "unknown", "summary": ""}


class WebhookProcessingStatus(StrEnum):
    RECEIVED = "received"
    ANALYZING = "analyzing"
    RETRY = "retry"
    FAILED = "failed"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class ForwardOutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRYING = "retrying"
    SENT = "sent"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"


class DeepAnalysisStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass(frozen=True)
class WebhookRequestContext:
    client_ip: str
    source: str
    payload: bytes
    parsed_data: WebhookData
    webhook_full_data: WebhookData
    headers: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebhookProcessContext:
    event_id: int | None
    request_id: str | None
    metric_source: str
    req_ctx: WebhookRequestContext
    alert_hash: str
    dedup_key: str


@dataclass(frozen=True)
class NoiseReductionContext:
    relation: str
    root_cause_event_id: int | None
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: list[int]


def webhook_data_from_mapping(data: Mapping[str, Any]) -> WebhookData:
    return cast(WebhookData, dict(data))
