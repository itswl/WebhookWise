from datetime import datetime

from crud.webhook import SaveWebhookResult, _build_event


def test_build_event_beyond_window_flag_can_be_persisted():
    event = _build_event(
        source="test",
        client_ip="127.0.0.1",
        raw_payload=b"{}",
        headers={},
        data={},
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


def test_save_result_carries_beyond_window_flag():
    result = SaveWebhookResult(1, True, 10, True)
    assert result.beyond_window is True
