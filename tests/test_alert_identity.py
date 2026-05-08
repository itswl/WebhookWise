from adapters.normalized import AlertIdentity, with_alert_identity
from models.webhook import WebhookEvent


def test_generate_hash_uses_adapter_identity_not_payload_noise() -> None:
    first = with_alert_identity(
        {"value": 95, "description": "cpu at 95"},
        AlertIdentity(source="prometheus", name="HighCPU", resource="node-01", severity="critical"),
    )
    second = with_alert_identity(
        {"value": 99, "description": "cpu at 99", "annotations": {"runbook": "changed"}},
        AlertIdentity(source="prometheus", name="highcpu", resource="NODE-01", severity="Critical"),
    )

    assert WebhookEvent.generate_hash(first, "prometheus") == WebhookEvent.generate_hash(second, "prometheus")


def test_generate_hash_falls_back_to_payload_when_identity_missing() -> None:
    first = WebhookEvent.generate_hash({"value": 95}, "custom")
    second = WebhookEvent.generate_hash({"value": 99}, "custom")

    assert first != second
