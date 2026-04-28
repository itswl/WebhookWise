"""PagerDuty 生态适配器插件。"""

from __future__ import annotations

from adapters.ecosystem_adapters import _normalize_level, _pick_first_string
from adapters.registry import registry


@registry.register_detector("pagerduty")
def detect(data: dict) -> bool:
    if not isinstance(data, dict):
        return False

    event = data.get("event")
    if isinstance(event, dict) and event.get("event_type"):
        return True

    return "incident" in data and any(k in data for k in ("messages", "event"))


@registry.register("pagerduty", aliases={"pagerduty"})
def normalize(data: dict) -> dict:
    event_obj = data.get("event") if isinstance(data.get("event"), dict) else {}
    incident = data.get("incident") if isinstance(data.get("incident"), dict) else {}
    event_data = event_obj.get("data") if isinstance(event_obj.get("data"), dict) else {}

    title = _pick_first_string(
        incident.get("title"), event_data.get("title"), data.get("description"), "pagerduty_incident"
    )

    event_type = _pick_first_string(event_obj.get("event_type"), data.get("event_type"), "incident.triggered")
    urgency = _pick_first_string(incident.get("urgency"), event_data.get("urgency"), event_type)

    service = None
    incident_service = incident.get("service")
    if isinstance(incident_service, dict):
        service = _pick_first_string(incident_service.get("summary"), incident_service.get("id"))

    if not service:
        event_service = event_data.get("service") if isinstance(event_data.get("service"), dict) else {}
        service = _pick_first_string(event_service.get("summary"), event_service.get("id"))

    incident_id = _pick_first_string(incident.get("id"), event_data.get("id"), data.get("incident_id"))

    normalized = dict(data)
    normalized.update(
        {
            "Type": "PagerDutyEvent",
            "RuleName": title,
            "alert_name": title,
            "Level": _normalize_level(urgency),
            "MetricName": _pick_first_string(event_type, "pagerduty_incident"),
            "event": event_type,
            "event_type": event_type,
            "alert_id": incident_id,
        }
    )

    if service:
        normalized["service"] = service

    if incident_id:
        normalized["Resources"] = [{"InstanceId": incident_id}]

    return normalized
