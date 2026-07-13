"""Periodic alert-health digest (cost + noise report).

Reads already-collected telemetry (AIUsageLog + webhook_events), aggregates it
into an alert-health summary, and pushes a single Feishu card. This is read-only
over existing data — no new instruments, no hot-path impact. Each cadence
(daily / weekly / monthly) is independent and off by default.
"""

from __future__ import annotations

import asyncio
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
from models import AIUsageLog, AnalysisFeedback, DecisionTrace, ForwardOutbox, Incident, WebhookEvent
from services.webhooks.types import ForwardOutboxStatus, ForwardResult

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
    previous_start = start - timedelta(days=window_days)

    total_events = int(
        await session.scalar(select(func.count(WebhookEvent.id)).where(WebhookEvent.timestamp >= start)) or 0
    )
    duplicate_events = int(
        await session.scalar(
            select(func.count(WebhookEvent.id)).where(
                WebhookEvent.timestamp >= start, WebhookEvent.is_duplicate.is_(True)
            )
        )
        or 0
    )
    previous_total = int(
        await session.scalar(
            select(func.count(WebhookEvent.id)).where(
                WebhookEvent.timestamp >= previous_start,
                WebhookEvent.timestamp < start,
            )
        )
        or 0
    )
    previous_duplicates = int(
        await session.scalar(
            select(func.count(WebhookEvent.id)).where(
                WebhookEvent.timestamp >= previous_start,
                WebhookEvent.timestamp < start,
                WebhookEvent.is_duplicate.is_(True),
            )
        )
        or 0
    )

    importance_rows = (
        await session.execute(
            select(WebhookEvent.importance, func.count(WebhookEvent.id))
            .where(WebhookEvent.timestamp >= start)
            .group_by(WebhookEvent.importance)
        )
    ).all()
    importance_breakdown = {(r[0] or "unknown"): int(r[1]) for r in importance_rows}

    source_rows = (
        await session.execute(
            select(WebhookEvent.source, func.count(WebhookEvent.id))
            .where(WebhookEvent.timestamp >= start)
            .group_by(WebhookEvent.source)
            .order_by(func.count(WebhookEvent.id).desc())
            .limit(_TOP_SOURCES)
        )
    ).all()
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
        rule_rows = (
            await session.execute(
                select(rule_expr, func.count(WebhookEvent.id))
                .where(WebhookEvent.timestamp >= start, WebhookEvent.source == noisiest)
                .group_by(rule_expr)
                .order_by(func.count(WebhookEvent.id).desc())
                .limit(_TOP_RULES)
            )
        ).all()
        top_rules = [{"source": noisiest, "rule": r[0] or "unknown", "count": int(r[1])} for r in rule_rows]

    cost_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(AIUsageLog.cost_estimate), 0.0),
                func.coalesce(func.sum(AIUsageLog.tokens_in), 0),
                func.coalesce(func.sum(AIUsageLog.tokens_out), 0),
                func.count(AIUsageLog.id),
            ).where(AIUsageLog.timestamp >= start)
        )
    ).first()
    ai_cost = float(cost_row[0]) if cost_row else 0.0
    ai_calls = int(cost_row[3]) if cost_row else 0

    cache_hits = int(
        await session.scalar(
            select(func.count(AIUsageLog.id)).where(AIUsageLog.timestamp >= start, AIUsageLog.cache_hit.is_(True))
        )
        or 0
    )

    noise_pct = round(100.0 * duplicate_events / total_events, 1) if total_events else 0.0
    cache_pct = round(100.0 * cache_hits / ai_calls, 1) if ai_calls else 0.0

    # Incident stats for the same window.
    incident_total = int(await session.scalar(select(func.count(Incident.id)).where(Incident.started_at >= start)) or 0)
    incident_active = int(
        await session.scalar(
            select(func.count(Incident.id)).where(Incident.started_at >= start, Incident.status == "active")
        )
        or 0
    )
    incident_quiet = int(
        await session.scalar(
            select(func.count(Incident.id)).where(Incident.started_at >= start, Incident.status == "quiet")
        )
        or 0
    )

    unresolved_incidents = int(
        await session.scalar(
            select(func.count(Incident.id)).where(Incident.workflow_status.notin_(["resolved", "ignored"]))
        )
        or 0
    )
    sla_breaches = int(
        await session.scalar(
            select(func.count(Incident.id)).where(
                Incident.sla_due_at.isnot(None),
                Incident.sla_due_at <= utcnow(),
                Incident.workflow_status.notin_(["resolved", "ignored"]),
            )
        )
        or 0
    )
    sla_breaches += int(
        await session.scalar(
            select(func.count(WebhookEvent.id)).where(
                WebhookEvent.sla_due_at.isnot(None),
                WebhookEvent.sla_due_at <= utcnow(),
                WebhookEvent.workflow_status.notin_(["resolved", "ignored"]),
            )
        )
        or 0
    )

    delivery_rows = (
        await session.execute(
            select(ForwardOutbox.status, func.count(ForwardOutbox.id))
            .where(ForwardOutbox.created_at >= start)
            .group_by(ForwardOutbox.status)
        )
    ).all()
    delivery_breakdown = {str(status): int(count) for status, count in delivery_rows}
    delivery_terminal = sum(
        delivery_breakdown.get(status, 0)
        for status in (
            ForwardOutboxStatus.SENT,
            ForwardOutboxStatus.EXHAUSTED,
            ForwardOutboxStatus.EXPIRED,
        )
    )
    delivery_success_rate = (
        round(100.0 * delivery_breakdown.get(ForwardOutboxStatus.SENT, 0) / delivery_terminal, 1)
        if delivery_terminal
        else None
    )
    ai_degraded = int(
        await session.scalar(
            select(func.count(DecisionTrace.id)).where(
                DecisionTrace.created_at >= start,
                DecisionTrace.degraded_reason.isnot(None),
            )
        )
        or 0
    )

    feedback_rows = (
        await session.execute(
            select(AnalysisFeedback.verdict, func.count(AnalysisFeedback.id))
            .where(AnalysisFeedback.created_at >= start)
            .group_by(AnalysisFeedback.verdict)
        )
    ).all()
    feedback_breakdown = {str(verdict): int(count) for verdict, count in feedback_rows}
    feedback_total = sum(feedback_breakdown.values())
    feedback_correct = feedback_breakdown.get("correct", 0)

    unhealthy_rule_rows = (
        await session.execute(
            select(ForwardOutbox.rule_name, func.count(ForwardOutbox.id))
            .where(
                ForwardOutbox.updated_at >= start,
                ForwardOutbox.status == ForwardOutboxStatus.EXHAUSTED,
            )
            .group_by(ForwardOutbox.rule_name)
            .order_by(func.count(ForwardOutbox.id).desc())
            .limit(_TOP_RULES)
        )
    ).all()
    unhealthy_rules = [
        {"rule": str(rule_name or "unknown"), "failures": int(count)}
        for rule_name, count in unhealthy_rule_rows
    ]

    from services.operations.action_center import get_action_center

    action_center = await get_action_center(session)
    previous_noise_pct = round(100.0 * previous_duplicates / previous_total, 1) if previous_total else 0.0
    volume_change_pct = (
        round(100.0 * (total_events - previous_total) / previous_total, 1) if previous_total else None
    )

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
        "incident_total": incident_total,
        "incident_active": incident_active,
        "incident_quiet": incident_quiet,
        "previous_total_events": previous_total,
        "previous_noise_pct": previous_noise_pct,
        "volume_change_pct": volume_change_pct,
        "noise_change_pp": round(noise_pct - previous_noise_pct, 1),
        "delivery_success_rate": delivery_success_rate,
        "delivery_breakdown": delivery_breakdown,
        "ai_degraded": ai_degraded,
        "unresolved_incidents": unresolved_incidents,
        "sla_breaches": sla_breaches,
        "action_center_total": int(action_center["summary"]["total"]),
        "feedback_total": feedback_total,
        "feedback_agreement_pct": round(100.0 * feedback_correct / feedback_total, 1) if feedback_total else None,
        "unhealthy_rules": unhealthy_rules,
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
    if stats.get("volume_change_pct") is not None:
        lines.append(
            f"Compared with the previous window: alert volume {stats['volume_change_pct']:+.1f}%, "
            f"noise rate {stats.get('noise_change_pp', 0):+.1f} percentage points."
        )
    if stats.get("delivery_success_rate") is not None:
        lines.append(f"Delivery success rate: {stats['delivery_success_rate']}% of terminal deliveries.")
    if stats.get("ai_degraded"):
        lines.append(f"AI degraded to fallback analysis for {stats['ai_degraded']} alert(s).")
    if stats.get("incident_total"):
        lines.append(
            f"Incidents: {stats['incident_total']} total ({stats['incident_active']} active / {stats['incident_quiet']} quiet)."
        )
    if stats.get("unresolved_incidents") or stats.get("sla_breaches") or stats.get("action_center_total"):
        lines.append(
            f"Operator queue: {stats.get('unresolved_incidents', 0)} unresolved incidents, "
            f"{stats.get('sla_breaches', 0)} SLA breaches, "
            f"{stats.get('action_center_total', 0)} Action Center items."
        )
    if stats.get("feedback_total"):
        lines.append(
            f"Human feedback: {stats['feedback_total']} review(s), "
            f"{stats.get('feedback_agreement_pct')}% agreed with the analysis."
        )
    unhealthy_rules = stats.get("unhealthy_rules") or []
    if unhealthy_rules:
        lines.append(
            "Unhealthy delivery rules:\n"
            + "\n".join(f"  · {row['rule']}: {row['failures']} exhausted" for row in unhealthy_rules)
        )
    return "\n".join(lines)


def _build_card(
    stats: dict[str, Any],
    title: str = "📊 WebhookWise Alert Health Weekly Report",
    dashboard_url: str = "",
) -> dict[str, Any]:
    body = _build_summary(stats)
    if dashboard_url:
        body += f"\n\n[Open WebhookWise dashboard]({dashboard_url})"
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
            parsed = datetime.fromisoformat(raw)
            # Coerce to tz-aware UTC. A marker written by older code (or any
            # non-offset isoformat) is naive; comparing it against the tz-aware
            # `fire` in run_report_catchup would raise TypeError and abort the
            # whole catch-up. Treat naive markers as UTC.
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
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


_REPORT_SEND_MAX_ATTEMPTS = 4
_REPORT_SEND_BACKOFF_SECONDS = (2.0, 5.0, 15.0)


async def _send_report_with_retry(webhook_url: str, card: dict[str, Any], period_label: str) -> ForwardResult:
    """Send the report card, retrying transient/non-success results with backoff.

    Unlike alert forwards (which retry via the outbox), a periodic report is a
    one-shot send — so a transient Feishu error (notably code 11232 "frequency
    limited", which collides with the 09:00 alert burst on the shared bot) would
    permanently drop the report. Retry a few times with backoff so a brief rate
    limit no longer loses the report. "invalid_target" is not retried (config
    error, not transient).
    """
    from services.notifications.feishu import send_to_feishu

    result: ForwardResult = {"status": "failed", "message": "not attempted"}
    for attempt in range(1, _REPORT_SEND_MAX_ATTEMPTS + 1):
        result = await send_to_feishu(webhook_url, card)
        status = result.get("status")
        if status == "success":
            if attempt > 1:
                logger.info("[PeriodicReport] %s send succeeded on attempt %d", period_label, attempt)
            return result
        if status == "invalid_target":
            # Misconfigured URL — retrying cannot help.
            return result
        if attempt < _REPORT_SEND_MAX_ATTEMPTS:
            delay = _REPORT_SEND_BACKOFF_SECONDS[min(attempt - 1, len(_REPORT_SEND_BACKOFF_SECONDS) - 1)]
            logger.warning(
                "[PeriodicReport] %s send failed (attempt %d/%d, status=%s msg=%s); retrying in %.0fs",
                period_label,
                attempt,
                _REPORT_SEND_MAX_ATTEMPTS,
                status,
                result.get("message"),
                delay,
            )
            await asyncio.sleep(delay)
    logger.error(
        "[PeriodicReport] %s send failed after %d attempts; giving up (last status=%s msg=%s)",
        period_label,
        _REPORT_SEND_MAX_ATTEMPTS,
        result.get("status"),
        result.get("message"),
    )
    return result


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
        getattr(notif, period.webhook_attr) or notif.WEEKLY_REPORT_FEISHU_WEBHOOK or notif.DEEP_ANALYSIS_FEISHU_WEBHOOK
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

    card = _build_card(stats, period.title, str(notif.DASHBOARD_PUBLIC_URL or "").strip())

    result = await _send_report_with_retry(webhook_url, card, period.key)
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


def _month_start(now: datetime) -> datetime:
    """First instant of the current calendar month (UTC), for month-to-date spend."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _claim_budget_tier(month_key: str, tier: str) -> bool:
    """Single-flight claim for one budget tier this month. True if this process
    won it. Redis error → False (skip): a missed budget alert self-heals on the
    next daily run, whereas double-sending is the annoyance we guard against."""
    from contextlib import suppress

    from redis.exceptions import RedisError

    from core.redis_client import redis_set_nx_ex
    from core.redis_health import ai_cost_budget_alert_claim

    with suppress(RedisError, RuntimeError, TypeError, ValueError):
        return await redis_set_nx_ex(ai_cost_budget_alert_claim(month_key, tier), utcnow().isoformat(), 40 * 24 * 3600)
    return False


async def check_ai_cost_budget() -> dict[str, Any]:
    """Alert on month-to-date AI spend, two tiers, once each per month.

    Reuses the already-collected ``AIUsageLog.cost_estimate`` (the same data the
    AI Cost dashboard shows) — sums the current calendar month and compares to
    ``AI_COST_MONTHLY_BUDGET_USD``:
    - **critical**: spend >= 100% of budget (over-budget);
    - **warning**: spend >= budget * ``AI_COST_BUDGET_ALERT_THRESHOLD`` (default 0.8).

    Each tier fires at most once per month via its own Redis NX claim, so a
    warning at 80% and a later critical once spend actually exceeds the budget
    are *separate* alerts — but neither nags daily (the daily task calls this
    daily). Returns a marker dict for tests/logs.

    Disabled (budget <= 0) or unconfigured webhook → ``{"skipped": ...}`` no-op.
    """
    notif = get_config_manager().notifications
    budget = float(notif.AI_COST_MONTHLY_BUDGET_USD or 0.0)
    if budget <= 0:
        return {"skipped": "disabled"}

    webhook_url = (
        notif.AI_COST_BUDGET_FEISHU_WEBHOOK
        or notif.DAILY_REPORT_FEISHU_WEBHOOK
        or notif.WEEKLY_REPORT_FEISHU_WEBHOOK
        or notif.DEEP_ANALYSIS_FEISHU_WEBHOOK
    )
    if not webhook_url:
        logger.warning("[AICostBudget] budget set but no webhook configured; skipping")
        return {"skipped": "no_webhook"}

    now = utcnow()
    month_start = _month_start(now)
    async with session_scope() as session:
        spend = await session.scalar(
            select(func.coalesce(func.sum(AIUsageLog.cost_estimate), 0.0)).where(AIUsageLog.timestamp >= month_start)
        )
    spent = float(spend or 0.0)
    warn_threshold = budget * float(notif.AI_COST_BUDGET_ALERT_THRESHOLD or 0.8)

    # The highest tier currently crossed (critical outranks warning).
    if spent >= budget:
        tier = "critical"
    elif spent >= warn_threshold:
        tier = "warning"
    else:
        return {"skipped": "under_threshold", "spent": spent, "budget": budget}

    if not await _claim_budget_tier(now.strftime("%Y-%m"), tier):
        return {"skipped": "already_alerted", "tier": tier, "spent": spent, "budget": budget}

    month_key = now.strftime("%Y-%m")
    pct = round(spent / budget * 100, 1) if budget else 0.0
    card = _build_ai_cost_budget_card(spent=spent, budget=budget, pct=pct, month_key=month_key, tier=tier)
    result = await _send_report_with_retry(webhook_url, card, f"ai_cost_budget:{tier}")
    logger.info(
        "[AICostBudget] month=%s tier=%s spent=$%.4f budget=$%.2f (%.1f%%) alert status=%s",
        month_key,
        tier,
        spent,
        budget,
        pct,
        result.get("status"),
    )
    return {"alerted": True, "tier": tier, "spent": spent, "budget": budget, "pct": pct, "status": result.get("status")}


def _build_ai_cost_budget_card(*, spent: float, budget: float, pct: float, month_key: str, tier: str) -> dict[str, Any]:
    over = tier == "critical"
    header = "🔴 WebhookWise AI 成本超预算" if over else "⚠️ WebhookWise AI 成本预算预警"
    body = (
        f"**{month_key} 月度 AI 成本{'已超支' if over else '接近预算'}**\n\n"
        f"- 本月已花费：**${spent:.4f}**\n"
        f"- 月度预算：${budget:.2f}\n"
        f"- 占用比例：**{pct:.1f}%**\n\n"
        f"{'已超出预算，请检查 AI 调用量或调整预算。' if over else '已达预警阈值，注意用量。'}"
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": header}},
            "elements": [{"tag": "markdown", "content": body}],
        },
    }


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
        logger.info("[PeriodicReport] catch-up: %s missed its %s fire; sending now", period.key, fire.isoformat())
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
