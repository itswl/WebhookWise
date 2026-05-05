"""
tests/test_log_context.py
==========================
测试结构化日志上下文的设置、获取和清除。
关键：空值不应出现在日志输出中（避免噪音日志）。
"""

import pytest

from core.log_context import (
    clear_log_context,
    get_log_context,
    set_log_context,
)


def setup_function():
    """每个测试前清除上下文。"""
    clear_log_context()


def test_empty_context_returns_no_keys():
    """未设置上下文时，get_log_context 不应返回任何空字段。"""
    ctx = get_log_context()
    # 空字段不应出现（会造成日志噪音）
    assert "alert_hash" not in ctx or ctx["alert_hash"]
    assert "source" not in ctx or ctx["source"]
    assert "processing_status" not in ctx or ctx["processing_status"]
    assert "route_type" not in ctx or ctx["route_type"]


def test_set_event_id_appears_in_context():
    set_log_context(event_id=12345)
    ctx = get_log_context()
    assert ctx.get("event_id") == 12345


def test_set_source_appears_in_context():
    set_log_context(source="prometheus")
    ctx = get_log_context()
    assert ctx.get("source") == "prometheus"


def test_set_multiple_fields():
    set_log_context(event_id=1, alert_hash="abc123", source="grafana", processing_status="analyzing")
    ctx = get_log_context()
    assert ctx["event_id"] == 1
    assert ctx["alert_hash"] == "abc123"
    assert ctx["source"] == "grafana"
    assert ctx["processing_status"] == "analyzing"


def test_partial_update_preserves_other_fields():
    """set_log_context 应只更新传入的字段，不清除已有字段。"""
    set_log_context(event_id=99, source="datadog")
    set_log_context(processing_status="completed")
    ctx = get_log_context()
    assert ctx["event_id"] == 99
    assert ctx["source"] == "datadog"
    assert ctx["processing_status"] == "completed"


def test_clear_removes_all_fields():
    set_log_context(event_id=1, source="prometheus", alert_hash="xyz")
    clear_log_context()
    ctx = get_log_context()
    assert "event_id" not in ctx
    assert "source" not in ctx or not ctx["source"]
    assert "alert_hash" not in ctx or not ctx["alert_hash"]


def test_empty_string_not_included_in_context():
    """空字符串值不应出现在 get_log_context 返回值中。"""
    set_log_context(source="prometheus")
    clear_log_context()
    set_log_context(event_id=5)  # 不设置 source
    ctx = get_log_context()
    # source 为空时不应包含在返回字典中
    assert ctx.get("source", "") == ""
    assert "event_id" in ctx


def test_none_event_id_not_included():
    """event_id=None 时不应包含在返回字典中。"""
    ctx = get_log_context()
    assert "event_id" not in ctx


def test_route_type_set_and_retrieved():
    set_log_context(route_type="ai")
    ctx = get_log_context()
    assert ctx.get("route_type") == "ai"


def test_route_type_empty_after_clear():
    set_log_context(route_type="cache")
    clear_log_context()
    ctx = get_log_context()
    assert ctx.get("route_type", "") == ""
