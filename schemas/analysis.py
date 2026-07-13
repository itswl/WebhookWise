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
    """AI analysis result model"""

    source: str = Field(description="Source system name")
    event_type: str = Field(description="Detailed event type/name")
    importance: Importance = Field(description="Event importance level", default=Importance.MEDIUM)
    summary: str = Field(
        description="Event summary (in Chinese, within 50 characters, including key metrics or error information)"
    )
    alert_identity: dict[str, Any] = Field(
        default_factory=dict,
        description="Key fields used to distinguish alert attribution and instances, e.g. project, region, namespace, service, resource, rule, and metric",
    )
    impact_scope: str | None = Field(None, description="Impact scope assessment")
    actions: list[str] = Field(default_factory=list, description="List of recommended response actions")
    risks: list[str] = Field(default_factory=list, description="List of potential related risks")
    monitoring_suggestions: list[str] = Field(
        default_factory=list, description="Follow-up monitoring optimization suggestions"
    )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class IncidentSummaryResult(BaseModel):
    """Structured post-incident analysis returned by the shared AI client."""

    summary: str = Field(min_length=1, max_length=1000)
    root_cause: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    timeline_summary: str = Field(min_length=1, max_length=4000)
    recommendations: list[str] = Field(default_factory=list, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)


class DeepAnalysisRecord(BaseModel):
    """Deep analysis record -- corresponds to DeepAnalysis"""

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
    """Deep analysis list item (lightweight).

    The list view only needs to render metadata plus a one-line preview; it does
    not need the full normalized_report, much less the raw analysis_result (which
    contains the large _openclaw_text blob). The full content is fetched on demand
    via the detail endpoint when expanded.
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
    """Deep analysis list data (with pagination)"""

    total: int | None = None
    total_pages: int | None = None
    page: int | None = None
    per_page: int
    next_cursor: int | None = None
    has_more: bool = False
    items: list[DeepAnalysisSummary]


class DeepAnalysisListResponse(BaseModel):
    """Deep analysis list response"""

    success: bool
    data: DeepAnalysisListData


class ReanalysisResponse(BaseModel):
    """Re-analysis response"""

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
