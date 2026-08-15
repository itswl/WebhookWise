"""The monthly AI budget as a brake, not only as an alarm.

``check_ai_cost_budget`` already watches month-to-date spend and posts a card at
80% and again at 100%. A card is a message to a person who may be asleep; it
does not stop the spending it is describing. This is the other half: the
analysis path asks, before paying for a model call, whether the month's budget
is already gone, and degrades to the rule route if it is.

Two things it is deliberately not:

* **Not on by default.** ``AI_COST_BUDGET_ENFORCE`` starts false, so an existing
  deployment keeps behaving exactly as it does today until someone decides that
  a quiet month matters more than an analysed alert.
* **Not a silent skip.** A budget refusal goes through the same degradation path
  as a provider outage and says so in ``_degraded_reason``. Tiered routing is an
  intentional route and is not marked degraded; running out of money is not the
  same thing, and an operator reading the alert must be able to tell which of
  the two happened.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.exceptions import RedisError
from sqlalchemy import func, select

from core.app_context import get_config_manager
from core.logger import get_logger
from db.session import session_scope
from models.analysis import AIUsageLog
from services.operations import runtime_settings

logger = get_logger("analysis.ai_budget")

# Month-to-date spend is a SUM over a table that only grows; asking per alert
# would put a scan in front of every analysis. The window is short enough that
# an overrun is bounded by one interval's worth of calls, not by the month.
_CACHE_KEY = "ai:budget:spent"
_CACHE_TTL_SECONDS = 60


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def month_to_date_spend() -> float:
    """This calendar month's AI spend, cached briefly."""
    try:
        from core.redis_client import redis_get_str

        cached = await redis_get_str(_CACHE_KEY)
        if cached is not None:
            return float(cached)
    except (RedisError, ValueError, TypeError):
        cached = None  # Redis is an accelerator here, never the source of truth

    async with session_scope() as session:
        spend = await session.scalar(
            select(func.coalesce(func.sum(AIUsageLog.cost_estimate), 0.0)).where(
                AIUsageLog.timestamp >= _month_start(datetime.now(UTC))
            )
        )
    spent = float(spend or 0.0)
    try:
        from core.redis_client import redis_setex_str

        await redis_setex_str(_CACHE_KEY, _CACHE_TTL_SECONDS, str(spent))
    except RedisError:
        pass
    return spent


async def budget_exhausted() -> tuple[bool, float, float]:
    """(refuse this call, month-to-date spend, budget).

    False whenever enforcement is off or no budget is set, so the default
    deployment never reaches the database for this.
    """
    notif = get_config_manager().notifications
    budget = runtime_settings.override_or(
        "AI_COST_MONTHLY_BUDGET_USD", float(getattr(notif, "AI_COST_MONTHLY_BUDGET_USD", 0.0) or 0.0)
    )
    enforce = runtime_settings.override_or(
        "AI_COST_BUDGET_ENFORCE", bool(getattr(notif, "AI_COST_BUDGET_ENFORCE", False))
    )
    if budget <= 0 or not enforce:
        return False, 0.0, budget

    try:
        spent = await month_to_date_spend()
    except Exception:  # noqa: BLE001 — a broken meter must not block analysis
        logger.warning("[AIBudget] could not read month-to-date spend; letting the call through", exc_info=True)
        return False, 0.0, budget
    return spent >= budget, spent, budget
