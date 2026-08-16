"""Where a SYSTEM notification goes: a forward rule if one claims it, else config.

An alert's destination has always been a rule — a table, a priority order, a
match vocabulary, a test button. A system notification's destination was seven
environment variables chained by `or`:

    incident created  <- DEEP_ANALYSIS_FEISHU_WEBHOOK or WEEKLY_REPORT_...
    SLA breached      <- SLA_BREACH_... or DEEP_ANALYSIS_... or WEEKLY_REPORT_...
    daily report      <- DAILY_REPORT_... or WEEKLY_REPORT_... or DEEP_ANALYSIS_...
    AI cost budget    <- AI_COST_BUDGET_... or DAILY_REPORT_... or ...

That cascade is not hypothetical tidiness: incident notifications had never
been given an address of their own, so they fell all the way through to
DEEP_ANALYSIS_FEISHU_WEBHOOK — whose bot token had been revoked. Twenty-six
incident notifications went nowhere over six days, and nothing said so, because
a URL in a file has no delivery record to look at.

These events already travel with an `event_type` (`incident_created`,
`sla_breached`, `outbox_exhausted`, `deep_analysis`), and forward rules already
match on it — rule "失败通知" is written against `ai_error,ai_degraded,
outbox_exhausted`. The only missing step was asking the rules.

**This is the compatible half.** A rule wins when one matches; otherwise the
configured address is used exactly as before, so nothing moves until an
operator writes a rule. Removing the cascade is a separate decision that needs
the rules to be in place first.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.logger import get_logger

logger = get_logger("notifications.routing")

# Targets that can receive a system card. A rule pointing somewhere else — a
# deep-analysis gateway, a raw webhook — is about alerts, not about this.
_NOTIFICATION_TARGETS = ("feishu", "feishu_app", "feishu_relay")


@dataclass(frozen=True, slots=True)
class NotificationTarget:
    """Where to send, and who decided — the second half is what was missing."""

    url: str
    target_type: str
    rule_id: int | None
    rule_name: str

    @property
    def from_rule(self) -> bool:
        return self.rule_id is not None


async def resolve_notification_target(
    event_type: str,
    *,
    fallback_url: str,
    fallback_name: str,
    fallback_target_type: str = "feishu",
) -> NotificationTarget:
    """The first enabled rule claiming this event type, else the configured address.

    Fails open to the fallback: a rules lookup that cannot run must not silence
    a notification, which is the failure this whole area is trying to end.
    """
    try:
        from services.forwarding.rules import get_cached_forward_rules
        from services.webhooks.decisioning import select_forward_rules

        rules = await get_cached_forward_rules()
        matched = select_forward_rules(rules, event_type=event_type)
    except Exception:  # noqa: BLE001 - a routing lookup must never lose the card
        logger.warning("[Notifications] rule lookup failed for %s; using configured address", event_type, exc_info=True)
        matched = []

    for rule in matched:
        target_type = str(rule.target_type or "")
        url = str(rule.target_url or "")
        if target_type not in _NOTIFICATION_TARGETS:
            continue
        if target_type != "feishu_app" and not url:
            continue
        return NotificationTarget(
            url=url,
            target_type=target_type,
            rule_id=rule.id,
            rule_name=str(rule.name or "") or f"rule:{rule.id}",
        )

    return NotificationTarget(
        url=fallback_url,
        target_type=fallback_target_type,
        rule_id=None,
        rule_name=fallback_name,
    )
