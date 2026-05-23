import contextlib
import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.webhook import JSONDict
from db.session import get_db_session
from schemas import AIUsageResponse
from services.analysis.analysis_queries import get_ai_usage_stats

ai_usage_router = APIRouter()


@ai_usage_router.get("/api/ai-usage", response_model=AIUsageResponse)
async def get_ai_usage_endpoint(
    period: str = Query("day"), session: AsyncSession = Depends(get_db_session)
) -> JSONDict:
    from core.redis_client import redis_get_json_dict, redis_setex_json

    cache_key = f"api:ai_usage:{period}:{int(time.time() // 60)}"
    cached_dict = await redis_get_json_dict(cache_key)
    if cached_dict is not None:
        return {"success": True, "data": cached_dict}

    data = await get_ai_usage_stats(session, period)
    with contextlib.suppress(Exception):
        await redis_setex_json(cache_key, 70, data)
    return {"success": True, "data": data}
