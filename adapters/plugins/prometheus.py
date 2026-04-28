"""Prometheus / Alertmanager 生态适配器插件。"""

from __future__ import annotations

from adapters.ecosystem_adapters import _normalize_level, _pick_first_string
from adapters.registry import registry


@registry.register_detector("prometheus")
def detect(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    alerts = data.get("alerts")
    return isinstance(alerts, list) and len(alerts) > 0


@registry.register("prometheus", aliases={"prometheus", "alertmanager"})
def normalize(data: dict) -> dict:
    first_alert = data.get("alerts", [{}])[0] if data.get("alerts") else {}
    labels = first_alert.get("labels", {}) if isinstance(first_alert, dict) else {}
    annotations = first_alert.get("annotations", {}) if isinstance(first_alert, dict) else {}

    alert_name = _pick_first_string(
        labels.get("alertname") if isinstance(labels, dict) else None,
        data.get("alertingRuleName"),
        data.get("groupLabels", {}).get("alertname") if isinstance(data.get("groupLabels"), dict) else None,
        "prometheus_alert",
    )

    level = _normalize_level(labels.get("severity") if isinstance(labels, dict) else None)

    instance = _pick_first_string(
        labels.get("instance") if isinstance(labels, dict) else None,
        labels.get("pod") if isinstance(labels, dict) else None,
        labels.get("service") if isinstance(labels, dict) else None,
        labels.get("host") if isinstance(labels, dict) else None,
    )

    normalized = dict(data)
    normalized.update(
        {
            "Type": "PrometheusAlert",
            "RuleName": alert_name,
            "alert_name": alert_name,
            "Level": level,
            "MetricName": _pick_first_string(
                labels.get("__name__") if isinstance(labels, dict) else None, "prometheus_alert"
            ),
            "event": "alert",
        }
    )

    summary = _pick_first_string(
        annotations.get("summary") if isinstance(annotations, dict) else None,
        annotations.get("description") if isinstance(annotations, dict) else None,
    )
    if summary:
        normalized["summary"] = summary

    if instance:
        normalized["Resources"] = [{"InstanceId": instance}]

    return normalized
