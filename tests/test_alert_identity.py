from adapters.normalized import AlertIdentity, with_alert_identity
from services.webhooks.deduplication import generate_alert_hash


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
