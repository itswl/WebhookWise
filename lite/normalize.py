"""Turn an arbitrary monitoring payload into one internal shape.

Every downstream gate reasons about (source, title, body, alert_hash), so this
is the only place that needs to know what Alertmanager or Grafana look like.
Unknown shapes degrade to a readable dump rather than being rejected — an alert
you cannot parse is still an alert you must not drop silently.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Recovery notices must not reuse the firing identity, or a resolve would look
# like a duplicate of the alert it closes. Chinese terms are matched on purpose:
# domestic monitoring stacks emit them verbatim.
_RESOLVED_MARKERS = ("resolved", "ok", "recovery", "恢复", "已恢复")


def _first(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _alertmanager(payload: dict[str, Any]) -> dict[str, str] | None:
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        return None
    first = alerts[0]
    if not isinstance(first, dict):
        return None
    raw_labels = first.get("labels")
    raw_annotations = first.get("annotations")
    labels: dict[str, Any] = raw_labels if isinstance(raw_labels, dict) else {}
    annotations: dict[str, Any] = raw_annotations if isinstance(raw_annotations, dict) else {}
    title = str(annotations.get("summary") or labels.get("alertname") or "alert")
    body = str(annotations.get("description") or json.dumps(labels, ensure_ascii=False))
    status = str(first.get("status") or payload.get("status") or "firing")
    # Identity is the label set, not the rendered text: the same alert keeps one
    # identity across firing/resolved and across description edits.
    identity = json.dumps(labels, sort_keys=True, ensure_ascii=False) or title
    if len(alerts) > 1:
        body = f"{body}\n(+{len(alerts) - 1} more alerts in this group)"
    return {"title": title, "body": body, "status": status, "identity": identity}


def _grafana(payload: dict[str, Any]) -> dict[str, str] | None:
    if "ruleName" not in payload and "evalMatches" not in payload:
        return None
    title = _first(payload, "title", "ruleName") or "grafana alert"
    body = _first(payload, "message", "ruleUrl")
    status = _first(payload, "state") or "alerting"
    return {"title": title, "body": body, "status": status, "identity": title}


def _generic(payload: dict[str, Any]) -> dict[str, str]:
    title = _first(payload, "title", "subject", "name", "alertname", "event", "message") or "webhook event"
    body = _first(payload, "body", "description", "content", "detail", "text")
    if not body:
        body = json.dumps(payload, ensure_ascii=False)[:2000]
    status = _first(payload, "status", "state", "severity") or "firing"
    return {"title": title, "body": body, "status": status, "identity": title}


def normalize(source: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {"body": str(payload)}

    parsed = _alertmanager(payload) or _grafana(payload) or _generic(payload)
    status = parsed["status"].lower()
    resolved = any(marker in status for marker in _RESOLVED_MARKERS)

    # alert_hash is the dedup/cooldown identity. Firing and resolved of the same
    # alert are deliberately different identities.
    digest = hashlib.sha256(f"{source}|{parsed['identity']}|{resolved}".encode()).hexdigest()[:32]

    return {
        "source": source,
        "title": parsed["title"][:300],
        "body": parsed["body"][:4000],
        "resolved": resolved,
        "alert_hash": digest,
        "raw": payload,
    }
