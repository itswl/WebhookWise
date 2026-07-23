"""
tests/adapters/test_ecosystem_adapters.py
=========================================
Tests how normalize_webhook_event() normalizes webhook formats from various platforms.
Ensures the adapters correctly identify the source and extract key fields.
"""

import pytest

from adapters.ecosystem_adapters import normalize_level, normalize_webhook_event

# ── Prometheus / Alertmanager ──────────────────────────────────────────────


PROMETHEUS_PAYLOAD = {
    "alerts": [
        {
            "labels": {
                "alertname": "HighCPUUsage",
                "severity": "critical",
                "instance": "prod-01:9100",
            },
            "annotations": {
                "summary": "CPU usage is above 90%",
                "description": "Node prod-01 CPU at 95%",
            },
            "status": "firing",
        }
    ],
    "status": "firing",
}


def test_prometheus_detected_by_payload():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, None)
    assert result.source == "prometheus"
    assert result.adapter == "prometheus"


def test_prometheus_rule_name_extracted():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, None)
    assert result.data.get("RuleName") == "HighCPUUsage"


def test_prometheus_level_normalized_to_critical():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, None)
    assert result.data.get("Level") == "critical"


def test_prometheus_resources_extracted():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, None)
    resources = result.data.get("Resources", [])
    assert len(resources) == 1
    assert resources[0]["InstanceId"] == "prod-01:9100"


def test_prometheus_summary_extracted():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, None)
    assert "summary" in result.data
    assert "CPU" in result.data["summary"]


def test_prometheus_type_set():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, None)
    assert result.data.get("Type") == "PrometheusAlert"


def test_prometheus_explicit_source():
    result = normalize_webhook_event(PROMETHEUS_PAYLOAD, "prometheus", {})
    assert result.source == "prometheus"
    assert result.data["RuleName"] == "HighCPUUsage"
    assert result.data["Resources"][0]["InstanceId"] == "prod-01:9100"


def test_prometheus_identity_uses_internal_service_labels():
    from adapters.normalized import extract_alert_identity

    payload = {
        "alerts": [
            {
                "labels": {
                    "alertname": "4fe6502e-587e-43a1-860f-bb575ab8476b",
                    "internal_label_alert_id": "6a0142e48f78951ec14b1fa4",
                    "internal_label_alert_level": "warning",
                    "internal_label_namespace": "eve-cn-prod",
                    "internal_label_service": "ai-router",
                },
                "annotations": {"summary": "OpenRouter success rate is 0%"},
            }
        ],
        "status": "firing",
    }

    result = normalize_webhook_event(payload, "prometheus", {})
    identity = extract_alert_identity(result.data)

    assert identity is not None
    assert identity["service"] == "ai-router"
    assert identity["resource"] == "ai-router"
    assert identity["fingerprint"] == "6a0142e48f78951ec14b1fa4"
    assert identity["severity"] == "warning"


# ── Grafana ───────────────────────────────────────────────────────────────


GRAFANA_PAYLOAD = {
    "ruleName": "DiskSpaceLow",
    "state": "alerting",
    "dashboardId": 42,
    "panelId": 7,
    "message": "Disk usage exceeded 85%",
    "ruleId": 101,
}


def test_grafana_detected_by_payload():
    result = normalize_webhook_event(GRAFANA_PAYLOAD, None)
    assert result.source == "grafana"


def test_grafana_auto_detect_without_source():
    payload = {"ruleName": "API Error Rate", "state": "alerting", "title": "Error Rate Alert", "dashboardId": "db-001"}
    result = normalize_webhook_event(payload, None, {})
    assert result.adapter == "grafana"
    assert result.data["RuleName"] == "API Error Rate"
    assert result.data["Level"] == "critical"  # alerting → critical


def test_grafana_rule_name_extracted():
    result = normalize_webhook_event(GRAFANA_PAYLOAD, None)
    assert result.data.get("RuleName") == "DiskSpaceLow"


def test_grafana_level_alerting_normalized():
    result = normalize_webhook_event(GRAFANA_PAYLOAD, None)
    assert result.data.get("Level") == "critical"  # "alerting" → critical


def test_grafana_summary_from_message():
    result = normalize_webhook_event(GRAFANA_PAYLOAD, None)
    assert result.data.get("summary") == "Disk usage exceeded 85%"


def test_grafana_type_set():
    result = normalize_webhook_event(GRAFANA_PAYLOAD, None)
    assert result.data.get("Type") == "GrafanaAlert"


def test_grafana_unified_webhook_extracts_alert_details():
    from adapters.normalized import extract_alert_identity

    payload = {
        "receiver": "webhookwise",
        "status": "firing",
        "orgId": 1,
        "title": "[FIRING:1] APIErrorRate",
        "message": "Fallback group message",
        "commonLabels": {"team": "payments"},
        "commonAnnotations": {"description": "Fallback description"},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "APIErrorRate",
                    "service": "checkout-api",
                    "instance": "checkout-01",
                },
                "annotations": {
                    "summary": "API error rate exceeded 5%",
                    "description": "The checkout API is returning too many errors",
                },
                "fingerprint": "b7d4f7d0f4be2c11",
                "valueString": "A=8.2",
            }
        ],
    }

    result = normalize_webhook_event(payload, None)
    identity = extract_alert_identity(result.data)

    assert result.source == "grafana"
    assert result.adapter == "grafana"
    assert result.data["Type"] == "GrafanaAlert"
    assert result.data["RuleName"] == "APIErrorRate"
    assert result.data["Level"] == "critical"
    assert result.data["summary"] == "API error rate exceeded 5%"
    assert result.data["service"] == "checkout-api"
    assert result.data["Resources"] == [{"InstanceId": "checkout-01"}]
    assert identity == {
        "source": "grafana",
        "name": "apierrorrate",
        "resource": "checkout-01",
        "service": "checkout-api",
        "fingerprint": "b7d4f7d0f4be2c11",
        "severity": "critical",
    }


# ── Datadog ───────────────────────────────────────────────────────────────


def test_datadog_detected_by_payload():
    payload = {
        "alert_type": "error",
        "event_type": "metric_alert_monitor",
        "alert_name": "Memory usage too high",
        "host": "web-server-02",
        "query": "avg:system.mem.used{host:web-server-02} > 90",
    }
    result = normalize_webhook_event(payload, None)
    assert result.source == "datadog"
    assert result.data.get("RuleName") == "Memory usage too high"
    assert result.data.get("Level") == "critical"


def test_datadog_host_from_tags():
    payload = {
        "title": "Memory usage high",
        "alert_type": "error",
        "query": "avg(last_5m):avg:system.mem.pct_usable{*} < 0.1",
        "tags": ["host:web-01", "service:web"],
    }
    result = normalize_webhook_event(payload, "unknown", {})
    assert result.adapter == "datadog"
    resources = result.data.get("Resources", [])
    assert any(r["InstanceId"] == "web-01" for r in resources)


def test_datadog_summary_from_text():
    payload = {
        "alert_type": "error",
        "event_type": "metric_alert",
        "query": "avg:cpu > 80",
        "text": "CPU is too high",
    }
    result = normalize_webhook_event(payload, None)
    assert "CPU" in (result.data.get("summary") or "")


# ── PagerDuty ─────────────────────────────────────────────────────────────


def test_pagerduty_from_incident():
    payload = {
        "incident": {
            "id": "P123ABC",
            "title": "Database connection failure",
            "urgency": "high",
            "service": {"summary": "Production DB"},
        },
        "event": {"event_type": "incident.trigger"},
    }
    result = normalize_webhook_event(payload, "pagerduty", {})
    assert result.adapter == "pagerduty"
    assert result.data.get("RuleName") == "Database connection failure"
    assert result.data.get("alert_id") == "P123ABC"
    assert result.data.get("Level") == "critical"


def test_pagerduty_from_event_data():
    payload = {
        "event": {
            "event_type": "incident.triggered",
            "data": {"id": "PDI123", "title": "Database Down", "service": {"summary": "order-service"}},
        }
    }
    result = normalize_webhook_event(payload, "pagerduty", {})
    assert result.adapter == "pagerduty"
    assert result.data["RuleName"] == "Database Down"
    assert result.data["alert_id"] == "PDI123"
    assert result.data["service"] == "order-service"


# ── Passthrough / Unknown ────────────────────────────────────────────────


def test_unknown_payload_passes_through():
    """An unrecognized payload is passed through unchanged."""
    unknown = {"custom_field": "value", "some_data": 123}
    result = normalize_webhook_event(unknown, "my-custom-system")
    assert result.source == "my-custom-system"
    assert result.adapter == "passthrough"
    assert result.data["custom_field"] == "value"


def test_custom_source_passthrough():
    payload = {"foo": "bar"}
    result = normalize_webhook_event(payload, "custom-source", {})
    assert result.adapter == "passthrough"
    assert result.source == "custom-source"
    assert result.data == payload


def test_non_dict_payload_wraps_in_raw():
    """A non-dict payload is wrapped in a raw field."""
    result = normalize_webhook_event("plain text payload", None)
    assert "raw" in result.data
    assert result.data["raw"] == "plain text payload"


def test_empty_payload_passes_through():
    result = normalize_webhook_event({}, None)
    assert result.source == "unknown"


# ── Level normalization edge cases ────────────────────────────────────────


@pytest.mark.parametrize(
    "val,expected",
    [
        ("resolved", "info"),
        ("ok", "info"),
        ("normal", "info"),
        ("critical", "critical"),
        ("fatal", "critical"),
        ("p0", "critical"),
        ("error", "critical"),
        ("firing", "critical"),
        ("triggered", "critical"),
        ("warning", "warning"),
        ("warn", "warning"),
        ("p1", "warning"),
    ],
)
def test_normalize_level_mapping(val, expected):
    assert normalize_level(val) == expected, f"normalize_level({val!r}) should be {expected!r}"
