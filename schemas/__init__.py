"""
schemas/
=========
Pydantic V2 响应模型，为所有 API 端点提供类型保障与 OpenAPI 文档。
"""

from schemas.admin import (
    AIUsageData,
    AIUsageResponse,
    CacheStatistics,
    ConfigResponse,
    ConfigUpdateResponse,
    CostStats,
    DeadLetterItem,
    DeadLetterListResponse,
    DeadLetterPagination,
    PromptGetResponse,
    PromptReloadResponse,
    ReplayAllResponse,
    ReplayResponse,
    TokenStats,
    TrendPoint,
)
from schemas.analysis import (
    DeepAnalysisListData,
    DeepAnalysisListResponse,
    DeepAnalysisRecord,
    ReanalysisResponse,
)
from schemas.base import APIResponse, CursorPaginationInfo, ErrorResponse
from schemas.rules import (
    ForwardRuleDetailResponse,
    ForwardRuleListResponse,
    ForwardRuleSchema,
)
from schemas.webhook import (
    HealthData,
    HealthResponse,
    WebhookDetailResponse,
    WebhookEventFull,
    WebhookEventSummary,
    WebhookListResponse,
    WebhookReceiveResponse,
)

__all__ = [
    # base
    "APIResponse",
    "CursorPaginationInfo",
    "ErrorResponse",
    # webhook
    "HealthData",
    "HealthResponse",
    "WebhookDetailResponse",
    "WebhookEventFull",
    "WebhookEventSummary",
    "WebhookListResponse",
    "WebhookReceiveResponse",
    # analysis
    "DeepAnalysisListData",
    "DeepAnalysisListResponse",
    "DeepAnalysisRecord",
    "ReanalysisResponse",
    # rules
    "ForwardRuleDetailResponse",
    "ForwardRuleListResponse",
    "ForwardRuleSchema",
    # admin
    "AIUsageData",
    "AIUsageResponse",
    "CacheStatistics",
    "ConfigResponse",
    "ConfigUpdateResponse",
    "CostStats",
    "DeadLetterItem",
    "DeadLetterListResponse",
    "DeadLetterPagination",
    "PromptGetResponse",
    "PromptReloadResponse",
    "ReplayAllResponse",
    "ReplayResponse",
    "TokenStats",
    "TrendPoint",
]
