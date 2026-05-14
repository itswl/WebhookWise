"""AI usage audit logging."""

from core.logger import logger
from db.session import session_scope
from models import AIUsageLog
from services.analysis.ai_policies import AIProviderPolicy


async def log_ai_usage(
    route_type: str,
    alert_hash: str,
    source: str,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cache_hit: bool = False,
    policy: AIProviderPolicy | None = None,
) -> None:
    try:
        policy = policy or AIProviderPolicy.from_config()
        cost = 0.0
        if route_type == "ai" and tokens_in > 0:
            cost = policy.cost_for_tokens(tokens_in, tokens_out)
        async with session_scope() as session:
            session.add(
                AIUsageLog(
                    model=model or policy.model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_estimate=cost,
                    cache_hit=cache_hit,
                    route_type=route_type,
                    alert_hash=alert_hash,
                    source=source,
                )
            )
    except Exception as e:
        logger.warning("记录 AI 使用日志失败: %s", e)
