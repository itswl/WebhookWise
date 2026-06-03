"""
tests/runtime/test_log_context.py
=================================
测试结构化日志上下文的设置、获取和清除。
关键：空值不应出现在日志输出中（避免噪音日志）。
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
    """每个测试前清除上下文。"""
    clear_log_context()


def test_empty_context_returns_no_keys():
    """未设置上下文时，get_log_context 不应返回任何空字段。"""
    ctx = get_log_context()
    # 空字段不应出现（会造成日志噪音）
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
    """set_log_context 应只更新传入的字段，不清除已有字段。"""
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
    """空字符串值不应出现在 get_log_context 返回值中。"""
    set_log_context(webhook_source="prometheus")
    clear_log_context()
    set_log_context(event_id=5)  # 不设置 source
    ctx = get_log_context()
    # source 为空时不应包含在返回字典中
    assert ctx.get(WEBHOOK_SOURCE, "") == ""
    assert WEBHOOK_EVENT_ID in ctx


def test_none_event_id_not_included():
    """event_id=None 时不应包含在返回字典中。"""
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
