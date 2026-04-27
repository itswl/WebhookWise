import json
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from api import _fail, _ok
from core.logger import logger
from core.redis_client import get_redis
from db.session import session_scope
from models import AIUsageLog, AnalysisCache

ai_usage_router = APIRouter()


@ai_usage_router.get('/api/ai-usage')
async async def get_ai_usage(period: str = Query('day')):
    try:
        cache_bucket = int(time.time() // 60)
        cache_key = f"ai_usage:{period}:{cache_bucket}"
        try:
            redis = get_redis()
            cached = await redis.get(cache_key)
            if cached:
                return _ok(json.loads(cached), 200)
        except Exception as e:
            logger.debug(f"AI usage 缓存读取失败: {e}")

        from sqlalchemy import func

        now = datetime.now()
        if period == 'week':
            start_time = now - timedelta(days=7)
        elif period == 'month':
            start_time = now - timedelta(days=30)
        else:
            start_time = now - timedelta(days=1)

        async with session_scope() as session:
            base_query = session.query(AIUsageLog).filter(
                AIUsageLog.timestamp >= start_time
            )

            total_calls = base_query.count()

            route_stats = session.query(
                AIUsageLog.route_type,
                func.count(AIUsageLog.id).label('count')
            ).filter(
                AIUsageLog.timestamp >= start_time
            ).group_by(AIUsageLog.route_type).all()

            route_breakdown = {r.route_type: r.count for r in route_stats}

            ai_stats = session.query(
                func.sum(AIUsageLog.tokens_in).label('total_tokens_in'),
                func.sum(AIUsageLog.tokens_out).label('total_tokens_out'),
                func.sum(AIUsageLog.cost_estimate).label('total_cost')
            ).filter(
                AIUsageLog.timestamp >= start_time,
                AIUsageLog.route_type == 'ai'
            ).first()

            _cache_hits = session.query(func.count(AIUsageLog.id)).filter(
                AIUsageLog.timestamp >= start_time,
                AIUsageLog.cache_hit
            ).scalar() or 0

            ai_calls = route_breakdown.get('ai', 0)
            rule_calls = route_breakdown.get('rule', 0)
            cache_calls = route_breakdown.get('cache', 0)
            reuse_calls = route_breakdown.get('reuse', 0)

            cache_hit_rate = (cache_calls / total_calls * 100) if total_calls > 0 else 0
            rule_route_rate = (rule_calls / total_calls * 100) if total_calls > 0 else 0
            ai_route_rate = (ai_calls / total_calls * 100) if total_calls > 0 else 0
            reuse_rate = (reuse_calls / total_calls * 100) if total_calls > 0 else 0

            avg_ai_cost = (ai_stats.total_cost / ai_calls) if ai_calls > 0 and ai_stats.total_cost else 0.01
            cost_saved = (cache_calls + rule_calls + reuse_calls) * avg_ai_cost

            active_caches = session.query(
                func.count(AnalysisCache.id),
                func.coalesce(func.sum(AnalysisCache.hit_count), 0)
            ).filter(
                AnalysisCache.expires_at > now
            ).first()

            total_cache_entries = active_caches[0] or 0
            total_hits = int(active_caches[1] or 0)
            avg_hits = round(total_hits / total_cache_entries, 1) if total_cache_entries > 0 else 0

            cache_statistics = {
                "total_cache_entries": total_cache_entries,
                "total_hits": total_hits,
                "avg_hits_per_entry": avg_hits,
                "cache_hit_rate": round(cache_hit_rate, 1),
                "saved_calls": cache_calls,
            }

            usage_data = {
                'period': period,
                'period_start': start_time.isoformat(),
                'period_end': now.isoformat(),
                'total_calls': total_calls,
                'route_breakdown': {
                    'ai': ai_calls,
                    'rule': rule_calls,
                    'cache': cache_calls,
                    'reuse': reuse_calls
                },
                'percentages': {
                    'ai': round(ai_route_rate, 1),
                    'rule': round(rule_route_rate, 1),
                    'cache': round(cache_hit_rate, 1),
                    'reuse': round(reuse_rate, 1)
                },
                'tokens': {
                    'input': ai_stats.total_tokens_in or 0,
                    'output': ai_stats.total_tokens_out or 0,
                    'total': (ai_stats.total_tokens_in or 0) + (ai_stats.total_tokens_out or 0)
                },
                'cost': {
                    'total': round(ai_stats.total_cost or 0, 4),
                    'saved_estimate': round(cost_saved, 4)
                },
                'efficiency': {
                    'cache_hit_rate': round(cache_hit_rate, 1),
                    'rule_route_rate': round(rule_route_rate, 1),
                    'reuse_rate': round(reuse_rate, 1),
                    'ai_calls_avoided': cache_calls + rule_calls + reuse_calls
                },
                'cache_statistics': cache_statistics
            }

            try:
                redis = get_redis()
                await redis.setex(cache_key, 70, json.dumps(usage_data, ensure_ascii=False))
            except Exception as e:
                logger.debug(f"AI usage 缓存写入失败: {e}")

            return _ok(usage_data, 200)

    except Exception as e:
        logger.error(f"获取 AI 使用统计失败: {e!s}", exc_info=True)
        return _fail(str(e), 500)
