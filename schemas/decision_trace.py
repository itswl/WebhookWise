from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .base import APIResponse, CursorPaginationInfo


class DecisionTraceStatsData(BaseModel):
    """Aggregate forward/skip outcomes over a time window."""

    period: str
    total: int
    forwarded: int
    skipped: int
    outcome_breakdown: dict[str, int]
    skip_code_breakdown: dict[str, int]


class DecisionTraceStatsResponse(APIResponse[DecisionTraceStatsData]):
    """Decision-trace aggregate statistics response."""


class DecisionTraceQualityData(BaseModel):
    """Proxy signals for AI-judgment quality (no human ground truth exists)."""

    period: str
    total: int
    ai_total: int
    route_breakdown: dict[str, int]
    override_count: int
    override_rate: float
    degraded_total: int
    degraded_rate: float
    degraded_reasons: dict[str, int]
    ai_importance_breakdown: dict[str, int]
    ai_importance_by_source: dict[str, dict[str, int]]


class DecisionTraceQualityResponse(APIResponse[DecisionTraceQualityData]):
    """AI-judgment quality statistics response."""


class OverviewData(BaseModel):
    """One-screen operational summary for the Overview home page."""

    period: str
    total: int
    forwarded: int
    skipped: int
    forward_rate: float
    skip_code_breakdown: dict[str, int]
    top_sources: list[dict[str, Any]]
    delivery: dict[str, Any]


class OverviewResponse(APIResponse[OverviewData]):
    """Overview summary response."""


class DecisionTraceItem(BaseModel):
    """One decision trace: the flattened outcome plus the full ordered chain."""

    id: int
    webhook_event_id: int
    created_at: str | None = None
    outcome: str
    skip_code: str
    source: str | None = None
    importance: str | None = None
    is_periodic_reminder: bool = False
    route: str | None = None
    importance_override: bool = False
    degraded_reason: str | None = None
    matched_rules: list[str] = []
    steps: list[dict[str, Any]] = []
    # Present only on forwarded rows with an outbox record: delivery outcome
    # (sent / pending / failed) + the most actionable target detail.
    delivery: dict[str, Any] | None = None


class DecisionTraceListResponse(BaseModel):
    """Cursor-paginated decision-trace list."""

    success: bool
    data: list[DecisionTraceItem]
    pagination: CursorPaginationInfo
