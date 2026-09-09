"""Immutable data contracts used by the forwarding domain."""

from dataclasses import dataclass

from core.text import split_csv_lower


@dataclass(frozen=True)
class ForwardRuleSnapshot:
    """A forwarding rule detached from its persistence model."""

    id: int | None
    name: str
    match_event_type: str
    match_importance: str
    match_source: str
    match_duplicate: str
    match_payload: str
    target_type: str
    target_url: str
    stop_on_match: bool
    target_name: str = ""
    target_gateway: str = ""
    match_project: str = ""
    match_region: str = ""
    match_environment: str = ""


# Event types that are WebhookWise reporting on ITSELF — an incident opening, an
# SLA breaching, a forward giving up. `outbox_exhausted` is the name this system
# emits; `forward_exhausted` is accepted as the same idea under an older name.
#
# Shared because two panels have to agree about them. A rule matching only these
# never carries an alert, so any number computed from ALERT traffic means
# something else for it: the noise centre must not propose tuning it, and the ROI
# panel must not read its empty decision-trace history as "matched nothing".
SYSTEM_EVENT_TYPES = frozenset(
    {
        "incident_created",
        "incident_resolved",
        "sla_breached",
        "deep_analysis",
        "ai_error",
        "ai_degraded",
        "outbox_exhausted",
        "forward_exhausted",
    }
)


def matches_only_system_events(match_event_type: str) -> bool:
    """Whether every event type this rule names is a system event.

    An empty criterion matches everything, including alerts, so it is NOT
    system-only — the caller's fallback (alert traffic) is the right reading.
    """
    event_types = split_csv_lower(str(match_event_type or ""))
    return bool(event_types) and all(event_type in SYSTEM_EVENT_TYPES for event_type in event_types)
