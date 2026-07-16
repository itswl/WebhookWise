"""Auto-SLA policy: arm incident SLAs from importance, without an operator.

The SLA machinery (sla_due_at + the breach sweep in notifications.py) predates
this module but was manual-only: someone had to set an SLA on each incident via
the workflow API, so in practice breaches never fired. This policy arms the
timer automatically — "a high-importance incident nobody acknowledges within N
minutes escalates" — which is the lightweight escalation story: no on-call
rotas, just "get loud when ignored".

Off by default (empty mapping). Configure e.g.:

    WEBHOOK_INCIDENT_AUTO_SLA_MINUTES=high=30,medium=240
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from core.logger import get_logger
from models import Incident

logger = get_logger("incidents.auto_sla")

_VALID_IMPORTANCE = frozenset({"high", "medium", "low"})


def parse_importance_minutes(raw: str) -> dict[str, int]:
    """Parse "high=30,medium=240" into {"high": 30, "medium": 240}.

    Invalid entries are dropped with a warning rather than failing the scan —
    a config typo must not stop incident grouping.
    """
    mapping: dict[str, int] = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key not in _VALID_IMPORTANCE or not value.isdigit() or int(value) <= 0:
            logger.warning("[AutoSLA] Ignoring invalid auto-SLA entry %r (expected e.g. high=30)", part)
            continue
        mapping[key] = int(value)
    return mapping


@dataclass(frozen=True, slots=True)
class AutoSlaPolicy:
    """Importance → minutes-to-acknowledge mapping. Empty = disabled."""

    minutes_by_importance: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls) -> AutoSlaPolicy:
        from core.app_context import get_config_manager

        raw = str(get_config_manager().notifications.INCIDENT_AUTO_SLA_MINUTES or "")
        return cls(minutes_by_importance=parse_importance_minutes(raw))

    @property
    def enabled(self) -> bool:
        return bool(self.minutes_by_importance)


def apply_auto_sla(incident: Incident, policy: AutoSlaPolicy) -> bool:
    """Arm the incident's SLA from its importance; return whether it was set.

    Only fills an EMPTY sla_due_at: an operator-set (or previously armed) SLA is
    never moved. Note the flip side: clearing an SLA on a still-firing incident
    re-arms it when the next member lands — "still firing" means "still on the
    hook", by design.
    """
    if not policy.enabled or incident.sla_due_at is not None:
        return False
    if incident.workflow_status in ("resolved", "ignored"):
        return False
    minutes = policy.minutes_by_importance.get(str(incident.top_importance or "").lower())
    if minutes is None:
        return False
    base = incident.updated_at or incident.started_at
    if base is None:
        return False
    incident.sla_due_at = base + timedelta(minutes=minutes)
    logger.info(
        "[AutoSLA] Armed SLA incident=%s importance=%s due_at=%s",
        incident.id,
        incident.top_importance,
        incident.sla_due_at.isoformat(),
    )
    return True
