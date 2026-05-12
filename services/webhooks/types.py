"""业务流程数据结构 — 供 pipeline 和其他 service 层使用，不依赖 api 层。"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models import WebhookEvent


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
    EXHAUSTED = "exhausted"


class FailedForwardStatus(StrEnum):
    PENDING = "pending"
    RETRYING = "retrying"
    SUCCESS = "success"
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
    analysis_result: dict[str, Any]
    reanalyzed: bool
    is_duplicate: bool
    original_event: "WebhookEvent | None"
    beyond_window: bool
    is_reused: bool = False  # True 表示从 Redis 缓存复用其他 Worker 的分析结果


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
    event_id: int
    client_ip: str
    metric_source: str
    req_ctx: WebhookRequestContext
    alert_hash: str


@dataclass
class ForwardDecision:
    should_forward: bool
    skip_reason: str | None
    is_periodic_reminder: bool
    matched_rules: list[dict[str, Any]] = field(default_factory=list)


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
    save_result: object  # SaveWebhookResult
    noise_context: NoiseReductionContext


# Unified type aliases — single source of truth
WebhookData = dict[str, Any]
AnalysisResult = dict[str, Any]
ForwardResult = dict[str, Any]
