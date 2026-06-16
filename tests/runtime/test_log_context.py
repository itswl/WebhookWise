"""
tests/runtime/test_log_context.py
=================================
Tests setting, getting, and clearing the structured logging context.
Key point: empty values must not appear in log output (to avoid noisy logs).
"""

from core.log_context import (
    clear_log_context,
    get_log_context,
    set_log_context,
)
from core.observability.attributes import (
    REQUEST_ID,
    WEBHOOK_ALERT_HASH,
    WEBHOOK_EVENT_ID,
    WEBHOOK_ROUTE,
    WEBHOOK_SOURCE,
    WEBHOOK_STATUS,
)


def setup_function():
    """Clear the context before each test."""
    clear_log_context()


def test_empty_context_returns_no_keys():
    """When no context is set, get_log_context must not return any empty fields."""
    ctx = get_log_context()
    # Empty fields must not appear (they would create log noise)
    assert WEBHOOK_ALERT_HASH not in ctx or ctx[WEBHOOK_ALERT_HASH]
    assert WEBHOOK_SOURCE not in ctx or ctx[WEBHOOK_SOURCE]
    assert WEBHOOK_STATUS not in ctx or ctx[WEBHOOK_STATUS]
    assert WEBHOOK_ROUTE not in ctx or ctx[WEBHOOK_ROUTE]


def test_set_event_id_appears_in_context():
    set_log_context(event_id=12345)
    ctx = get_log_context()
    assert ctx.get(WEBHOOK_EVENT_ID) == 12345


def test_set_request_id_appears_in_context():
    set_log_context(request_id="req-123")
    ctx = get_log_context()
    assert ctx.get(REQUEST_ID) == "req-123"


def test_set_source_appears_in_context():
    set_log_context(webhook_source="prometheus")
    ctx = get_log_context()
    assert ctx.get(WEBHOOK_SOURCE) == "prometheus"


def test_set_multiple_fields():
    set_log_context(
        event_id=1,
        request_id="req-abc",
        alert_hash="abc123",
        webhook_source="grafana",
        webhook_status="analyzing",
    )
    ctx = get_log_context()
    assert ctx[WEBHOOK_EVENT_ID] == 1
    assert ctx[REQUEST_ID] == "req-abc"
    assert ctx[WEBHOOK_ALERT_HASH] == "abc123"
    assert ctx[WEBHOOK_SOURCE] == "grafana"
    assert ctx[WEBHOOK_STATUS] == "analyzing"


def test_partial_update_preserves_other_fields():
    """set_log_context should only update the fields passed in, not clear existing ones."""
    set_log_context(event_id=99, webhook_source="datadog")
    set_log_context(webhook_status="completed")
    ctx = get_log_context()
    assert ctx[WEBHOOK_EVENT_ID] == 99
    assert ctx[WEBHOOK_SOURCE] == "datadog"
    assert ctx[WEBHOOK_STATUS] == "completed"


def test_clear_removes_all_fields():
    set_log_context(event_id=1, webhook_source="prometheus", alert_hash="xyz")
    clear_log_context()
    ctx = get_log_context()
    assert WEBHOOK_EVENT_ID not in ctx
    assert REQUEST_ID not in ctx
    assert WEBHOOK_SOURCE not in ctx or not ctx[WEBHOOK_SOURCE]
    assert WEBHOOK_ALERT_HASH not in ctx or not ctx[WEBHOOK_ALERT_HASH]


def test_empty_string_not_included_in_context():
    """Empty-string values must not appear in the value returned by get_log_context."""
    set_log_context(webhook_source="prometheus")
    clear_log_context()
    set_log_context(event_id=5)  # do not set source
    ctx = get_log_context()
    # When source is empty it must not be included in the returned dict
    assert ctx.get(WEBHOOK_SOURCE, "") == ""
    assert WEBHOOK_EVENT_ID in ctx


def test_none_event_id_not_included():
    """When event_id is None it must not be included in the returned dict."""
    ctx = get_log_context()
    assert WEBHOOK_EVENT_ID not in ctx


def test_route_type_set_and_retrieved():
    set_log_context(webhook_route="ai")
    ctx = get_log_context()
    assert ctx.get(WEBHOOK_ROUTE) == "ai"


def test_route_type_empty_after_clear():
    set_log_context(webhook_route="cache")
    clear_log_context()
    ctx = get_log_context()
    assert ctx.get(WEBHOOK_ROUTE, "") == ""
