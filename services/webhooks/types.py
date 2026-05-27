"""业务流程数据结构 — 供 pipeline 和其他 service 层使用，不依赖 api 层。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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
    body: Any
    timestamp: str
    client_ip: str | None
    Type: str
    RuleName: str
    AlertName: str
    MetricName: str
    Namespace: str
    Level: str
    Severity: str
    Resources: list[JsonObject]
    event: Any
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
    id: Any
    action: str
    status: str
    raw: Any
    first_trigger_time: str
    trigger_condition: str
    query_result: str
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
    related_alert_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "related_alert_ids", tuple(int(item) for item in self.related_alert_ids))


_STRING_WEBHOOK_FIELDS: Final = frozenset(
    {
        "source",
        "timestamp",
        "Type",
        "RuleName",
        "AlertName",
        "MetricName",
        "Namespace",
        "Level",
        "Severity",
        "event_type",
        "alert_id",
        "alert_name",
        "service",
        "summary",
        "msg_type",
        "action",
        "status",
        "openclaw_session_key",
        "openclaw_run_id",
        "engine",
        "first_trigger_time",
        "trigger_condition",
        "query_result",
        WEBHOOK_ADAPTER,
    }
)
_MAPPING_WEBHOOK_FIELDS: Final = frozenset(
    {"headers", "parsed_data", "card", "labels", "annotations", "analysis_result", "_alert_identity"}
)
_LIST_MAPPING_WEBHOOK_FIELDS: Final = frozenset({"Resources", "alerts"})
_INT_WEBHOOK_FIELDS: Final = frozenset({"webhook_event_id", "poll_attempts"})
_FLOAT_WEBHOOK_FIELDS: Final = frozenset({"duration_seconds"})
_BOOL_WEBHOOK_FIELDS: Final = frozenset({OPENCLAW_NEED_SUCCESS_NOTIFY})
_LIST_FLOAT_WEBHOOK_FIELDS: Final = frozenset({ANALYSIS_EMBEDDING})
_OPTIONAL_STRING_WEBHOOK_FIELDS: Final = frozenset({"client_ip"})
_DATETIME_WEBHOOK_FIELDS: Final = frozenset({"created_at", "next_poll_at", "last_polled_at"})
_JSON_WEBHOOK_FIELDS: Final = frozenset({"body", "event", "id", "raw"})


def _copy_json_compatible(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains non-string key: {key!r}")
            copied[key] = _copy_json_compatible(item, path=f"{path}.{key}")
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_json_compatible(item, path=f"{path}[]") for item in value]
    raise ValueError(f"{path} contains non-JSON value: {type(value).__name__}")


def _copy_mapping_field(value: Any, *, field_name: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise ValueError(f"WebhookData.{field_name} must be an object")
    return cast(JsonObject, _copy_json_compatible(value, path=f"WebhookData.{field_name}"))


def _copy_list_of_mappings(value: Any, *, field_name: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ValueError(f"WebhookData.{field_name} must be a list")
    copied: list[JsonObject] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"WebhookData.{field_name}[{index}] must be an object")
        copied.append(cast(JsonObject, _copy_json_compatible(item, path=f"WebhookData.{field_name}[{index}]")))
    return copied


def webhook_data_from_mapping(data: Mapping[str, Any], *, strict: bool = True) -> WebhookData:
    """Validate and copy data into the declared WebhookData boundary.

    Adapter ingress can opt into ``strict=False`` when preserving source-native
    fields for downstream analysis. All other call sites reject undeclared keys
    by default so internal contracts fail fast instead of drifting silently.
    """

    if not isinstance(data, Mapping):
        raise TypeError("WebhookData input must be a mapping")

    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"WebhookData contains non-string key: {key!r}")
        if key in _STRING_WEBHOOK_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"WebhookData.{key} must be a string")
            normalized[key] = value
        elif key in _OPTIONAL_STRING_WEBHOOK_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"WebhookData.{key} must be a string or null")
            normalized[key] = value
        elif key in _MAPPING_WEBHOOK_FIELDS:
            normalized[key] = _copy_mapping_field(value, field_name=key)
        elif key in _LIST_MAPPING_WEBHOOK_FIELDS:
            normalized[key] = _copy_list_of_mappings(value, field_name=key)
        elif key in _INT_WEBHOOK_FIELDS:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"WebhookData.{key} must be an integer")
            normalized[key] = value
        elif key in _FLOAT_WEBHOOK_FIELDS:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"WebhookData.{key} must be a number")
            normalized[key] = float(value)
        elif key in _BOOL_WEBHOOK_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"WebhookData.{key} must be a boolean")
            normalized[key] = value
        elif key in _LIST_FLOAT_WEBHOOK_FIELDS:
            if not isinstance(value, list) or any(not isinstance(item, (int, float)) for item in value):
                raise ValueError(f"WebhookData.{key} must be a numeric list")
            normalized[key] = [float(item) for item in value]
        elif key in _DATETIME_WEBHOOK_FIELDS:
            normalized[key] = value if isinstance(value, (datetime, date)) else _copy_json_compatible(
                value, path=f"WebhookData.{key}"
            )
        elif key in _JSON_WEBHOOK_FIELDS:
            normalized[key] = _copy_json_compatible(value, path=f"WebhookData.{key}")
        else:
            if strict:
                raise ValueError(f"WebhookData.{key} is not declared")
            normalized[key] = _copy_json_compatible(value, path=f"WebhookData.{key}")
    return cast(WebhookData, normalized)
