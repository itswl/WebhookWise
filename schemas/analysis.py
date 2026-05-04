"""深度分析相关响应模型"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    """深度分析记录 —— 对应 DeepAnalysis.to_dict()"""

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
