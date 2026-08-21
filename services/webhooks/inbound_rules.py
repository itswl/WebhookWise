"""Reading and writing inbound rules, cached the way forward rules are.

Same shape as `services/forwarding/rules.py` on purpose: a per-worker TTL cache
with cross-worker Pub/Sub invalidation, so an edit takes effect everywhere
within seconds without a restart. The decision this feeds sits in front of a
paid model call, so it must never wait on a database.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.datetime_utils import utcnow
from core.logger import get_logger
from core.pubsub_cache import TtlPubSubCache
from db.session import session_scope
from models import InboundRule
from services.webhooks.decisioning import (
    InboundRuleSnapshot,
    matching_inbound_actions,
    matching_inbound_importance_cap,
)
from services.webhooks.types import (
    INBOUND_ACTIONS,
    INBOUND_ACTIONS_WITH_VALUE,
    SKIP_AI,
)

logger = get_logger("webhooks.inbound_rules")


# Criteria that are only known AFTER an alert has been judged. A skip_ai rule
# runs before that, so one filtering on importance could never match — the kind
# of rule that looks configured and does nothing.
POST_JUDGEMENT_FIELDS: Final = ("match_importance",)

# A cap may only name a severity WebhookWise actually stores.
VALID_IMPORTANCE_CAPS: Final = frozenset({"high", "medium", "low"})

_INVALIDATION_CHANNEL: Final = "webhookwise:inbound_rules:invalidate"


def alert_rule_name(parsed: dict[str, Any]) -> str:
    """The alert rule behind this event, however the sender spells it.

    Lives here rather than in the analyzer because both sides of the decision
    need it — the analysis path and the delivery path — and forwarding must not
    import analysis to ask what an alert is called.
    """
    for key in ("RuleName", "alert_name", "AlertName", "alertname"):
        value = str(parsed.get(key) or "").strip()
        if value:
            return value
    labels = parsed.get("commonLabels")
    if isinstance(labels, dict):
        return str(labels.get("alertname") or "").strip()
    return ""


def snapshot(rule: InboundRule) -> InboundRuleSnapshot:
    return InboundRuleSnapshot(
        id=rule.id,
        name=rule.name or "",
        action=rule.action,
        priority=rule.priority or 0,
        match_event_type=rule.match_event_type or "",
        match_importance=rule.match_importance or "",
        match_duplicate=rule.match_duplicate or "all",
        match_source=rule.match_source or "",
        match_project=rule.match_project or "",
        match_region=rule.match_region or "",
        match_environment=rule.match_environment or "",
        match_payload=rule.match_payload or "",
        action_value=rule.action_value or "",
        match_rule_name=rule.match_rule_name or "",
        comment=rule.comment or "",
    )


async def list_enabled_inbound_rules(session: AsyncSession | None = None) -> list[InboundRuleSnapshot]:
    async def _list(sess: AsyncSession) -> list[InboundRuleSnapshot]:
        stmt = select(InboundRule).filter_by(enabled=True).order_by(InboundRule.priority.desc())
        return [snapshot(rule) for rule in (await sess.execute(stmt)).scalars().all()]

    if session is not None:
        return await _list(session)
    async with session_scope() as sess:
        return await _list(sess)


_cache: TtlPubSubCache[list[InboundRuleSnapshot]] = TtlPubSubCache(
    channel=_INVALIDATION_CHANNEL,
    loader=list_enabled_inbound_rules,
    log_prefix="InboundRules",
)


async def publish_inbound_rules_invalidation() -> None:
    await _cache.publish_invalidation()


async def cached_inbound_rules() -> list[InboundRuleSnapshot]:
    try:
        return await _cache.get()
    except Exception:  # noqa: BLE001 — a rules outage must not stop analysis
        logger.warning("[InboundRules] could not load rules; treating none as matching", exc_info=True)
        return []


async def inbound_actions_for(
    *,
    parsed_data: dict[str, Any] | None,
    source: str = "",
    event_type: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    rule_name: str = "",
) -> set[str]:
    """Which inbound actions this alert triggers. Fails open (an empty set)."""
    rules = await cached_inbound_rules()
    if not rules:
        return set()
    return matching_inbound_actions(
        rules,
        event_type=event_type,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
        rule_name=rule_name,
    )


def validate(payload: dict[str, Any]) -> str | None:
    """Why this rule would never do what its author expects, or None."""
    action = str(payload.get("action") or "").strip()
    if action not in INBOUND_ACTIONS:
        return f"action must be one of {', '.join(sorted(INBOUND_ACTIONS))}"
    if not str(payload.get("name") or "").strip():
        return "name is required"
    value = str(payload.get("action_value") or "").strip().lower()
    if action in INBOUND_ACTIONS_WITH_VALUE:
        if value not in VALID_IMPORTANCE_CAPS:
            return f"{action} needs action_value to be one of {', '.join(sorted(VALID_IMPORTANCE_CAPS))}"
    elif value:
        # Storing a value a verb never reads is how a rule comes to look
        # configured while doing something else.
        return f"{action} takes no action_value"
    if action == SKIP_AI:
        stated = [field for field in POST_JUDGEMENT_FIELDS if str(payload.get(field) or "").strip()]
        if stated:
            return (
                f"a {SKIP_AI} rule runs before the alert is judged, so it cannot filter on "
                f"{', '.join(stated)} — it would never match"
            )
    if not any(
        str(payload.get(field) or "").strip()
        for field in (
            "match_event_type",
            "match_importance",
            "match_source",
            "match_project",
            "match_region",
            "match_environment",
            "match_payload",
            "match_rule_name",
        )
    ):
        # Every alert would match. Almost certainly not what someone meant, and
        # expensive to discover: it silently turns the whole AI layer off.
        return "a rule with no criteria would match every alert; state at least one"
    return None


async def create_inbound_rule(session: AsyncSession, payload: dict[str, Any], *, actor: str = "") -> InboundRule:
    rule = InboundRule(
        name=str(payload.get("name") or "").strip(),
        enabled=bool(payload.get("enabled", True)),
        priority=int(payload.get("priority") or 0),
        match_event_type=str(payload.get("match_event_type") or ""),
        match_importance=str(payload.get("match_importance") or ""),
        match_duplicate=str(payload.get("match_duplicate") or "all"),
        match_source=str(payload.get("match_source") or ""),
        match_project=str(payload.get("match_project") or ""),
        match_region=str(payload.get("match_region") or ""),
        match_environment=str(payload.get("match_environment") or ""),
        match_payload=str(payload.get("match_payload") or ""),
        match_rule_name=str(payload.get("match_rule_name") or ""),
        action=str(payload.get("action") or "").strip(),
        action_value=str(payload.get("action_value") or "").strip().lower(),
        comment=str(payload.get("comment") or ""),
        created_by=actor,
    )
    session.add(rule)
    await session.flush()
    return rule


async def update_inbound_rule(session: AsyncSession, rule_id: int, payload: dict[str, Any]) -> InboundRule | None:
    rule = await session.get(InboundRule, rule_id)
    if rule is None:
        return None
    for field in (
        "name",
        "enabled",
        "priority",
        "match_event_type",
        "match_importance",
        "match_duplicate",
        "match_source",
        "match_project",
        "match_region",
        "match_environment",
        "match_payload",
        "match_rule_name",
        "action",
        "action_value",
        "comment",
    ):
        if field in payload:
            setattr(rule, field, payload[field])
    rule.updated_at = utcnow()
    await session.flush()
    return rule


async def delete_inbound_rule(session: AsyncSession, rule_id: int) -> bool:
    rule = await session.get(InboundRule, rule_id)
    if rule is None:
        return False
    await session.delete(rule)
    await session.flush()
    return True


def to_dict(rule: InboundRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "priority": rule.priority,
        "action": rule.action,
        "action_value": rule.action_value,
        "match_event_type": rule.match_event_type,
        "match_importance": rule.match_importance,
        "match_duplicate": rule.match_duplicate,
        "match_source": rule.match_source,
        "match_project": rule.match_project,
        "match_region": rule.match_region,
        "match_environment": rule.match_environment,
        "match_payload": rule.match_payload,
        "match_rule_name": rule.match_rule_name,
        "comment": rule.comment,
        "created_by": rule.created_by,
    }


async def inbound_importance_cap_for(
    *,
    parsed_data: dict[str, Any] | None,
    source: str = "",
    event_type: str = "",
    importance: str = "",
    is_duplicate: bool = False,
    rule_name: str = "",
) -> tuple[str, str]:
    """The ceiling an operator has set for this alert: (importance, rule name).

    ("", "") when none applies. Fails open like `inbound_actions_for` — a rules
    outage must not silently rewrite severities.
    """
    rules = await cached_inbound_rules()
    if not rules:
        return "", ""
    return matching_inbound_importance_cap(
        rules,
        event_type=event_type,
        importance=importance,
        source=source,
        is_duplicate=is_duplicate,
        parsed_data=parsed_data,
        rule_name=rule_name,
    )
