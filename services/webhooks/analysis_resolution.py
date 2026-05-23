"""AI analysis resolution stage for webhook processing."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast

from core.logger import get_logger
from db.session import session_scope
from services.analysis.ai_analyzer import analyze_webhook_with_ai, log_ai_usage
from services.webhooks.deduplication import CachedDuplicate, get_cached_duplicate
from services.webhooks.policies import AnalysisResolutionPolicy
from services.webhooks.repository import DuplicateCheckResult, check_duplicate_event
from services.webhooks.types import AnalysisResolution, AnalysisResult

logger = get_logger("webhooks.analysis_resolution")


class _AnalysisAction(StrEnum):
    REUSE_REDIS_CACHE = "reuse_redis_cache"
    REUSE_RECENT_BEYOND_WINDOW = "reuse_recent_beyond_window"
    REUSE_ORIGINAL_BEYOND_WINDOW = "reuse_original_beyond_window"
    REUSE_IN_WINDOW = "reuse_in_window"
    ANALYZE = "analyze"


@dataclass(frozen=True, slots=True)
class _ResolutionPlan:
    action: _AnalysisAction
    analysis: Mapping[str, Any] | None
    route_type: Literal["redis_reuse", "db_reuse"] | None
    reanalyzed: bool
    is_duplicate: bool
    original_event: Any | None
    beyond_window: bool
    is_reused: bool
    original_event_id: int | None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _ResolutionState:
    cached: CachedDuplicate | None
    check: DuplicateCheckResult
    policy: AnalysisResolutionPolicy
    now: datetime

    @property
    def original_event(self) -> Any | None:
        return self.check.original_event

    @property
    def last_beyond_window_event(self) -> Any | None:
        return self.check.last_beyond_window_event

    @property
    def original_event_id(self) -> int | None:
        event_id = getattr(self.original_event, "id", None)
        return event_id if isinstance(event_id, int) else None

    @property
    def cached_matches_current_window(self) -> bool:
        return (
            self.cached is not None
            and _has_reusable_analysis(self.cached.analysis)
            and self.check.is_duplicate
            and not self.check.beyond_window
            and self.original_event is not None
            and self.cached.original_event_id == self.original_event_id
        )

    @property
    def recent_beyond_window_reusable(self) -> bool:
        event = self.last_beyond_window_event
        if event is None:
            return False
        created_at = getattr(event, "created_at", None)
        return (
            isinstance(created_at, datetime)
            and (self.now - created_at).total_seconds() < self.policy.recent_beyond_window_reuse_seconds
            and _has_reusable_analysis(_event_analysis(event))
        )

    @property
    def original_analysis_reusable(self) -> bool:
        return _has_reusable_analysis(_event_analysis(self.original_event))

    @property
    def in_window_reuse_event(self) -> Any | None:
        last_beyond_analysis = _event_analysis(self.last_beyond_window_event)
        if self.last_beyond_window_event is not None and last_beyond_analysis:
            return self.last_beyond_window_event
        return self.original_event

    @property
    def in_window_analysis_reusable(self) -> bool:
        return _has_reusable_analysis(_event_analysis(self.in_window_reuse_event))


def _event_analysis(event: Any | None) -> Mapping[str, Any] | None:
    analysis = getattr(event, "ai_analysis", None)
    return analysis if isinstance(analysis, Mapping) else None


def _has_reusable_analysis(analysis: Mapping[str, Any] | None) -> bool:
    if not analysis:
        return False
    return not analysis.get("_degraded")


def _analysis_with_route(analysis: Mapping[str, Any], route_type: Literal["redis_reuse", "db_reuse"]) -> AnalysisResult:
    routed = cast(AnalysisResult, dict(analysis))
    routed["_route_type"] = route_type
    return routed


def _reuse_plan(
    state: _ResolutionState,
    action: _AnalysisAction,
    analysis: Mapping[str, Any],
    route_type: Literal["redis_reuse", "db_reuse"],
    *,
    is_duplicate: bool,
    beyond_window: bool,
    is_reused: bool = False,
    original_event_id: int | None = None,
) -> _ResolutionPlan:
    return _ResolutionPlan(
        action=action,
        analysis=analysis,
        route_type=route_type,
        reanalyzed=False,
        is_duplicate=is_duplicate,
        original_event=state.original_event,
        beyond_window=beyond_window,
        is_reused=is_reused,
        original_event_id=original_event_id if original_event_id is not None else state.original_event_id,
    )


def _redis_cache_plan(state: _ResolutionState) -> _ResolutionPlan:
    cached = cast(CachedDuplicate, state.cached)
    return _reuse_plan(
        state,
        _AnalysisAction.REUSE_REDIS_CACHE,
        cast(Mapping[str, Any], cached.analysis),
        "redis_reuse",
        is_duplicate=True,
        beyond_window=False,
        is_reused=True,
        original_event_id=cached.original_event_id,
    )


def _recent_beyond_window_plan(state: _ResolutionState) -> _ResolutionPlan:
    return _reuse_plan(
        state,
        _AnalysisAction.REUSE_RECENT_BEYOND_WINDOW,
        cast(Mapping[str, Any], _event_analysis(state.last_beyond_window_event)),
        "db_reuse",
        is_duplicate=True,
        beyond_window=True,
    )


def _original_beyond_window_plan(state: _ResolutionState) -> _ResolutionPlan:
    return _reuse_plan(
        state,
        _AnalysisAction.REUSE_ORIGINAL_BEYOND_WINDOW,
        cast(Mapping[str, Any], _event_analysis(state.original_event)),
        "db_reuse",
        is_duplicate=True,
        beyond_window=True,
    )


def _in_window_reuse_plan(state: _ResolutionState) -> _ResolutionPlan:
    return _reuse_plan(
        state,
        _AnalysisAction.REUSE_IN_WINDOW,
        cast(Mapping[str, Any], _event_analysis(state.in_window_reuse_event)),
        "db_reuse",
        is_duplicate=True,
        beyond_window=False,
    )


def _analysis_reason(state: _ResolutionState) -> str:
    if state.check.beyond_window and state.original_event is not None:
        if state.policy.reanalyze_after_time_window:
            return "reanalyze_enabled"
        return "prev_degraded_or_missing"
    if state.check.is_duplicate and state.original_event is not None:
        return "prev_degraded_or_missing"
    return "new_event"


def _analyze_plan(state: _ResolutionState) -> _ResolutionPlan:
    return _ResolutionPlan(
        action=_AnalysisAction.ANALYZE,
        analysis=None,
        route_type=None,
        reanalyzed=True,
        is_duplicate=state.check.is_duplicate,
        original_event=state.original_event,
        beyond_window=state.check.beyond_window,
        is_reused=False,
        original_event_id=state.original_event_id,
        reason=_analysis_reason(state),
    )


def _matches_redis_cache(state: _ResolutionState) -> bool:
    return state.cached_matches_current_window


def _matches_recent_beyond_window(state: _ResolutionState) -> bool:
    return state.check.beyond_window and state.original_event is not None and state.recent_beyond_window_reusable


def _matches_original_beyond_window(state: _ResolutionState) -> bool:
    return (
        state.check.beyond_window
        and state.original_event is not None
        and not state.policy.reanalyze_after_time_window
        and state.original_analysis_reusable
    )


def _matches_in_window_reuse(state: _ResolutionState) -> bool:
    return state.check.is_duplicate and state.original_event is not None and state.in_window_analysis_reusable


def _plan_analysis_resolution(
    cached: CachedDuplicate | None,
    check: DuplicateCheckResult,
    policy: AnalysisResolutionPolicy,
    *,
    now: datetime | None = None,
) -> _ResolutionPlan:
    state = _ResolutionState(cached=cached, check=check, policy=policy, now=now or datetime.now())
    if _matches_redis_cache(state):
        return _redis_cache_plan(state)
    if _matches_recent_beyond_window(state):
        return _recent_beyond_window_plan(state)
    if _matches_original_beyond_window(state):
        return _original_beyond_window_plan(state)
    if _matches_in_window_reuse(state):
        return _in_window_reuse_plan(state)
    return _analyze_plan(state)


def _log_reuse_plan(plan: _ResolutionPlan, alert_hash: str) -> None:
    if plan.action is _AnalysisAction.REUSE_REDIS_CACHE:
        logger.debug(
            "[Pipeline] 窗口内复用 Redis 去重缓存 orig_id=%s hash=%s...",
            plan.original_event_id,
            alert_hash[:12],
        )
    elif plan.action is _AnalysisAction.REUSE_RECENT_BEYOND_WINDOW:
        logger.debug(
            "[Pipeline] 窗口外复用最近 beyond_window 事件分析 orig_id=%s hash=%s...",
            plan.original_event_id,
            alert_hash[:12],
        )
    elif plan.action is _AnalysisAction.REUSE_ORIGINAL_BEYOND_WINDOW:
        logger.debug("[Pipeline] 窗口外复用原始事件分析 orig_id=%s hash=%s...", plan.original_event_id, alert_hash[:12])
    elif plan.action is _AnalysisAction.REUSE_IN_WINDOW:
        logger.debug("[Pipeline] 窗口内复用原始事件分析 orig_id=%s hash=%s...", plan.original_event_id, alert_hash[:12])


def _log_analyze_plan(plan: _ResolutionPlan, alert_hash: str) -> None:
    if plan.beyond_window and plan.original_event is not None:
        logger.debug(
            "[Pipeline] 窗口外重新分析 orig_id=%s reason=%s hash=%s...",
            plan.original_event_id,
            plan.reason,
            alert_hash[:12],
        )
    elif plan.is_duplicate and plan.original_event is not None:
        logger.debug(
            "[Pipeline] 窗口内重新分析 orig_id=%s reason=%s hash=%s...",
            plan.original_event_id,
            plan.reason,
            alert_hash[:12],
        )
    else:
        logger.debug("[Pipeline] 新事件，发起 AI 分析 hash=%s...", alert_hash[:12])


async def resolve_analysis(
    alert_hash: str,
    full_data: dict[str, Any],
    *,
    policy: AnalysisResolutionPolicy | None = None,
    http_client: Any | None = None,
) -> AnalysisResolution:
    policy = policy or AnalysisResolutionPolicy.from_config()
    cached = await get_cached_duplicate(alert_hash)

    async with session_scope() as session:
        check = await check_duplicate_event(
            alert_hash, session=session, time_window_hours=policy.duplicate_window_hours
        )
    orig = check.original_event
    plan = _plan_analysis_resolution(cached, check, policy)

    if cached and _has_reusable_analysis(cached.analysis) and plan.action is not _AnalysisAction.REUSE_REDIS_CACHE:
        logger.debug(
            "[Pipeline] Redis 去重缓存已过窗口或与 DB 不一致，改走 DB 判定 hash=%s... cached_orig=%s db_orig=%s beyond=%s",
            alert_hash[:12],
            cached.original_event_id,
            orig.id if orig else None,
            check.beyond_window,
        )

    if plan.analysis is not None and plan.route_type is not None:
        _log_reuse_plan(plan, alert_hash)
        await log_ai_usage(route_type="reuse", alert_hash=alert_hash, source=full_data.get("source", ""))
        return AnalysisResolution(
            _analysis_with_route(plan.analysis, plan.route_type),
            reanalyzed=plan.reanalyzed,
            is_duplicate=plan.is_duplicate,
            original_event=plan.original_event,
            beyond_window=plan.beyond_window,
            is_reused=plan.is_reused,
            original_event_id=plan.original_event_id,
        )

    _log_analyze_plan(plan, alert_hash)
    res = await analyze_webhook_with_ai(full_data, http_client=http_client)
    return AnalysisResolution(
        res,
        reanalyzed=plan.reanalyzed,
        is_duplicate=plan.is_duplicate,
        original_event=plan.original_event,
        beyond_window=plan.beyond_window,
        original_event_id=plan.original_event_id,
    )
