"""Redis-backed AI analysis cache."""

import orjson

from core.logger import logger
from services.analysis.ai_policies import AICachePolicy
from services.webhooks.types import AnalysisResult


def get_cache_key(alert_hash: str) -> str:
    return f"analysis_{alert_hash}"


async def get_cached_analysis(alert_hash: str, *, policy: AICachePolicy | None = None) -> AnalysisResult | None:
    policy = policy or AICachePolicy.from_config()
    if not policy.enabled:
        return None
    try:
        from core.redis_client import redis_get_str, redis_incr_with_expire

        ck = get_cache_key(alert_hash)
        cached_json = await redis_get_str(ck)
        if not cached_json:
            return None
        parsed = orjson.loads(cached_json)
        if not isinstance(parsed, dict):
            return None
        res: AnalysisResult = dict(parsed)
        counter_key = f"{ck}:hits"
        hits = await redis_incr_with_expire(counter_key, policy.ttl_seconds)
        res.update({"_cache_hit": True, "_cache_hit_count": hits})
        return res
    except Exception as e:
        logger.warning("读取缓存失败: %s", e)
        return None


async def save_to_cache(
    alert_hash: str, analysis_result: AnalysisResult, *, policy: AICachePolicy | None = None
) -> bool:
    policy = policy or AICachePolicy.from_config()
    if not policy.enabled:
        return False
    try:
        from core.redis_client import redis_setex_bytes, redis_setex_str

        ck = get_cache_key(alert_hash)
        res_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}
        cached_bytes = orjson.dumps(res_to_cache)
        counter_key = f"{ck}:hits"
        await redis_setex_bytes(ck, policy.ttl_seconds, cached_bytes)
        await redis_setex_str(counter_key, policy.ttl_seconds, "0")
        return True
    except Exception as e:
        logger.warning("保存缓存失败: %s", e)
        return False
