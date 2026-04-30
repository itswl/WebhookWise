"""管理端点响应模型"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from schemas.base import APIResponse

# ── Dead Letter ──────────────────────────────────────────────────────────────


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


# ── Config ───────────────────────────────────────────────────────────────────


class ConfigResponse(APIResponse[dict[str, Any]]):
    """配置读取响应"""


class ConfigUpdateResponse(BaseModel):
    """配置更新响应"""

    success: bool
    message: str


# ── Prompt ───────────────────────────────────────────────────────────────────


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


# ── AI Usage ─────────────────────────────────────────────────────────────────


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
