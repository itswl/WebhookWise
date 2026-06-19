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
    matched_rules: list[str] = []
    steps: list[dict[str, Any]] = []


class DecisionTraceListResponse(BaseModel):
    """Cursor-paginated decision-trace list."""

    success: bool
    data: list[DecisionTraceItem]
    pagination: CursorPaginationInfo
