import pytest

from core.datetime_utils import utcnow
from models.webhook import WebhookEvent
from services.webhooks.command_service import SaveWebhookResult, _resolve_request_id, save_webhook_data


def test_build_event_fill_fields_works():
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
        last_notified_at=utcnow(),
    )
    assert event.is_duplicate is True


def test_fill_fields_rejects_unknown_fields_at_call_boundary() -> None:
    event = WebhookEvent()

    with pytest.raises(TypeError, match="duplicate_from"):
        event.fill_fields(source="test", duplicate_from=1)


def test_save_result_uses_database_event_ids_only():
    result = SaveWebhookResult(1, True, 10)
    assert result.webhook_id == 1
    assert result.original_id == 10


@pytest.mark.asyncio
async def test_save_webhook_data_propagates_database_failures(monkeypatch):
    class FailingSessionScope:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("services.webhooks.command_service.session_scope", FailingSessionScope)

    with pytest.raises(RuntimeError, match="database unavailable"):
        from services.webhooks.command_service import SaveWebhookInput

        await save_webhook_data(input=SaveWebhookInput(data={"alert": "down"}, source="test", alert_hash="hash"))


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
        skip_duplicate_lookup=False,
    )

    assert resolved.completed_result == SaveWebhookResult(42, True, 7)
    assert resolved.existing_event_id == 42
    assert resolved.skip_duplicate_lookup is True
