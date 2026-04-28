"""Datadog 生态适配器插件。"""

from __future__ import annotations

from adapters.ecosystem_adapters import _extract_tag_value, _normalize_level, _pick_first_string
from adapters.registry import registry


@registry.register_detector("datadog")
def detect(data: dict) -> bool:
    if not isinstance(data, dict):
        return False

    keys = ("alert_type", "event_type", "event_type_text", "query", "title")
    present = sum(1 for key in keys if key in data)
    return present >= 2 or ("tags" in data and ("alert_type" in data or "title" in data))


@registry.register("datadog", aliases={"datadog"})
def normalize(data: dict) -> dict:
    tags = data.get("tags")
    title = _pick_first_string(data.get("alert_name"), data.get("title"), "datadog_alert")
    alert_type = _pick_first_string(
        data.get("alert_type"), data.get("event_type"), data.get("event_type_text"), data.get("priority")
    )

    host = _pick_first_string(data.get("host"), _extract_tag_value(tags, "host"), _extract_tag_value(tags, "instance"))
    service = _pick_first_string(data.get("service"), _extract_tag_value(tags, "service"))

    normalized = dict(data)
    normalized.update(
        {
            "Type": "DatadogAlert",
            "RuleName": title,
            "alert_name": title,
            "Level": _normalize_level(alert_type),
            "MetricName": _pick_first_string(data.get("metric"), data.get("query"), "datadog_alert"),
            "event": "alert",
            "event_type": _pick_first_string(alert_type, "alert"),
            "alert_id": _pick_first_string(data.get("id"), data.get("alert_id")),
        }
    )

    if service:
        normalized["service"] = service

    if host:
        normalized["Resources"] = [{"InstanceId": host}]

    text = _pick_first_string(data.get("text"), data.get("body"))
    if text:
        normalized["summary"] = text

    return normalized
