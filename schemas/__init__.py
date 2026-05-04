"""
Unified Schemas for WebhookWise.
Consolidated from base, admin, analysis, rules, and webhook schemas.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# ── Base Schemas ─────────────────────────────────────────────────────────────


class APIResponse(BaseModel, Generic[T]):
    """统一 API 响应包装"""

    success: bool
    data: T | None = None
    message: str | None = None
    error: str | None = None


class CursorPaginationInfo(BaseModel):
    next_cursor: int | None = None
    has_more: bool = False
    limit: int | None = None
    page_size: int | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: str


# ── Analysis Schemas ─────────────────────────────────────────────────────────


class Importance(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WebhookAnalysisResult(BaseModel):
    """AI 分析结果模型"""

    source: str = Field(description="来源系统名称")
    event_type: str = Field(description="详细的事件类型/名称")
    importance: Importance = Field(description="事件重要程度", default=Importance.MEDIUM)
    summary: str = Field(description="事件摘要（中文，50字内，包含关键指标或报错信息）")
    impact_scope: str | None = Field(None, description="影响范围评估")
    actions: list[str] = Field(default_factory=list, description="建议的响应操作列表")
    risks: list[str] = Field(default_factory=list, description="潜在的关联风险列表")
    monitoring_suggestions: list[str] = Field(default_factory=list, description="后续的监控优化建议")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class DeepAnalysisRecord(BaseModel):
    """深度分析记录 —— 对应 DeepAnalysis"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    webhook_event_id: int
    engine: str | None = None
    user_question: str | None = None
    analysis_result: dict | str | None = None
    duration_seconds: float | None = None
    created_at: str | None = None
    openclaw_run_id: str | None = None
    openclaw_session_key: str | None = None
    status: str | None = None
    source: str | None = None
    is_duplicate: bool = False
    beyond_window: bool = False


class DeepAnalysisListData(BaseModel):
    """深度分析列表数据（含分页）"""

    total: int | None = None
    total_pages: int | None = None
    page: int | None = None
    per_page: int
    next_cursor: int | None = None
    items: list[DeepAnalysisRecord]


class DeepAnalysisListResponse(BaseModel):
    """深度分析列表响应"""

    success: bool
    data: DeepAnalysisListData


class ReanalysisResponse(BaseModel):
    """重新分析响应"""

    success: bool
    status: str | None = None
    analysis: dict[str, Any] | None = None
    original_importance: str | None = None
    new_importance: str | None = None
    updated_duplicates: int | None = None
    message: str | None = None


# ── Webhook Schemas ──────────────────────────────────────────────────────────

DuplicateType = Literal["new", "within_window", "beyond_window"]


class WebhookEventFull(BaseModel):
    """完整 Webhook 事件"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    client_ip: str | None = None
    timestamp: str | None = None
    raw_payload: str | None = None
    headers: dict | None = None
    parsed_data: dict | None = None
    alert_hash: str | None = None
    ai_analysis: dict | None = None
    importance: str | None = None
    processing_status: str
    forward_status: str | None = None
    is_duplicate: bool
    duplicate_of: int | None = None
    duplicate_count: int
    beyond_window: bool
    beyond_time_window: bool = False
    is_within_window: bool = False
    duplicate_type: DuplicateType = "new"
    created_at: str | None = None
    updated_at: str | None = None
    prev_alert_id: int | None = None
    prev_alert_timestamp: str | None = None


class WebhookEventSummary(BaseModel):
    """Webhook 事件摘要"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    client_ip: str | None = None
    timestamp: str | None = None
    importance: str | None = None
    is_duplicate: bool
    duplicate_of: int | None = None
    duplicate_count: int
    beyond_window: bool
    beyond_time_window: bool = False
    is_within_window: bool = False
    duplicate_type: DuplicateType = "new"
    forward_status: str | None = None
    summary: str | None = None
    alert_info: dict | None = None
    created_at: str | None = None
    prev_alert_id: int | None = None
    prev_alert_timestamp: str | None = None


class WebhookReceiveResponse(BaseModel):
    """Webhook 接收响应"""

    success: bool
    message: str
    event_id: int


class WebhookListResponse(BaseModel):
    """Webhook 列表响应"""

    success: bool
    data: list[WebhookEventSummary]
    status: int = 200
    pagination: CursorPaginationInfo


class HealthData(BaseModel):
    status: str
    database: str


class HealthResponse(APIResponse[HealthData]):
    pass


class WebhookDetailResponse(APIResponse[WebhookEventFull]):
    pass


# ── Forwarding Rule Schemas ──────────────────────────────────────────────────


class ForwardRuleSchema(BaseModel):
    """转发规则"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool
    priority: int
    match_importance: str | None = None
    match_duplicate: str | None = None
    match_source: str | None = None
    target_type: str
    target_url: str
    target_name: str | None = None
    stop_on_match: bool
    created_at: str | None = None
    updated_at: str | None = None


class ForwardRuleListResponse(APIResponse[list[ForwardRuleSchema]]):
    """转发规则列表响应"""


class ForwardRuleDetailResponse(APIResponse[ForwardRuleSchema]):
    """转发规则详情响应"""


# ── Admin & Maintenance Schemas ──────────────────────────────────────────────


class DeadLetterItem(BaseModel):
    """Dead letter 单条记录"""

    id: int
    source: str | None = None
    timestamp: str | None = None
    processing_status: str | None = None
    retry_count: int | None = None
    created_at: str | None = None


class DeadLetterPagination(BaseModel):
    page: int
    page_size: int
    total: int | None = None


class DeadLetterListResponse(BaseModel):
    """Dead letter 列表响应"""

    success: bool
    data: list[DeadLetterItem]
    pagination: DeadLetterPagination


class ReplayResponse(BaseModel):
    """单条 dead letter 重放响应"""

    success: bool
    message: str
    event_id: int


class ReplayAllResponse(BaseModel):
    """批量重放响应"""

    success: bool
    message: str
    replayed: int
    event_ids: list[int] = []


class StuckEventItem(BaseModel):
    id: int
    source: str | None = None
    processing_status: str | None = None
    retry_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class StuckEventListResponse(BaseModel):
    success: bool
    data: list[StuckEventItem]


class StuckEventRequeueResponse(BaseModel):
    success: bool
    message: str
    event_id: int


class ConfigResponse(APIResponse[dict[str, Any]]):
    """配置读取响应"""


class ConfigUpdateResponse(BaseModel):
    """配置更新响应"""

    success: bool
    message: str


class ConfigSourceItem(BaseModel):
    key: str
    source: str
    updated_at: str | None = None
    updated_by: str | None = None


class ConfigSourcesResponse(APIResponse[list[ConfigSourceItem]]):
    """配置来源响应"""


class PromptGetResponse(BaseModel):
    """获取 Prompt 模板响应"""

    success: bool
    template: str
    source: str


class PromptReloadResponse(BaseModel):
    """重载 Prompt 模板响应"""

    success: bool
    message: str
    template_length: int
    preview: str


# ── AI Usage Schemas ─────────────────────────────────────────────────────────


class TokenStats(BaseModel):
    total: int
    input: int
    output: int


class CostStats(BaseModel):
    total: float
    saved_estimate: float


class CacheStatistics(BaseModel):
    total_cache_entries: int
    total_hits: int
    avg_hits_per_entry: float
    cache_hit_rate: float
    saved_calls: int


class TrendPoint(BaseModel):
    time: str
    total_calls: int
    ai_calls: int
    rule_calls: int
    tokens: int
    cost: float


class AIUsageData(BaseModel):
    """AI 使用统计数据"""

    total_calls: int
    route_breakdown: dict[str, int]
    percentages: dict[str, float]
    tokens: TokenStats
    cost: CostStats
    cache_statistics: CacheStatistics
    trend: list[TrendPoint]


class AIUsageResponse(APIResponse[AIUsageData]):
    """AI 使用统计响应"""
