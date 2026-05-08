from datetime import datetime

import pytest

from models.webhook import WebhookEvent
from services.webhooks.command_service import SaveWebhookResult, save_webhook_data


def test_build_event_beyond_window_flag_can_be_persisted():
    event = WebhookEvent()
    event.fill_fields(
        source="test",
        client_ip="127.0.0.1",
        raw_payload=b"{}",
        headers={},
        parsed_data={},
        alert_hash="abc",
        ai_analysis={"importance": "high"},
        importance="high",
        forward_status="pending",
        is_duplicate=1,
        duplicate_of=1,
        duplicate_count=2,
        beyond_window=1,
        last_notified_at=datetime.now(),
    )
    assert event.beyond_window == 1


def test_save_result_uses_database_event_ids_only():
    result = SaveWebhookResult(1, True, 10, True)
    assert result.webhook_id == 1
    assert result.beyond_window is True


@pytest.mark.asyncio
async def test_save_webhook_data_propagates_database_failures(monkeypatch):
    class FailingSessionScope:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("services.webhooks.command_service.session_scope", FailingSessionScope)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await save_webhook_data({"alert": "down"}, source="test", alert_hash="hash")
