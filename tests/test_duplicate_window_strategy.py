"""Unit tests for duplicate-window helper logic in utils.py."""

from datetime import datetime

from crud.webhook import DuplicateCheckResult, SaveWebhookResult, _resolve_window_start


class _FakeEvent:
    def __init__(self, event_id: int, timestamp: datetime):
        self.id = event_id
        self.timestamp = timestamp


def test_resolve_window_start_prefers_recent_beyond_window_event():
    original = _FakeEvent(100, datetime(2026, 3, 1, 10, 0, 0))
    recent_beyond = _FakeEvent(200, datetime(2026, 3, 2, 9, 0, 0))

    window_start, window_start_id = _resolve_window_start(original, recent_beyond)

    assert window_start == recent_beyond.timestamp
    assert window_start_id == recent_beyond.id


def test_resolve_window_start_falls_back_to_original_event():
    original = _FakeEvent(101, datetime(2026, 3, 1, 10, 0, 0))

    window_start, window_start_id = _resolve_window_start(original, None)

    assert window_start == original.timestamp
    assert window_start_id == original.id


def test_duplicate_check_result_dataclass_fields():
    result = DuplicateCheckResult(True, None, True, None)
    assert result.is_duplicate is True
    assert result.beyond_window is True


def test_save_webhook_result_dataclass_fields():
    result = SaveWebhookResult(123, True, 1, False)
    assert result.webhook_id == 123
    assert result.original_id == 1
