def test_openclaw_prompt_payload_keeps_full_payload_when_payload_is_large() -> None:
    from services.forwarding.forward import _build_openclaw_prompt_payload

    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "4fe6502e-587e-43a1-860f-bb575ab8476b",
                    "internal_label_alert_id": "6a0142e48f78951ec14b1fa4",
                    "internal_label_namespace": "eve-cn-prod",
                    "internal_label_service": "ai-router",
                },
                "annotations": {"summary": "OpenRouter success rate is 0%"},
                "startsAt": "2026-05-11T02:51:00Z",
            }
        ],
        "raw_debug_blob": "x" * 100_000,
    }

    result = _build_openclaw_prompt_payload("prometheus", payload)

    assert result["overview"]["labels"]["internal_label_service"] == "ai-router"
    assert result["overview"]["annotations"]["summary"] == "OpenRouter success rate is 0%"
    assert result["payload"]["raw_debug_blob"] == "x" * 100_000
    assert "payload_note" not in result
