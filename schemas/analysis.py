from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts.deep_analysis_report import normalize_deep_analysis_report, summarize_deep_analysis_preview
from core.datetime_utils import utc_isoformat


class Importance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WebhookAnalysisResult(BaseModel):
    """AI 分析结果模型"""

    source: str = Field(description="来源系统名称")
    event_type: str = Field(description="详细的事件类型/名称")
    importance: Importance = Field(description="事件重要程度", default=Importance.MEDIUM)
    summary: str = Field(description="事件摘要（中文，50字内，包含关键指标或报错信息）")
    alert_identity: dict[str, Any] = Field(
        default_factory=dict,
        description="用于区分告警归属和实例的关键字段，例如项目、区域、命名空间、服务、资源、规则和指标",
    )
    impact_scope: str | None = Field(None, description="影响范围评估")
    actions: list[str] = Field(default_factory=list, description="建议的响应操作列表")
    risks: list[str] = Field(default_factory=list, description="潜在的关联风险列表")
    monitoring_suggestions: list[str] = Field(default_factory=list, description="后续的监控优化建议")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DeepAnalysisRecord(BaseModel):
    """深度分析记录 —— 对应 DeepAnalysis"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    webhook_event_id: int
    engine: str | None = None
    user_question: str | None = None
    analysis_result: dict[str, Any] | str | None = None
    normalized_report: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float | None = None
    created_at: datetime | str | None = None
    openclaw_run_id: str | None = None
    openclaw_session_key: str | None = None
    status: str | None = None
    poll_attempts: int | None = None
    next_poll_at: datetime | str | None = None
    last_polled_at: datetime | str | None = None
    source: str | None = None
    is_duplicate: bool = False


class DeepAnalysisSummary(BaseModel):
    """深度分析列表项（轻量）。

    列表视图只需渲染元信息 + 一句预览,不需要完整 normalized_report,更不需要
    原始 analysis_result(含 _openclaw_text 大 blob)。完整内容在展开时经详情
    接口按需获取。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    webhook_event_id: int
    engine: str | None = None
    user_question: str | None = None
    summary_preview: str = ""
    duration_seconds: float | None = None
    created_at: datetime | str | None = None
    openclaw_run_id: str | None = None
    status: str | None = None
    poll_attempts: int | None = None
    last_polled_at: datetime | str | None = None
    source: str | None = None
    is_duplicate: bool = False


class DeepAnalysisListData(BaseModel):
    """深度分析列表数据（含分页）"""

    total: int | None = None
    total_pages: int | None = None
    page: int | None = None
    per_page: int
    next_cursor: int | None = None
    has_more: bool = False
    items: list[DeepAnalysisSummary]


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
    forward_status: str | None = None
    forward_outbox_ids: list[int] = Field(default_factory=list)
    message: str | None = None


def deep_analysis_to_dict(record: Any) -> dict[str, Any]:
    data = DeepAnalysisRecord.model_validate(record).model_dump()
    for field in ("created_at", "next_poll_at", "last_polled_at"):
        if isinstance(data.get(field), datetime):
            data[field] = utc_isoformat(data[field])
    data["normalized_report"] = normalize_deep_analysis_report(data.get("analysis_result")).to_dict()
    return data


def deep_analysis_to_summary_dict(record: Any) -> dict[str, Any]:
    """Lightweight list-item serializer.

    Cheaply derives a one-line ``summary_preview`` instead of building (and
    shipping) the full normalized report, and omits the raw ``analysis_result``
    blob entirely. Used by the list endpoint to keep page payloads small.
    """
    summary = DeepAnalysisSummary.model_validate(record).model_dump()
    summary["summary_preview"] = summarize_deep_analysis_preview(getattr(record, "analysis_result", None))
    for field in ("created_at", "last_polled_at"):
        if isinstance(summary.get(field), datetime):
            summary[field] = utc_isoformat(summary[field])
    return summary
