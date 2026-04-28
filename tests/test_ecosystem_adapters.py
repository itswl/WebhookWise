from adapters.ecosystem_adapters import normalize_webhook_event


def test_prometheus_normalization_from_source_alias():
    payload = {
        "status": "firing",
        "alerts": [
            {
                "labels": {"alertname": "HighCPU", "severity": "critical", "instance": "node-1"},
                "annotations": {"summary": "CPU > 90%"},
            }
        ],
    }

    normalized = normalize_webhook_event(payload, "prometheus", {})

    assert normalized.adapter == "prometheus"
    assert normalized.source == "prometheus"
    assert normalized.data["RuleName"] == "HighCPU"
    assert normalized.data["Level"] == "critical"
    assert normalized.data["Resources"][0]["InstanceId"] == "node-1"


def test_grafana_auto_detect_without_source():
    payload = {"ruleName": "API Error Rate", "state": "alerting", "title": "Error Rate Alert", "dashboardId": "db-001"}

    normalized = normalize_webhook_event(payload, None, {})

    assert normalized.adapter == "grafana"
    assert normalized.source == "grafana"
    assert normalized.data["RuleName"] == "API Error Rate"
    assert normalized.data["Level"] == "critical"


def test_pagerduty_event_normalization():
    payload = {
        "event": {
            "event_type": "incident.triggered",
            "data": {"id": "PDI123", "title": "Database Down", "service": {"summary": "order-service"}},
        }
    }

    normalized = normalize_webhook_event(payload, "pagerduty", {})

    assert normalized.adapter == "pagerduty"
    assert normalized.source == "pagerduty"
    assert normalized.data["RuleName"] == "Database Down"
    assert normalized.data["alert_id"] == "PDI123"
    assert normalized.data["service"] == "order-service"


def test_datadog_normalization_from_payload():
    payload = {
        "title": "Memory usage high",
        "alert_type": "error",
        "query": "avg(last_5m):avg:system.mem.pct_usable{*} < 0.1",
        "tags": ["host:web-01", "service:web"],
    }

    normalized = normalize_webhook_event(payload, "unknown", {})

    assert normalized.adapter == "datadog"
    assert normalized.source == "datadog"
    assert normalized.data["RuleName"] == "Memory usage high"
    assert normalized.data["Level"] == "critical"
    assert normalized.data["Resources"][0]["InstanceId"] == "web-01"


def test_unknown_payload_passthrough():
    payload = {"foo": "bar"}

    normalized = normalize_webhook_event(payload, "custom-source", {})

    assert normalized.adapter == "passthrough"
    assert normalized.source == "custom-source"
    assert normalized.data == payload
