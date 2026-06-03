from adapters.normalized import AlertIdentity, with_alert_identity
from services.dedup import generate_alert_hash, generate_event_keys


def test_generate_hash_uses_adapter_identity_not_payload_noise() -> None:
    first = with_alert_identity(
        {"value": 95, "description": "cpu at 95"},
        AlertIdentity(source="prometheus", name="HighCPU", resource="node-01", severity="critical"),
    )
    second = with_alert_identity(
        {"value": 99, "description": "cpu at 99", "annotations": {"runbook": "changed"}},
        AlertIdentity(source="prometheus", name="highcpu", resource="NODE-01", severity="Critical"),
    )

    assert generate_alert_hash(first, "prometheus") == generate_alert_hash(second, "prometheus")


def test_generate_hash_falls_back_to_payload_when_identity_missing() -> None:
    first = generate_alert_hash({"value": 95}, "custom")
    second = generate_alert_hash({"value": 99}, "custom")

    assert first != second


def _gpu_payload(*, gpu_used: float, gpu_memory: float) -> dict[str, object]:
    return with_alert_identity(
        {
            "RuleName": "云服务器GPU卡告警",
            "SubNamespace": "GPU",
            "Resources": [
                {
                    "InstanceId": "i-gpu-01",
                    "Metrics": [
                        {"Name": "GpuUsedUtilization", "CurrentValue": gpu_used, "Threshold": 80},
                        {"Name": "GpuMemoryUsedUtilization", "CurrentValue": gpu_memory, "Threshold": 90},
                    ],
                }
            ],
        },
        AlertIdentity(source="volcengine", name="云服务器GPU卡告警", resource="i-gpu-01", severity="warning"),
    )


def test_generate_event_keys_splits_resource_risk_buckets_for_reanalysis() -> None:
    warning_hash, warning_dedup = generate_event_keys(_gpu_payload(gpu_used=0, gpu_memory=90.5), "volcengine")
    high_hash, high_dedup = generate_event_keys(_gpu_payload(gpu_used=100, gpu_memory=90.5), "volcengine")

    assert warning_hash == high_hash
    assert warning_dedup != high_dedup
