"""Periodic alert-health digest (cost + noise report).

Reads already-collected telemetry (AIUsageLog + webhook_events), aggregates it
into an alert-health summary, and pushes a single Feishu card. This is read-only
over existing data — no new instruments, no hot-path impact. Each cadence
(daily / weekly / monthly) is independent and off by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pycron
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.app_context import get_config_manager
from core.datetime_utils import utcnow
from core.logger import get_logger
from db.session import session_scope
from models import AIUsageLog, WebhookEvent

logger = get_logger("periodic_report")

# Must match the cron_offset on the scheduled report tasks (services/operations/
# tasks.py): TaskIQ matches cron in this zone, so catch-up must evaluate the same
# zone to compute the same fire times.
_REPORT_CRON_TZ = ZoneInfo("Asia/Shanghai")

_TOP_SOURCES = 5
_TOP_RULES = 5


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    """One report cadence and the config keys that drive it."""

    key: str  # "daily" | "weekly" | "monthly"
    title: str  # Feishu card header
    enabled_attr: str
    cron_attr: str
    window_attr: str
    webhook_attr: str
    # How far back catch-up scans for a missed fire (minutes). Bounds the worst
    # case at startup; comfortably covers each cadence's natural gap.
    catchup_lookback_minutes: int


REPORT_PERIODS: dict[str, ReportPeriod] = {
    "daily": ReportPeriod(
        key="daily",
        title="📊 WebhookWise Alert Health Daily Report",
        enabled_attr="DAILY_REPORT_ENABLED",
        cron_attr="DAILY_REPORT_CRON",
        window_attr="DAILY_REPORT_WINDOW_DAYS",
        webhook_attr="DAILY_REPORT_FEISHU_WEBHOOK",
        catchup_lookback_minutes=24 * 60 + 60,  # ~1 day + slack
    ),
    "weekly": ReportPeriod(
        key="weekly",
        title="📊 WebhookWise Alert Health Weekly Report",
        enabled_attr="WEEKLY_REPORT_ENABLED",
        cron_attr="WEEKLY_REPORT_CRON",
        window_attr="WEEKLY_REPORT_WINDOW_DAYS",
        webhook_attr="WEEKLY_REPORT_FEISHU_WEBHOOK",
        catchup_lookback_minutes=7 * 24 * 60 + 60,  # ~1 week + slack
    ),
    "monthly": ReportPeriod(
        key="monthly",
        title="📊 WebhookWise Alert Health Monthly Report",
        enabled_attr="MONTHLY_REPORT_ENABLED",
        cron_attr="MONTHLY_REPORT_CRON",
        window_attr="MONTHLY_REPORT_WINDOW_DAYS",
        webhook_attr="MONTHLY_REPORT_FEISHU_WEBHOOK",
        catchup_lookback_minutes=31 * 24 * 60 + 60,  # ~1 month + slack
    ),
}


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

    # "source" is just the adapter (e.g. volcengine) — too coarse. Break the
    # noisiest source down by its alert rule name so the report says WHAT is
    # actually noisy. .astext is portable (-> ->> on Postgres, json_extract on
    # SQLite). RuleName is the volcengine/grafana rule; fall back to Type.
    top_rules: list[dict[str, Any]] = []
    if top_sources:
        noisiest = top_sources[0]["source"]
        rule_expr = func.coalesce(
            WebhookEvent.parsed_data["RuleName"].astext,
            WebhookEvent.parsed_data["AlertName"].astext,
            WebhookEvent.parsed_data["MetricName"].astext,
            WebhookEvent.parsed_data["Type"].astext,
        )
        rule_rows = (await session.execute(
            select(rule_expr, func.count(WebhookEvent.id))
            .where(WebhookEvent.timestamp >= start, WebhookEvent.source == noisiest)
            .group_by(rule_expr)
            .order_by(func.count(WebhookEvent.id).desc())
            .limit(_TOP_RULES)
        )).all()
        top_rules = [
            {"source": noisiest, "rule": r[0] or "unknown", "count": int(r[1])} for r in rule_rows
        ]

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
        "top_rules": top_rules,
        "ai_cost_usd": round(ai_cost, 4),
        "ai_calls": ai_calls,
        "cache_hit_pct": cache_pct,
    }


def _build_summary(stats: dict[str, Any]) -> str:
    """Deterministic plain-text summary — the numbers ARE the value, so v1 needs
    no LLM call (keeps the report a pure read over existing data, always works
    even if AI is degraded). An LLM narrative could be layered on later."""
    top = stats["top_sources"][0] if stats["top_sources"] else None
    top_txt = f"{top['source']} ({top['count']} alerts)" if top else "none"
    imp = stats["importance_breakdown"]
    imp_txt = ", ".join(f"{k}:{v}" for k, v in sorted(imp.items(), key=lambda kv: -kv[1])) or "none"
    lines = [
        f"Past {stats['window_days']} days: {stats['total_events']} alerts, "
        f"of which {stats['duplicate_events']} were deduplicated/duplicates (noise rate {stats['noise_pct']}%).",
        f"Importance breakdown: {imp_txt}.",
        f"Noisiest source: {top_txt}.",
    ]
    # Break the noisiest source down by rule so the report says WHAT is noisy.
    rules = stats.get("top_rules") or []
    if rules:
        rule_lines = "\n".join(f"  · {r['rule']}: {r['count']} alerts" for r in rules)
        lines.append(f"Where {rules[0]['source']} broken down by alert rule (Top {len(rules)}):\n{rule_lines}")
    lines.append(
        f"AI: {stats['ai_calls']} calls, cache hit rate {stats['cache_hit_pct']}%, cost ${stats['ai_cost_usd']}."
    )
    return "\n".join(lines)


def _build_card(stats: dict[str, Any], title: str = "📊 WebhookWise Alert Health Weekly Report") -> dict[str, Any]:
    body = _build_summary(stats)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "markdown", "content": body}],
        },
    }


async def _record_report_sent(period_key: str, fire_ts: datetime) -> None:
    """Persist the fire-time this cadence was last sent for (catch-up idempotency).

    Best-effort: a Redis miss only risks a duplicate catch-up send, never a lost
    report, so failures are swallowed (the report itself already went out).
    """
    from contextlib import suppress

    from redis.exceptions import RedisError

    from core.redis_client import redis_setex_str
    from core.redis_health import periodic_report_last_sent

    # Keep the marker well beyond the longest cadence gap so it never expires
    # between two legitimate fires.
    ttl_seconds = 45 * 24 * 3600
    with suppress(RedisError, RuntimeError, TypeError, ValueError):
        await redis_setex_str(periodic_report_last_sent(period_key), ttl_seconds, fire_ts.isoformat())


async def _last_sent_fire(period_key: str) -> datetime | None:
    from contextlib import suppress

    from redis.exceptions import RedisError

    from core.redis_client import redis_get_str
    from core.redis_health import periodic_report_last_sent

    with suppress(RedisError, RuntimeError, TypeError, ValueError):
        raw = await redis_get_str(periodic_report_last_sent(period_key))
        if raw:
            return datetime.fromisoformat(raw)
    return None


def _most_recent_fire(cron: str, now: datetime, lookback_minutes: int) -> datetime | None:
    """Most recent minute at/before ``now`` (within lookback) that ``cron`` matched.

    Returns None if the cron never matched in the window. Scans minute-by-minute
    backwards from now and short-circuits on the first match; the lookback bound
    keeps the worst case cheap even for the monthly cadence.
    """
    # Evaluate the cron in the scheduler's offset zone (Asia/Shanghai) so the
    # computed fire times match what TaskIQ actually scheduled; return in UTC so
    # all fire timestamps (and the Redis last-sent marker) share one basis.
    cursor = now.astimezone(_REPORT_CRON_TZ).replace(second=0, microsecond=0)
    for _ in range(max(1, lookback_minutes) + 1):
        try:
            if pycron.is_now(cron, cursor):
                return cursor.astimezone(UTC)
        except ValueError:
            logger.warning("[PeriodicReport] invalid cron %r; skipping catch-up", cron)
            return None
        cursor -= timedelta(minutes=1)
    return None


async def generate_and_send_report(period_key: str, *, fire_ts: datetime | None = None) -> dict[str, Any]:
    """Generate and send one cadence's report. Returns the stats (for tests/logs).

    Each cadence has its own enable flag, window, and (optional) dedicated
    webhook; the webhook falls back to the weekly webhook, then the deep-analysis
    webhook. Returns a ``{"skipped": ...}`` marker when not enabled / unconfigured.
    ``fire_ts`` records which scheduled occurrence this send satisfies, for
    catch-up idempotency; defaults to now (a regular on-time scheduled run).
    """
    period = REPORT_PERIODS[period_key]
    notif = get_config_manager().notifications

    if not getattr(notif, period.enabled_attr):
        logger.debug("[PeriodicReport] %s not enabled, skipping", period.key)
        return {"skipped": "disabled"}

    webhook_url = (
        getattr(notif, period.webhook_attr)
        or notif.WEEKLY_REPORT_FEISHU_WEBHOOK
        or notif.DEEP_ANALYSIS_FEISHU_WEBHOOK
    )
    if not webhook_url:
        logger.warning(
            "[PeriodicReport] %s enabled but no webhook configured (%s / WEEKLY_REPORT_FEISHU_WEBHOOK / DEEP_ANALYSIS_FEISHU_WEBHOOK)",
            period.key,
            period.webhook_attr,
        )
        return {"skipped": "no_webhook"}

    async with session_scope() as session:
        stats = await collect_report_stats(session, getattr(notif, period.window_attr))

    card = _build_card(stats, period.title)

    from services.notifications.feishu import send_to_feishu

    result = await send_to_feishu(webhook_url, card)
    # Record a tz-aware UTC marker (matches _most_recent_fire's basis so catch-up
    # comparisons never mix naive/aware datetimes).
    await _record_report_sent(period_key, fire_ts or utcnow().replace(tzinfo=UTC))
    logger.info(
        "[PeriodicReport] %s sent events=%s noise=%s%% ai_cost=$%s status=%s",
        period.key,
        stats["total_events"],
        stats["noise_pct"],
        stats["ai_cost_usd"],
        result.get("status"),
    )
    return stats


async def run_report_catchup() -> dict[str, str]:
    """Send any enabled cadence whose most recent scheduled fire was missed.

    Called on worker startup. For each enabled cadence it finds the most recent
    fire time at/before now; if no report has been sent for that occurrence (the
    scheduler was down at that minute), it sends one catch-up now. Idempotent via
    the Redis last-sent marker, so repeated restarts don't re-send.
    """
    # tz-aware UTC so _most_recent_fire can convert into the cron offset zone.
    now = utcnow().replace(tzinfo=UTC)
    notif = get_config_manager().notifications
    outcomes: dict[str, str] = {}
    for period_key, period in REPORT_PERIODS.items():
        if not getattr(notif, period.enabled_attr):
            continue
        cron = str(getattr(notif, period.cron_attr))
        fire = _most_recent_fire(cron, now, period.catchup_lookback_minutes)
        if fire is None:
            outcomes[period_key] = "no_recent_fire"
            continue
        last_sent = await _last_sent_fire(period_key)
        if last_sent is not None and last_sent >= fire:
            outcomes[period_key] = "already_sent"
            continue
        # Single-flight across worker replicas: only the process that wins the
        # NX claim for this exact (period, fire) sends, so concurrent worker
        # startups don't each fire a duplicate catch-up.
        if not await _claim_catchup(period_key, fire):
            outcomes[period_key] = "claimed_elsewhere"
            continue
        logger.info(
            "[PeriodicReport] catch-up: %s missed its %s fire; sending now", period.key, fire.isoformat()
        )
        result = await generate_and_send_report(period_key, fire_ts=fire)
        outcomes[period_key] = "skipped" if "skipped" in result else "sent"
    return outcomes


async def _claim_catchup(period_key: str, fire: datetime) -> bool:
    """Atomically claim one catch-up occurrence. True if this process won it.

    On Redis error, return False (skip) — a missed catch-up self-heals on the
    next restart, whereas sending would risk the duplicate this guards against.
    """
    from contextlib import suppress

    from redis.exceptions import RedisError

    from core.redis_client import redis_set_nx_ex
    from core.redis_health import periodic_report_catchup_claim

    with suppress(RedisError, RuntimeError, TypeError, ValueError):
        # TTL only needs to outlive concurrent startups; an hour is ample.
        return await redis_set_nx_ex(periodic_report_catchup_claim(period_key, fire.isoformat()), "1", 3600)
    return False


async def generate_and_send_daily_report() -> dict[str, Any]:
    return await generate_and_send_report("daily")


async def generate_and_send_weekly_report() -> dict[str, Any]:
    return await generate_and_send_report("weekly")


async def generate_and_send_monthly_report() -> dict[str, Any]:
    return await generate_and_send_report("monthly")
