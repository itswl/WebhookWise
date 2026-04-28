"""Grafana 生态适配器插件。"""

from __future__ import annotations

from adapters.ecosystem_adapters import _normalize_level, _pick_first_string
from adapters.registry import registry


@registry.register_detector("grafana")
def detect(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    return any(k in data for k in ("ruleName", "dashboardId", "panelId")) and any(
        k in data for k in ("state", "status", "title")
    )


@registry.register("grafana", aliases={"grafana"})
def normalize(data: dict) -> dict:
    rule_name = _pick_first_string(data.get("ruleName"), data.get("title"), "grafana_alert")
    state = _pick_first_string(data.get("state"), data.get("status"))

    level = _normalize_level(state)
    summary = _pick_first_string(data.get("message"), data.get("title"), data.get("ruleUrl"))
    resource_id = _pick_first_string(data.get("ruleId"), data.get("dashboardId"), data.get("panelId"))

    normalized = dict(data)
    normalized.update(
        {
            "Type": "GrafanaAlert",
            "RuleName": rule_name,
            "alert_name": rule_name,
            "Level": level,
            "MetricName": _pick_first_string(data.get("evalMatches"), rule_name),
            "event": "alert",
            "event_type": _pick_first_string(state, "alert"),
        }
    )

    if summary:
        normalized["summary"] = summary

    if resource_id:
        normalized["Resources"] = [{"InstanceId": str(resource_id)}]

    return normalized
