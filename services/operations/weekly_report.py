"""Periodic alert-health digest (cost + noise report).

Reads already-collected telemetry (AIUsageLog + webhook_events), aggregates it
into an alert-health summary, optionally adds a one-paragraph LLM narrative, and
pushes a single Feishu card. This is read-only over existing data — no new
instruments, no hot-path impact. Off by default (WEEKLY_REPORT_ENABLED).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import AIUsageLog, WebhookEvent

logger = get_logger("weekly_report")

_TOP_SOURCES = 5


async def collect_report_stats(session: AsyncSession, window_days: int) -> dict[str, Any]:
    """Aggregate alert-health numbers over the trailing window. Pure reads."""
    window_days = max(1, int(window_days))
    start = utcnow() - timedelta(days=window_days)

    total_events = int(await session.scalar(
        select(func.count(WebhookEvent.id)).where(WebhookEvent.timestamp >= start)
    ) or 0)
    duplicate_events = int(await session.scalar(
        select(func.count(WebhookEvent.id)).where(
            WebhookEvent.timestamp >= start, WebhookEvent.is_duplicate.is_(True)
        )
    ) or 0)

    importance_rows = (await session.execute(
        select(WebhookEvent.importance, func.count(WebhookEvent.id))
        .where(WebhookEvent.timestamp >= start)
        .group_by(WebhookEvent.importance)
    )).all()
    importance_breakdown = {(r[0] or "unknown"): int(r[1]) for r in importance_rows}

    source_rows = (await session.execute(
        select(WebhookEvent.source, func.count(WebhookEvent.id))
        .where(WebhookEvent.timestamp >= start)
        .group_by(WebhookEvent.source)
        .order_by(func.count(WebhookEvent.id).desc())
        .limit(_TOP_SOURCES)
    )).all()
    top_sources = [{"source": r[0] or "unknown", "count": int(r[1])} for r in source_rows]

    cost_row = (await session.execute(
        select(
            func.coalesce(func.sum(AIUsageLog.cost_estimate), 0.0),
            func.coalesce(func.sum(AIUsageLog.tokens_in), 0),
            func.coalesce(func.sum(AIUsageLog.tokens_out), 0),
            func.count(AIUsageLog.id),
        ).where(AIUsageLog.timestamp >= start)
    )).first()
    ai_cost = float(cost_row[0]) if cost_row else 0.0
    ai_calls = int(cost_row[3]) if cost_row else 0

    cache_hits = int(await session.scalar(
        select(func.count(AIUsageLog.id)).where(
            AIUsageLog.timestamp >= start, AIUsageLog.cache_hit.is_(True)
        )
    ) or 0)

    noise_pct = round(100.0 * duplicate_events / total_events, 1) if total_events else 0.0
    cache_pct = round(100.0 * cache_hits / ai_calls, 1) if ai_calls else 0.0
    return {
        "window_days": window_days,
        "total_events": total_events,
        "duplicate_events": duplicate_events,
        "noise_pct": noise_pct,
        "importance_breakdown": importance_breakdown,
        "top_sources": top_sources,
        "ai_cost_usd": round(ai_cost, 4),
        "ai_calls": ai_calls,
        "cache_hit_pct": cache_pct,
    }


def _build_summary(stats: dict[str, Any]) -> str:
    """Deterministic plain-text summary — the numbers ARE the value, so v1 needs
    no LLM call (keeps the report a pure read over existing data, always works
    even if AI is degraded). An LLM narrative could be layered on later."""
    top = stats["top_sources"][0] if stats["top_sources"] else None
    top_txt = f"{top['source']}（{top['count']} 条）" if top else "无"
    imp = stats["importance_breakdown"]
    imp_txt = "、".join(f"{k}:{v}" for k, v in sorted(imp.items(), key=lambda kv: -kv[1])) or "无"
    return (
        f"过去 {stats['window_days']} 天：告警 {stats['total_events']} 条，"
        f"其中去重/重复 {stats['duplicate_events']} 条（噪声率 {stats['noise_pct']}%）。\n"
        f"重要度分布：{imp_txt}。\n"
        f"最吵的来源：{top_txt}。\n"
        f"AI：调用 {stats['ai_calls']} 次，缓存命中 {stats['cache_hit_pct']}%，花费 ${stats['ai_cost_usd']}。"
    )


def _build_card(stats: dict[str, Any]) -> dict[str, Any]:
    body = _build_summary(stats)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "📊 WebhookWise 告警健康周报"}},
            "elements": [{"tag": "markdown", "content": body}],
        },
    }


async def generate_and_send_weekly_report() -> dict[str, Any]:
    """Entry point for the scheduled task. Returns the stats (for tests/logs)."""
    cfg = get_config_manager()
    notif = cfg.notifications
    if not notif.WEEKLY_REPORT_ENABLED:
        logger.debug("[WeeklyReport] 未启用，跳过")
        return {"skipped": "disabled"}

    webhook_url = notif.WEEKLY_REPORT_FEISHU_WEBHOOK or notif.DEEP_ANALYSIS_FEISHU_WEBHOOK
    if not webhook_url:
        logger.warning("[WeeklyReport] 已启用但未配置 webhook（WEEKLY_REPORT_FEISHU_WEBHOOK / DEEP_ANALYSIS_FEISHU_WEBHOOK）")
        return {"skipped": "no_webhook"}

    async with session_scope() as session:
        stats = await collect_report_stats(session, notif.WEEKLY_REPORT_WINDOW_DAYS)

    card = _build_card(stats)

    from services.notifications.feishu import send_to_feishu

    result = await send_to_feishu(webhook_url, card)
    logger.info(
        "[WeeklyReport] 已发送 events=%s noise=%s%% ai_cost=$%s status=%s",
        stats["total_events"],
        stats["noise_pct"],
        stats["ai_cost_usd"],
        result.get("status"),
    )
    return stats
