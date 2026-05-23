"""业务流程数据结构 — 供 pipeline 和其他 service 层使用，不依赖 api 层。"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypedDict

if TYPE_CHECKING:
    from models import WebhookEvent


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class NoiseReductionSnapshot(TypedDict):
    relation: str
    root_cause_event_id: int | None
    confidence: float
    suppress_forward: bool
    reason: str
    related_alert_count: int
    related_alert_ids: list[int]


class AnalysisResult(TypedDict, total=False):
    """AI/rule analysis contract shared by cache, noise reduction and persistence."""

    source: str
    event_type: str
    importance: str
    summary: str
    impact_scope: str | None
    impact: str
    actions: list[str]
    risks: list[str]
    monitoring_suggestions: list[str]
    noise_reduction: NoiseReductionSnapshot
    _route_type: Literal["ai", "cache", "rule", "redis_reuse", "db_reuse"]
    _degraded: bool
    _degraded_reason: str
    _cache_hit: bool
    _cache_hit_count: int


class ForwardResult(TypedDict, total=False):
    """Result shape returned by forwarding integrations."""

    status: str
    reason: str
    message: str
    status_code: int
    response: dict[str, Any]
    outbox_id: int
    outbox_ids: list[int]
    _pending: bool
    _openclaw_run_id: Any
    _openclaw_session_key: str
    _degraded: bool
    _degraded_reason: str
    source: str
    event_type: str
    importance: str
    summary: str
    impact_scope: str | None
    actions: list[str]
    risks: list[str]
    monitoring_suggestions: list[str]


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
class AnalysisResolution:
    analysis_result: AnalysisResult
    reanalyzed: bool
    is_duplicate: bool
    original_event: "WebhookEvent | None"
    beyond_window: bool
    is_reused: bool = False  # True 表示从 Redis 缓存复用其他 Worker 的分析结果
    original_event_id: int | None = None


@dataclass(frozen=True)
class WebhookRequestContext:
    client_ip: str
    source: str
    payload: bytes
    parsed_data: dict[str, Any]
    webhook_full_data: dict[str, Any]
    headers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebhookProcessContext:
    event_id: int | None
    request_id: str | None
    client_ip: str
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


# Unified type alias for external webhook payloads. The source data is intentionally
# loose at the ingress boundary; internal analysis and forwarding contracts above
# are typed once the payload has been interpreted.
WebhookData = dict[str, Any]
