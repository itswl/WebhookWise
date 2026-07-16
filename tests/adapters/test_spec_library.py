"""Fixture tests for the declarative spec library shipped in adapters/specs/.

Each spec gets a realistic fixture payload asserted three ways:

- selection: a registry holding all shipped specs routes the payload to the
  expected spec (registration follows file-name order, so this also proves no
  earlier sibling spec steals it);
- end-to-end: ``normalize_webhook_event`` on the global registry routes the
  payload past the built-in code adapters, which detect first;
- normalization: alert identity and canonical output fields come out as
  expected (identity values are lowercased by ``AlertIdentity.to_payload``).
"""

from __future__ import annotations

import pytest

from adapters.declarative import CompiledSpec, load_specs, register_declarative_adapters
from adapters.ecosystem_adapters import normalize_webhook_event
from adapters.registry import AdapterRegistry

# ── Fixture payloads (one realistic sample per shipped spec) ─────────────────

ZABBIX_PAYLOAD = {
    "event_name": "High CPU utilization on web-01",
    "event_id": "4721",
    "event_status": "PROBLEM",
    "event_severity": "High",
    "host_name": "web-01",
    "host_ip": "10.0.0.5",
    "trigger_description": "CPU utilization above 90% for 5m",
}

UPTIME_KUMA_PAYLOAD = {
    "heartbeat": {"status": 0, "time": "2026-07-16 10:32:01", "msg": "Request failed with status code 500"},
    "monitor": {"name": "api-prod", "url": "https://api.example.com/health"},
    "msg": "[api-prod] [Down] Request failed with status code 500",
}

ALIYUN_CMS_PAYLOAD = {
    "alertName": "cpu_total on prod-web-01",
    "alertState": "ALERT",
    "curValue": "95.2",
    "dimensions": "{userId=12345, instanceId=i-abc123}",
    "expression": "$Average>90",
    "instanceName": "prod-web-01",
    "metricName": "cpu_total",
    "namespace": "acs_ecs_dashboard",
    "triggerLevel": "CRITICAL",
    "lastTime": "300000",
    "ruleId": "putNewAlarm_ab12cd34",
    "timestamp": "1752650000000",
    "userId": "12345",
}

TENCENT_CLOUD_MONITOR_PAYLOAD = {
    "sessionId": "6b52d17a-4d78-4bc7-9f8e-2a2c61e6b0f1",
    "alarmStatus": "1",
    "alarmType": "metric",
    "alarmObjInfo": {"region": "gz", "namespace": "qce/cvm", "dimensions": {"unInstanceId": "ins-o9p3rg3m"}},
    "alarmPolicyInfo": {
        "policyId": "policy-n4exeh88",
        "policyName": "cpu-usage-high",
        "conditions": {
            "metricName": "cpu_usage",
            "metricShowName": "CPU utilization",
            "calcType": ">",
            "calcValue": "90",
            "currentValue": "97",
        },
    },
    "firstOccurTime": "2026-07-16 10:00:00",
    "durationTime": 500,
    "recoverTime": "0",
}

JENKINS_PAYLOAD = {
    "name": "deploy-api",
    "url": "job/deploy-api/",
    "build": {
        "full_url": "https://ci.example.com/job/deploy-api/118/",
        "number": 118,
        "phase": "COMPLETED",
        "status": "FAILURE",
        "url": "job/deploy-api/118/",
    },
}

SENTRY_PAYLOAD = {
    "id": "42",
    "project": "backend",
    "project_name": "Backend",
    "project_slug": "backend",
    "level": "error",
    "culprit": "app.api.handlers in create_order",
    "message": "IntegrityError: duplicate key value violates unique constraint",
    "url": "https://sentry.example.com/organizations/acme/issues/42/",
    "event": {
        "event_id": "d34db33f2ab84a21a4a1cd6d72cdd5b7",
        "title": "IntegrityError: duplicate key value violates unique constraint",
    },
}

FIXTURES: dict[str, dict[str, object]] = {
    "zabbix": ZABBIX_PAYLOAD,
    "uptime_kuma": UPTIME_KUMA_PAYLOAD,
    "aliyun_cms": ALIYUN_CMS_PAYLOAD,
    "tencent_cloud_monitor": TENCENT_CLOUD_MONITOR_PAYLOAD,
    "jenkins": JENKINS_PAYLOAD,
    "sentry": SENTRY_PAYLOAD,
}


@pytest.fixture(scope="module")
def specs_by_name() -> dict[str, CompiledSpec]:
    return {spec.name: spec for spec in load_specs()}


@pytest.fixture(scope="module")
def declarative_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    register_declarative_adapters(registry)
    return registry


# ── Detection ────────────────────────────────────────────────────────────────


def test_spec_library_loads(specs_by_name: dict[str, CompiledSpec]) -> None:
    assert set(FIXTURES) <= set(specs_by_name)


@pytest.mark.parametrize("spec_name", sorted(FIXTURES))
def test_fixture_selects_its_spec(declarative_registry: AdapterRegistry, spec_name: str) -> None:
    assert declarative_registry.find_adapter_by_payload(FIXTURES[spec_name]) == spec_name


@pytest.mark.parametrize("spec_name", sorted(FIXTURES))
def test_fixture_survives_builtin_detectors(spec_name: str) -> None:
    # Global registry (initialized by tests/conftest.py) checks the built-in
    # code adapters first; the fixture must still land on its own spec.
    result = normalize_webhook_event(FIXTURES[spec_name], None)
    assert result.adapter == spec_name
    assert result.source == spec_name
    assert result.data["_alert_identity"]["source"] == spec_name


def test_fixtures_do_not_cross_match(specs_by_name: dict[str, CompiledSpec]) -> None:
    for spec_name, payload in FIXTURES.items():
        for other_name in FIXTURES:
            assert specs_by_name[other_name].detector(payload) is (other_name == spec_name), (
                f"{other_name} detector vs {spec_name} fixture"
            )


def test_generic_payload_matches_no_new_spec(
    declarative_registry: AdapterRegistry, specs_by_name: dict[str, CompiledSpec]
) -> None:
    generic = {
        "alert_name": "High CPU",
        "level": "critical",
        "host": "web-01",
        "service": "web",
        "id": "evt-1",
        "message": "CPU above 90% for 5m",
    }
    for spec_name in sorted(FIXTURES):
        assert specs_by_name[spec_name].detector(generic) is False
    assert declarative_registry.find_adapter_by_payload(generic) == "generic_json"


def test_aliases_resolve(declarative_registry: AdapterRegistry) -> None:
    expected = {
        "zabbix_webhook": "zabbix",
        "uptimekuma": "uptime_kuma",
        "uptime-kuma": "uptime_kuma",
        "aliyun": "aliyun_cms",
        "aliyun_cloudmonitor": "aliyun_cms",
        "tencent_cm": "tencent_cloud_monitor",
        "qcloud_monitor": "tencent_cloud_monitor",
        "jenkins_notification": "jenkins",
        "sentry_legacy": "sentry",
    }
    for alias, name in expected.items():
        assert declarative_registry.find_adapter_by_source(alias) == name


# ── Normalization (identity + canonical output fields) ───────────────────────


def test_zabbix_normalizes(specs_by_name: dict[str, CompiledSpec]) -> None:
    data = specs_by_name["zabbix"].normalizer(ZABBIX_PAYLOAD)
    identity = data["_alert_identity"]
    assert identity["source"] == "zabbix"
    assert identity["name"] == "high cpu utilization on web-01"
    assert identity["resource"] == "web-01"
    assert identity["fingerprint"] == "4721"
    assert identity["severity"] == "critical"  # "High" via normalize_level
    assert data["Type"] == "ZabbixAlert"
    assert data["RuleName"] == "High CPU utilization on web-01"
    assert data["Level"] == "critical"
    assert data["summary"] == "CPU utilization above 90% for 5m"
    assert data["Resources"] == [{"InstanceId": "web-01"}]


def test_uptime_kuma_normalizes(specs_by_name: dict[str, CompiledSpec]) -> None:
    data = specs_by_name["uptime_kuma"].normalizer(UPTIME_KUMA_PAYLOAD)
    identity = data["_alert_identity"]
    assert identity["source"] == "uptime_kuma"
    assert identity["name"] == "api-prod"
    assert identity["resource"] == "https://api.example.com/health"
    # Numeric heartbeat.status is deliberately not severity-normalized.
    assert "severity" not in identity
    assert "Level" not in data
    assert data["Type"] == "UptimeKumaAlert"
    assert data["RuleName"] == "api-prod"
    assert data["summary"] == "[api-prod] [Down] Request failed with status code 500"


def test_aliyun_cms_normalizes(specs_by_name: dict[str, CompiledSpec]) -> None:
    data = specs_by_name["aliyun_cms"].normalizer(ALIYUN_CMS_PAYLOAD)
    identity = data["_alert_identity"]
    assert identity["source"] == "aliyun_cms"
    assert identity["name"] == "cpu_total on prod-web-01"
    assert identity["resource"] == "prod-web-01"
    assert identity["service"] == "acs_ecs_dashboard"
    assert identity["fingerprint"] == "putnewalarm_ab12cd34"
    assert identity["severity"] == "critical"  # "CRITICAL" via normalize_level
    assert data["Type"] == "AliyunCMSAlert"
    assert data["RuleName"] == "cpu_total on prod-web-01"
    assert data["Level"] == "critical"
    assert data["summary"] == "$Average>90"
    assert data["Resources"] == [{"InstanceId": "prod-web-01"}]


def test_tencent_cloud_monitor_normalizes(specs_by_name: dict[str, CompiledSpec]) -> None:
    data = specs_by_name["tencent_cloud_monitor"].normalizer(TENCENT_CLOUD_MONITOR_PAYLOAD)
    identity = data["_alert_identity"]
    assert identity["source"] == "tencent_cloud_monitor"
    assert identity["name"] == "cpu-usage-high"
    assert identity["resource"] == "ins-o9p3rg3m"
    assert identity["service"] == "qce/cvm"
    assert identity["fingerprint"] == "policy-n4exeh88"
    # alarmStatus "0"/"1" is a flag, deliberately not severity-normalized.
    assert "severity" not in identity
    assert "Level" not in data
    assert data["Type"] == "TencentCloudMonitorAlarm"
    assert data["RuleName"] == "cpu-usage-high"
    assert data["summary"] == "CPU utilization"


def test_jenkins_normalizes(specs_by_name: dict[str, CompiledSpec]) -> None:
    data = specs_by_name["jenkins"].normalizer(JENKINS_PAYLOAD)
    identity = data["_alert_identity"]
    assert identity["source"] == "jenkins"
    assert identity["name"] == "deploy-api"
    assert identity["resource"] == "job/deploy-api/"  # job URL, not per-build URL
    assert identity["fingerprint"] == "https://ci.example.com/job/deploy-api/118/"
    # SUCCESS/FAILURE/UNSTABLE all normalize to "warning", deliberately unmapped.
    assert "severity" not in identity
    assert "Level" not in data
    assert data["Type"] == "JenkinsBuild"
    assert data["RuleName"] == "deploy-api"
    assert data["summary"] == "https://ci.example.com/job/deploy-api/118/"


def test_sentry_normalizes(specs_by_name: dict[str, CompiledSpec]) -> None:
    data = specs_by_name["sentry"].normalizer(SENTRY_PAYLOAD)
    identity = data["_alert_identity"]
    assert identity["source"] == "sentry"
    assert identity["name"] == "integrityerror: duplicate key value violates unique constraint"
    assert identity["resource"] == "app.api.handlers in create_order"
    assert identity["service"] == "backend"
    assert identity["fingerprint"] == "42"  # issue id wins over event.event_id
    assert identity["severity"] == "critical"  # "error" via normalize_level
    assert data["Type"] == "SentryEvent"
    assert data["RuleName"] == "IntegrityError: duplicate key value violates unique constraint"
    assert data["Level"] == "critical"
    assert data["summary"] == "IntegrityError: duplicate key value violates unique constraint"
