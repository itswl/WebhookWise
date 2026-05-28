from services.analysis.alert_identity_context import build_alert_identity_context


def test_build_alert_identity_context_extracts_volcengine_fields() -> None:
    payload = {
        "RuleName": "对象存储桶状态码告警策略_自定义",
        "RuleId": "rule-1",
        "Level": "critical",
        "AccountId": "2101986858",
        "Namespace": "VCM_TOS",
        "SubNamespace": "bucket",
        "Project": "default",
        "Resources": [
            {
                "Name": "common-prod-object",
                "Id": "common-prod-object",
                "Region": "cn-shanghai",
                "ProjectName": "common-infra",
                "Dimensions": [{"Name": "ResourceID", "NameCN": "存储桶", "Value": "common-prod-object"}],
                "Metrics": [
                    {
                        "Name": "4xxQPS",
                        "DescriptionCN": "4xx状态码QPS",
                        "CurrentValue": 39.06,
                        "Threshold": 0,
                        "Unit": "Count/s",
                        "TriggerCondition": "桶状态码/4xx状态码QPS > 0",
                    }
                ],
            }
        ],
    }

    context = build_alert_identity_context("volcengine", payload)

    assert context["identity"] == {
        "source": "volcengine",
        "severity": "critical",
        "rule_name": "对象存储桶状态码告警策略_自定义",
        "rule_id": "rule-1",
        "account_id": "2101986858",
        "project": "common-infra",
        "cloud_project": "default",
        "product_namespace": "VCM_TOS",
        "sub_namespace": "bucket",
        "region": "cn-shanghai",
        "resource_name": "common-prod-object",
        "resource_id": "common-prod-object",
        "metric_name": "4xxQPS",
        "metric_description": "4xx状态码QPS",
        "current_value": 39.06,
        "threshold": 0,
        "unit": "Count/s",
        "trigger_condition": "桶状态码/4xx状态码QPS > 0",
    }
    assert context["resources"][0]["dimensions"]["ResourceID"] == "common-prod-object"
    assert context["metrics"][0]["name"] == "4xxQPS"


def test_build_alert_identity_context_extracts_prometheus_labels() -> None:
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "fp-1",
                "labels": {
                    "alertname": "pod-restart",
                    "internal_label_alert_id": "alert-1",
                    "internal_label_alert_level": "P1",
                    "namespace": "eve-cn-prod",
                    "service": "chat-backend",
                    "pod": "chat-backend-123",
                    "container": "chat-backend",
                    "cluster": "ccv-prod",
                    "__name__": "kube_pod_container_status_restarts_total",
                },
                "annotations": {"summary": "chat-backend pod restarted"},
            }
        ],
        "commonLabels": {"namespace": "eve-cn-prod"},
    }

    context = build_alert_identity_context("prometheus", payload)

    assert context["identity"]["source"] == "prometheus"
    assert context["identity"]["status"] == "firing"
    assert context["identity"]["severity"] == "P1"
    assert context["identity"]["rule_name"] == "pod-restart"
    assert context["identity"]["rule_id"] == "alert-1"
    assert context["identity"]["namespace"] == "eve-cn-prod"
    assert context["identity"]["service"] == "chat-backend"
    assert context["identity"]["resource_name"] == "chat-backend-123"
    assert context["identity"]["cluster"] == "ccv-prod"
    assert context["identity"]["container"] == "chat-backend"
    assert context["identity"]["metric_name"] == "kube_pod_container_status_restarts_total"
    assert context["identity"]["fingerprint"] == "fp-1"
