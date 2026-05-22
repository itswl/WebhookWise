from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from services.webhooks.analysis_resolution import _AnalysisAction, _plan_analysis_resolution
from services.webhooks.deduplication import CachedDuplicate
from services.webhooks.policies import AnalysisResolutionPolicy
from services.webhooks.repository import DuplicateCheckResult

NOW = datetime(2026, 5, 22, 12, 0, 0)


@dataclass
class _Event:
    id: int
    ai_analysis: dict[str, Any] | None = None
    created_at: datetime | None = None


def _policy(*, reanalyze: bool = True, recent_reuse_seconds: int = 60) -> AnalysisResolutionPolicy:
    return AnalysisResolutionPolicy(
        duplicate_window_hours=24,
        recent_beyond_window_reuse_seconds=recent_reuse_seconds,
        reanalyze_after_time_window=reanalyze,
    )


def _check(
    *,
    is_duplicate: bool,
    original: _Event | None,
    beyond_window: bool,
    last_beyond: _Event | None = None,
) -> DuplicateCheckResult:
    return DuplicateCheckResult(is_duplicate, original, beyond_window, last_beyond)  # type: ignore[arg-type]


def test_analysis_resolution_decision_table_covers_core_paths() -> None:
    original = _Event(10, {"importance": "high", "summary": "original"})
    recent_beyond = _Event(
        11,
        {"importance": "high", "summary": "recent beyond"},
        created_at=NOW - timedelta(seconds=10),
    )

    cases = [
        (
            "redis cache hit inside current window",
            CachedDuplicate(10, {"importance": "high", "summary": "cached"}),
            _check(is_duplicate=True, original=original, beyond_window=False),
            _policy(),
            _AnalysisAction.REUSE_REDIS_CACHE,
            "redis_reuse",
            False,
            True,
            False,
            True,
            10,
            {"importance": "high", "summary": "cached"},
        ),
        (
            "beyond window reuses recent beyond event",
            None,
            _check(is_duplicate=False, original=original, beyond_window=True, last_beyond=recent_beyond),
            _policy(reanalyze=True),
            _AnalysisAction.REUSE_RECENT_BEYOND_WINDOW,
            "db_reuse",
            False,
            True,
            True,
            False,
            10,
            {"importance": "high", "summary": "recent beyond"},
        ),
        (
            "beyond window reuses original when reanalysis is disabled",
            None,
            _check(is_duplicate=False, original=original, beyond_window=True),
            _policy(reanalyze=False),
            _AnalysisAction.REUSE_ORIGINAL_BEYOND_WINDOW,
            "db_reuse",
            False,
            True,
            True,
            False,
            10,
            {"importance": "high", "summary": "original"},
        ),
        (
            "beyond window reanalyzes when policy requires it",
            None,
            _check(is_duplicate=False, original=original, beyond_window=True),
            _policy(reanalyze=True),
            _AnalysisAction.ANALYZE,
            None,
            True,
            False,
            True,
            False,
            10,
            None,
        ),
        (
            "in-window duplicate reuses DB analysis",
            None,
            _check(is_duplicate=True, original=original, beyond_window=False),
            _policy(),
            _AnalysisAction.REUSE_IN_WINDOW,
            "db_reuse",
            False,
            True,
            False,
            False,
            10,
            {"importance": "high", "summary": "original"},
        ),
        (
            "new event runs AI analysis",
            None,
            _check(is_duplicate=False, original=None, beyond_window=False),
            _policy(),
            _AnalysisAction.ANALYZE,
            None,
            True,
            False,
            False,
            False,
            None,
            None,
        ),
    ]

    for (
        label,
        cached,
        check,
        policy,
        action,
        route_type,
        reanalyzed,
        is_duplicate,
        beyond_window,
        is_reused,
        original_event_id,
        analysis,
    ) in cases:
        plan = _plan_analysis_resolution(cached, check, policy, now=NOW)

        assert plan.action is action, label
        assert plan.route_type == route_type, label
        assert plan.reanalyzed is reanalyzed, label
        assert plan.is_duplicate is is_duplicate, label
        assert plan.beyond_window is beyond_window, label
        assert plan.is_reused is is_reused, label
        assert plan.original_event_id == original_event_id, label
        assert plan.analysis == analysis, label


@pytest.mark.parametrize("missing_analysis", [None, {}, {"_degraded": True, "summary": "fallback"}])
def test_analysis_resolution_requires_usable_analysis_before_reuse(missing_analysis: dict[str, Any] | None) -> None:
    original = _Event(10, missing_analysis)
    recent_beyond = _Event(11, missing_analysis, created_at=NOW - timedelta(seconds=5))

    plan = _plan_analysis_resolution(
        None,
        _check(is_duplicate=False, original=original, beyond_window=True, last_beyond=recent_beyond),
        _policy(reanalyze=False),
        now=NOW,
    )

    assert plan.action is _AnalysisAction.ANALYZE
    assert plan.reanalyzed is True
    assert plan.beyond_window is True
    assert plan.analysis is None
