"""Read-only Decision Trace API: why each alert was forwarded or skipped."""

import contextlib
import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.webhook import JSONDict
from db.session import get_db_session
from schemas.decision_trace import (
    DecisionTraceListResponse,
    DecisionTraceQualityResponse,
    DecisionTraceStatsResponse,
)
from services.webhooks.decision_trace_queries import (
    get_decision_trace_for_event,
    get_decision_trace_quality_stats,
    get_decision_trace_stats,
    list_decision_traces,
)

decision_trace_router = APIRouter()

_STATS_CACHE_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


@decision_trace_router.get("/decision-traces/stats", response_model=DecisionTraceStatsResponse)
async def get_decision_trace_stats_endpoint(
    # Constrain to the buckets the query understands so an arbitrary period can't
    # mint unlimited distinct Redis cache keys (cache-cardinality abuse).
    period: str = Query("day", pattern="^(day|week|month|year)$"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    from core.redis_client import redis_get_json_dict, redis_setex_json

    cache_key = f"api:decision_trace_stats:{period}:{int(time.time() // 60)}"
    cached_dict = await redis_get_json_dict(cache_key)
    if cached_dict is not None:
        return {"success": True, "data": cached_dict}

    data = await get_decision_trace_stats(session, period)
    with contextlib.suppress(*_STATS_CACHE_ERRORS):
        await redis_setex_json(cache_key, 70, data)
    return {"success": True, "data": data}


@decision_trace_router.get("/decision-traces/quality-stats", response_model=DecisionTraceQualityResponse)
async def get_decision_trace_quality_stats_endpoint(
    period: str = Query("day", pattern="^(day|week|month|year)$"),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    from core.redis_client import redis_get_json_dict, redis_setex_json

    cache_key = f"api:decision_trace_quality:{period}:{int(time.time() // 60)}"
    cached_dict = await redis_get_json_dict(cache_key)
    if cached_dict is not None:
        return {"success": True, "data": cached_dict}

    data = await get_decision_trace_quality_stats(session, period)
    with contextlib.suppress(*_STATS_CACHE_ERRORS):
        await redis_setex_json(cache_key, 70, data)
    return {"success": True, "data": data}


@decision_trace_router.get("/decision-traces", response_model=DecisionTraceListResponse)
async def list_decision_traces_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    cursor: int | None = Query(None),
    outcome: str = Query("", pattern="^(forwarded|skipped|)$"),
    skip_code: str = Query("", max_length=40),
    source: str = Query("", max_length=100),
    session: AsyncSession = Depends(get_db_session),
) -> JSONDict:
    """List decision traces (newest first), each with its full chain inline."""
    items, has_more, next_cursor = await list_decision_traces(
        session,
        cursor=cursor,
        outcome=outcome,
        skip_code=skip_code,
        source=source,
        page=page,
        page_size=page_size,
    )
    return {
        "success": True,
        "data": items,
        "pagination": {"next_cursor": next_cursor, "has_more": has_more, "page_size": page_size},
    }


@decision_trace_router.get("/decision-traces/by-event/{webhook_id}", response_model=None)
async def get_decision_trace_by_event_endpoint(
    webhook_id: int, session: AsyncSession = Depends(get_db_session)
) -> JSONDict | JSONResponse:
    """Get the decision trace for a single webhook event."""
    trace = await get_decision_trace_for_event(session, webhook_id)
    if trace is None:
        return JSONResponse(status_code=404, content={"success": False, "error": "Decision trace not found"})
    return {"success": True, "data": trace}
