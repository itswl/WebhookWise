"""Severity-based ingest queue routing.

Decides whether a freshly received webhook should be enqueued onto the priority
queue (high-severity, so a low-priority AI backlog can't starve it) or the
default queue. Best-effort: any parse/field failure routes to the default queue,
and a misroute never affects correctness (both worker pools run the identical
pipeline) — it only changes which pool processes the alert first.
"""

from __future__ import annotations

from typing import Any

from core import json
from core.app_context import get_config_manager
from core.logger import get_logger
from core.text import split_csv_lower

logger = get_logger("webhooks.ingest_routing")

# Common places a raw severity/level lives across supported sources
# (Prometheus/Alertmanager, Grafana, Feishu, Datadog, generic).
_SEVERITY_KEYS = ("severity", "Severity", "level", "Level", "priority", "Priority", "urgency")


def _peek_raw_severity(payload: dict[str, Any]) -> str | None:
    for key in _SEVERITY_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    labels = payload.get("labels")
    if isinstance(labels, dict):
        for key in ("severity", "level"):
            value = labels.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def is_priority_mapping(payload: dict[str, Any]) -> bool:
    """Best-effort: True when an already-parsed payload's severity is priority.

    Never raises; returns False on any lookup failure or when routing is off.
    """
    try:
        cfg = get_config_manager().mq
        if not getattr(cfg, "WEBHOOK_PRIORITY_ROUTING_ENABLED", False):
            return False
        priority_levels = set(split_csv_lower(str(cfg.WEBHOOK_PRIORITY_LEVELS or "critical")))
        raw_severity = _peek_raw_severity(payload)
        if not raw_severity:
            return False
        from adapters.simple_adapters import normalize_level

        return normalize_level(raw_severity) in priority_levels
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        logger.debug("[Ingest] 优先级探测失败,按普通队列处理: %s", exc)
        return False


def is_priority_payload(raw_body: str) -> bool:
    """Best-effort: True when the raw JSON body's severity maps to a priority level.

    Never raises — returns False on any parse/lookup failure so the caller falls
    back to the default queue.
    """
    if not raw_body:
        return False
    try:
        loaded = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(loaded, dict):
        return False
    return is_priority_mapping(loaded)
