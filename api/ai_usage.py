import json
import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.logger import logger
from core.redis_client import get_redis
from crud.ai_usage import get_ai_usage_stats
from db.session import get_db_session
from schemas.admin import AIUsageResponse

ai_usage_router = APIRouter()


def _ok(data: dict, status_code: int = 200):
    return JSONResponse(content={"success": True, "data": data}, status_code=status_code)


def _fail(msg: str, status_code: int = 500):
    return JSONResponse(content={"success": False, "error": msg}, status_code=status_code)


@ai_usage_router.get("/api/ai-usage", response_model=AIUsageResponse)
async def get_ai_usage_endpoint(period: str = Query("day"), session: AsyncSession = Depends(get_db_session)):
    try:
        cache_bucket = int(time.time() // 60)
        cache_key = f"api:ai_usage:{period}:{cache_bucket}"

        redis = get_redis()
        try:
            cached = await redis.get(cache_key)
            if cached:
                return _ok(json.loads(cached), 200)
        except Exception as e:
            logger.debug(f"AI usage 读取缓存失败: {e}")

        usage_data = await get_ai_usage_stats(session=session, period=period)

        try:
            redis = get_redis()
            await redis.setex(cache_key, 70, json.dumps(usage_data, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"AI usage 缓存写入失败: {e}")

        return _ok(usage_data, 200)

    except Exception as e:
        logger.error(f"获取 AI 使用统计失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)
