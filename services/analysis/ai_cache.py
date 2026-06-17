"""Redis-backed AI analysis cache."""

import time
from typing import cast

from redis.exceptions import RedisError

from core import json
from core.app_context import get_config_manager
from core.logger import get_logger
from core.observability.metrics import AI_CACHE_OPERATION_DURATION_SECONDS, AI_CACHE_REQUESTS_TOTAL
from services.webhooks.types import AnalysisResult, mark_cache_hit

logger = get_logger("analysis.ai_cache")


def _cache_fingerprint() -> str:
    """Short fingerprint of the model + prompt version.

    Folding this into the cache key means changing the model or editing the
    analysis prompt naturally invalidates stale cached results, and two configs
    can never read each other's cache entries for the same alert_hash.
    """
    import hashlib

    from services.analysis.ai_prompt import USER_PROMPT_KIND, get_prompt_version

    model = str(get_config_manager().ai.OPENAI_MODEL or "")
    prompt_version = get_prompt_version(USER_PROMPT_KIND)
    return hashlib.blake2b(f"{model}|{prompt_version}".encode(), digest_size=6).hexdigest()


def get_cache_key(alert_hash: str) -> str:
    return f"analysis_{_cache_fingerprint()}_{alert_hash}"


def _resolve_cache_settings(*, enabled: bool | None, ttl_seconds: int | None) -> tuple[bool, int]:
    config = get_config_manager().ai
    resolved_enabled = bool(config.CACHE_ENABLED) if enabled is None else bool(enabled)
    resolved_ttl = int(config.ANALYSIS_CACHE_TTL_SECONDS) if ttl_seconds is None else int(ttl_seconds)
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
        logger.warning("Failed to read cache: %s", e)
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
        from core.redis_client import redis_eval_int
        from core.redis_lua import AI_CACHE_SAVE

        ck = get_cache_key(alert_hash)
        res_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}
        cached_bytes = json.dumps_bytes(res_to_cache)
        counter_key = f"{ck}:hits"
        # Write the blob and its hit-counter in a single round-trip (was two
        # serial SETEX). KEYS=[blob, counter], ARGV=[ttl, blob bytes].
        await redis_eval_int(AI_CACHE_SAVE, 2, ck, counter_key, ttl_resolved, cached_bytes)
        return True
    except (RedisError, RuntimeError, TypeError, ValueError) as e:
        result = "error"
        logger.warning("Failed to save cache: %s", e)
        return False
    finally:
        AI_CACHE_REQUESTS_TOTAL.labels("set", result).inc()
        AI_CACHE_OPERATION_DURATION_SECONDS.labels("set", result).observe(time.perf_counter() - start)
