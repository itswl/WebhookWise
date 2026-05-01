"""AI 分析结果缓存管理模块

基于 Redis 的分析结果缓存，支持 SETEX 自动过期和命中计数。
"""

import contextlib
import logging

import orjson

from core.config import Config
from core.config_provider import policies

logger = logging.getLogger("webhook_service.ai_cache")


def get_cache_key(alert_hash: str) -> str:
    """生成缓存 key"""
    return f"analysis_{alert_hash}"


async def get_cached_analysis(alert_hash: str) -> dict | None:
    """从 Redis 获取缓存的分析结果"""
    if not Config.ai.CACHE_ENABLED:
        return None
    try:
        from core.redis_client import get_redis

        redis_client = get_redis()
        cache_key = get_cache_key(alert_hash)

        cached_json = await redis_client.get(cache_key)
        if not cached_json:
            logger.debug("缓存未命中: %s...", cache_key[:20])
            return None

        cached_result = orjson.loads(cached_json)

        # 增加命中计数（pipeline 合并 RTT）
        counter_key = f"{cache_key}:hits"
        pipe = redis_client.pipeline()
        pipe.incr(counter_key)
        pipe.expire(counter_key, Config.ai.ANALYSIS_CACHE_TTL)
        results = await pipe.execute()
        hit_count = results[0]

        cached_result["_cache_hit"] = True
        cached_result["_cache_hit_count"] = hit_count

        logger.info(f"缓存命中: {cache_key[:20]}..., 已命中 {hit_count} 次")
        return cached_result
    except Exception as e:
        logger.warning(f"读取缓存失败: {e}")
        return None


async def save_to_cache(alert_hash: str, analysis_result: dict) -> bool:
    """将分析结果保存到 Redis（SETEX 自动过期）"""
    if not Config.ai.CACHE_ENABLED:
        return False
    try:
        from core.redis_client import get_redis

        redis_client = get_redis()
        cache_key = get_cache_key(alert_hash)

        # 清理内部字段（以 _ 开头的）
        result_to_cache = {k: v for k, v in analysis_result.items() if not k.startswith("_")}

        cached_bytes = orjson.dumps(result_to_cache)

        # pipeline 合并两次 SETEX，减少 RTT
        counter_key = f"{cache_key}:hits"
        pipe = redis_client.pipeline()
        pipe.setex(cache_key, Config.ai.ANALYSIS_CACHE_TTL, cached_bytes)
        pipe.setex(counter_key, Config.ai.ANALYSIS_CACHE_TTL, "0")
        await pipe.execute()

        logger.info(f"分析结果已缓存到 Redis: {cache_key[:20]}..., TTL={Config.ai.ANALYSIS_CACHE_TTL}s")

        # 发布完成事件，通知等待中的 Worker（Pub/Sub 不保证送达，仅作加速）
        channel = f"analysis_done:{alert_hash}"
        with contextlib.suppress(Exception):
            await redis_client.publish(channel, "1")

        return True
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")
        return False


async def log_ai_usage(
    route_type: str,
    alert_hash: str,
    source: str,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False,
) -> None:
    """
    记录 AI 使用日志

    Args:
        route_type: 路由类型 ('ai', 'rule', 'cache')
        alert_hash: 告警哈希
        source: 告警来源
        model: 使用的模型名称
        tokens_in: 输入 token 数
        tokens_out: 输出 token 数
        cache_hit: 是否命中缓存
    """
    try:
        from db.session import session_scope
        from models import AIUsageLog

        # 计算估算成本
        cost_estimate = 0.0
        if route_type == "ai" and tokens_in > 0:
            cost_estimate = (tokens_in / 1000) * Config.ai.AI_COST_PER_1K_INPUT_TOKENS + (
                tokens_out / 1000
            ) * Config.ai.AI_COST_PER_1K_OUTPUT_TOKENS

        async with session_scope() as session:
            usage_log = AIUsageLog(
                model=model or policies.ai.OPENAI_MODEL,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_estimate=cost_estimate,
                cache_hit=cache_hit,
                route_type=route_type,
                alert_hash=alert_hash,
                source=source,
            )
            session.add(usage_log)
            logger.debug(
                "AI 使用记录: type=%s, tokens=%s+%s, cost=$%.6f", route_type, tokens_in, tokens_out, cost_estimate
            )

    except Exception as e:
        logger.warning(f"记录 AI 使用日志失败: {e}")
