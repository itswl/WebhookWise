"""业务流程数据结构 — 供 pipeline 和其他 service 层使用，不依赖 api 层。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AnalysisResolution:
    analysis_result: dict
    reanalyzed: bool
    is_duplicate: bool
    original_event: object | None  # WebhookEvent
    beyond_window: bool
    is_reused: bool = False  # True 表示从 Redis 缓存复用其他 Worker 的分析结果


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
    save_result: object  # SaveWebhookResult
    noise_context: NoiseReductionContext
