import logging
from datetime import datetime

import pytest

from models.webhook import WebhookEvent
from services.webhooks.command_service import SaveWebhookResult, _resolve_request_id, save_webhook_data


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
        is_duplicate=True,
        duplicate_of=1,
        duplicate_count=2,
        beyond_window=True,
        last_notified_at=datetime.now(),
    )
    assert event.beyond_window is True


def test_fill_fields_warns_when_unknown_fields_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    event = WebhookEvent()

    with caplog.at_level(logging.WARNING, logger="models.webhook"):
        event.fill_fields(source="test", duplicate_from=1)

    assert "duplicate_from" in caplog.text
    assert not hasattr(event, "duplicate_from")


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


@pytest.mark.asyncio
async def test_resolve_request_id_returns_completed_result_without_resave() -> None:
    event = WebhookEvent()
    event.id = 42
    event.fill_fields(
        source="test",
        request_id="req-1",
        processing_status="completed",
        is_duplicate=True,
        duplicate_of=7,
        beyond_window=True,
    )

    class _Result:
        def scalar_one_or_none(self) -> WebhookEvent:
            return event

    class _Session:
        async def execute(self, stmt: object) -> _Result:
            return _Result()

    resolved = await _resolve_request_id(
        _Session(),  # type: ignore[arg-type]
        request_id="req-1",
        event_id=None,
        skip_duplicate_lookup=False,
    )

    assert resolved.completed_result == SaveWebhookResult(42, True, 7, True)
    assert resolved.event_id == 42
    assert resolved.skip_duplicate_lookup is True
