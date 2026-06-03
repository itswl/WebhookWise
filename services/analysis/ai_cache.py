"""Redis-backed AI analysis cache."""

import time
from typing import cast

from core import json
from redis.exceptions import RedisError
from core.app_context import get_config_manager
from core.logger import get_logger
from core.observability.metrics import AI_CACHE_OPERATION_DURATION_SECONDS, AI_CACHE_REQUESTS_TOTAL
from services.webhooks.types import AnalysisResult, mark_cache_hit

logger = get_logger("analysis.ai_cache")


def get_cache_key(alert_hash: str) -> str:
    return f"analysis_{alert_hash}"


def _resolve_cache_settings(*, enabled: bool | None, ttl_seconds: int | None) -> tuple[bool, int]:
    config = get_config_manager().ai
    resolved_enabled = bool(config.CACHE_ENABLED) if enabled is None else bool(enabled)
    resolved_ttl = int(config.ANALYSIS_CACHE_TTL) if ttl_seconds is None else int(ttl_seconds)
    return resolved_enabled, max(1, resolved_ttl)


async def get_cached_analysis(
    alert_hash: str, *, enabled: bool | None = None, ttl_seconds: int | None = None
) -> AnalysisResult | None:
    enabled_resolved, ttl_resolved = _resolve_cache_settings(enabled=enabled, ttl_seconds=ttl_seconds)
    if not enabled_resolved:
        AI_CACHE_REQUESTS_TOTAL.labels("get", "disabled").inc()
        return None
    start = time.perf_counter()
    result = "miss"
    try:
        from core.redis_client import redis_get_str, redis_incr_with_expire

        ck = get_cache_key(alert_hash)
        cached_json = await redis_get_str(ck)
        if not cached_json:
            return None
        parsed = json.loads(cached_json)
        if not isinstance(parsed, dict):
            result = "invalid"
            return None
        res = cast(AnalysisResult, dict(parsed))
        counter_key = f"{ck}:hits"
        hits = await redis_incr_with_expire(counter_key, ttl_resolved)
        mark_cache_hit(res, hits)
        result = "hit"
        return res
    except (RedisError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as e:
        result = "error"
        logger.warning("读取缓存失败: %s", e)
        return None
    finally:
        AI_CACHE_REQUESTS_TOTAL.labels("get", result).inc()
        AI_CACHE_OPERATION_DURATION_SECONDS.labels("get", result).observe(time.perf_counter() - start)


async def save_to_cache(
    alert_hash: str, analysis_result: AnalysisResult, *, enabled: bool | None = None, ttl_seconds: int | None = None
) -> bool:
    enabled_resolved, ttl_resolved = _resolve_cache_settings(enabled=enabled, ttl_seconds=ttl_seconds)
    if not enabled_resolved:
        AI_CACHE_REQUESTS_TOTAL.labels("set", "disabled").inc()
        return False
    start = time.perf_counter()
    result = "success"
    try:
        from core.redis_client import redis_setex_bytes, redis_setex_str

        ck = get_cache_key(alert_hash)
        res_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}
        cached_bytes = json.dumps_bytes(res_to_cache)
        counter_key = f"{ck}:hits"
        await redis_setex_bytes(ck, ttl_resolved, cached_bytes)
        await redis_setex_str(counter_key, ttl_resolved, "0")
        return True
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        result = "error"
        logger.warning("保存缓存失败: %s", e)
        return False
    finally:
        AI_CACHE_REQUESTS_TOTAL.labels("set", result).inc()
        AI_CACHE_OPERATION_DURATION_SECONDS.labels("set", result).observe(time.perf_counter() - start)
