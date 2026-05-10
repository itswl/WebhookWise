"""
tests/test_orchestrator.py
==========================
测试 webhook command/query services 的纯函数逻辑：
- get_client_ip()：IP 提取
- _resolve_analysis_for_duplicate()：重复告警的分析结果解析
- _row_to_summary_dict()：行数据序列化
"""

from datetime import datetime
from unittest.mock import MagicMock

# ── get_client_ip ─────────────────────────────────────────────────────────────


def _make_request(headers: dict, client_host: str | None = "127.0.0.1"):
    """构造最小 Request mock。"""
    req = MagicMock()
    req.headers = headers
    req.client = MagicMock()
    req.client.host = client_host
    return req


def test_get_client_ip_from_x_forwarded_for():
    from services.webhooks.command_service import get_client_ip

    req = _make_request({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
    assert get_client_ip(req) == "1.2.3.4"


def test_get_client_ip_strips_whitespace():
    from services.webhooks.command_service import get_client_ip

    req = _make_request({"x-forwarded-for": "  5.6.7.8 , 192.168.1.1"})
    assert get_client_ip(req) == "5.6.7.8"


def test_get_client_ip_from_x_real_ip():
    from services.webhooks.command_service import get_client_ip

    req = _make_request({"x-real-ip": "9.10.11.12"})
    assert get_client_ip(req) == "9.10.11.12"


def test_get_client_ip_prefers_x_forwarded_for_over_x_real_ip():
    from services.webhooks.command_service import get_client_ip

    req = _make_request({"x-forwarded-for": "1.1.1.1", "x-real-ip": "2.2.2.2"})
    assert get_client_ip(req) == "1.1.1.1"


def test_get_client_ip_falls_back_to_client_host():
    from services.webhooks.command_service import get_client_ip

    req = _make_request({}, client_host="192.168.0.5")
    assert get_client_ip(req) == "192.168.0.5"


def test_get_client_ip_no_client_returns_unknown():
    from services.webhooks.command_service import get_client_ip

    req = MagicMock()
    req.headers = {}
    req.client = None
    assert get_client_ip(req) == "unknown"


# ── _resolve_analysis_for_duplicate ──────────────────────────────────────────


def _make_original(ai_analysis=None, importance=None):
    evt = MagicMock()
    evt.id = 1
    evt.ai_analysis = ai_analysis
    evt.importance = importance
    return evt


def test_resolve_analysis_uses_provided_ai_analysis():
    from services.webhooks.command_service import _resolve_analysis_for_duplicate

    ai = {"summary": "High CPU", "importance": "high"}
    original = _make_original(ai_analysis=None)
    result, importance = _resolve_analysis_for_duplicate(ai, original, reanalyzed=False)
    assert result == ai
    assert importance == "high"


def test_resolve_analysis_falls_back_to_original_if_no_new_analysis():
    from services.webhooks.command_service import _resolve_analysis_for_duplicate

    original = _make_original(ai_analysis={"summary": "Disk low", "importance": "medium"}, importance="medium")
    result, importance = _resolve_analysis_for_duplicate(None, original, reanalyzed=False)
    assert result["summary"] == "Disk low"
    assert importance == "medium"


def test_resolve_analysis_returns_empty_if_both_missing():
    from services.webhooks.command_service import _resolve_analysis_for_duplicate

    original = _make_original(ai_analysis=None, importance=None)
    result, importance = _resolve_analysis_for_duplicate(None, original, reanalyzed=False)
    assert result == {}
    assert importance is None


def test_resolve_analysis_updates_original_when_reanalyzed_and_original_missing():
    """重新分析后，若原始告警无分析，应更新原始告警的 ai_analysis。"""
    from services.webhooks.command_service import _resolve_analysis_for_duplicate

    ai = {"summary": "Root cause found", "importance": "high"}
    original = _make_original(ai_analysis=None, importance=None)
    _resolve_analysis_for_duplicate(ai, original, reanalyzed=True)
    assert original.ai_analysis == ai
    assert original.importance == "high"


def test_resolve_analysis_does_not_overwrite_original_when_not_reanalyzed():
    """非重分析时，不应修改原始告警的 ai_analysis。"""
    from services.webhooks.command_service import _resolve_analysis_for_duplicate

    ai = {"summary": "New analysis", "importance": "medium"}
    original = _make_original(ai_analysis={"summary": "Original"}, importance="low")
    _resolve_analysis_for_duplicate(ai, original, reanalyzed=False)
    # 原始的 ai_analysis 不应被覆盖（reanalyzed=False 且原始已有分析）
    assert original.ai_analysis["summary"] == "Original"


# ── _row_to_summary_dict ──────────────────────────────────────────────────────


def _make_row(
    id=1,
    source="prometheus",
    client_ip="1.2.3.4",
    timestamp=None,
    importance="high",
    is_duplicate=False,
    duplicate_of=None,
    duplicate_count=0,
    beyond_window=False,
    forward_status="success",
    ai_analysis=None,
    parsed_data=None,
    created_at=None,
    prev_alert_id=None,
):
    row = MagicMock()
    row.id = id
    row.source = source
    row.client_ip = client_ip
    row.timestamp = timestamp or datetime(2025, 1, 1, 12, 0, 0)
    row.importance = importance
    row.is_duplicate = is_duplicate
    row.duplicate_of = duplicate_of
    row.duplicate_count = duplicate_count
    row.beyond_window = beyond_window
    row.forward_status = forward_status
    row.ai_analysis = ai_analysis
    row.parsed_data = parsed_data or {}
    row.created_at = created_at or datetime(2025, 1, 1, 11, 59, 0)
    row.prev_alert_id = prev_alert_id
    # prev_alert_timestamp 是子查询列
    row.prev_alert_timestamp = None
    return row


def test_row_to_summary_dict_basic_fields():
    from services.webhooks.query_service import _row_to_summary_dict

    row = _make_row()
    d = _row_to_summary_dict(row)
    assert d["id"] == 1
    assert d["source"] == "prometheus"
    assert d["importance"] == "high"
    assert d["is_duplicate"] is False
    assert d["beyond_window"] is False
    assert d["duplicate_type"] == "new"


def test_row_to_summary_dict_timestamps_are_isoformat():
    from services.webhooks.query_service import _row_to_summary_dict

    row = _make_row()
    d = _row_to_summary_dict(row)
    # timestamp 和 created_at 应为 ISO 格式字符串（可被 JSON 序列化）
    assert isinstance(d["timestamp"], str)
    assert "T" in d["timestamp"]
    assert isinstance(d["created_at"], str)


def test_row_to_summary_dict_duplicate_within_window():
    from services.webhooks.query_service import _row_to_summary_dict

    row = _make_row(is_duplicate=True, duplicate_of=5, beyond_window=False)
    d = _row_to_summary_dict(row)
    assert d["is_duplicate"] is True
    assert d["beyond_window"] is False
    assert d["duplicate_type"] == "within_window"
    assert d["is_within_window"] is True


def test_row_to_summary_dict_duplicate_beyond_window():
    from services.webhooks.query_service import _row_to_summary_dict

    row = _make_row(is_duplicate=True, duplicate_of=5, beyond_window=True)
    d = _row_to_summary_dict(row)
    assert d["is_duplicate"] is True
    assert d["beyond_window"] is True
    assert d["duplicate_type"] == "beyond_window"
    assert d["is_within_window"] is False


def test_row_to_summary_dict_summary_from_ai_analysis():
    from services.webhooks.query_service import _row_to_summary_dict

    row = _make_row(ai_analysis={"summary": "High memory usage detected"})
    d = _row_to_summary_dict(row)
    assert d["summary"] == "High memory usage detected"


def test_row_to_summary_dict_summary_none_when_no_ai_analysis():
    from services.webhooks.query_service import _row_to_summary_dict

    row = _make_row(ai_analysis=None)
    d = _row_to_summary_dict(row)
    assert d["summary"] is None


def test_analyze_with_rules_high_by_level_keyword():
    from services.analysis.ai_analyzer import analyze_with_rules

    res = analyze_with_rules({"RuleName": "HighCPUUsage", "Level": "critical"}, "prometheus")
    assert res["importance"] == "high"


def test_analyze_with_rules_medium_by_warning_level():
    from services.analysis.ai_analyzer import analyze_with_rules

    res = analyze_with_rules({"RuleName": "LatencyIncreased", "Level": "warning"}, "cloud-monitor")
    assert res["importance"] == "medium"


def test_analyze_with_rules_metric_threshold_promotes_to_high():
    from services.analysis.ai_analyzer import analyze_with_rules

    res = analyze_with_rules({"RuleName": "5xxQPSHigh", "CurrentValue": 500, "Threshold": 100}, "cloud-monitor")
    assert res["importance"] == "high"


def test_analyze_with_rules_records_non_numeric_metric_value(monkeypatch):
    from services.analysis import ai_analyzer

    class _Counter:
        calls: list[dict[str, str]]

        def __init__(self) -> None:
            self.calls = []

        def labels(self, **labels: str) -> "_Counter":
            self.calls.append(labels)
            return self

        def inc(self) -> None:
            pass

    counter = _Counter()
    monkeypatch.setattr(ai_analyzer, "ALERT_NUMERIC_PARSE_FAILURE_TOTAL", counter)

    res = ai_analyzer.analyze_with_rules(
        {"RuleName": "MemoryUsage", "CurrentValue": "500MB", "Threshold": 100}, "prometheus"
    )

    assert res["importance"] == "medium"
    assert counter.calls == [{"source": "prometheus", "field": "current", "reason": "non_numeric"}]


def test_analyze_with_rules_records_non_finite_metric_value(monkeypatch):
    from services.analysis import ai_analyzer

    class _Counter:
        calls: list[dict[str, str]]

        def __init__(self) -> None:
            self.calls = []

        def labels(self, **labels: str) -> "_Counter":
            self.calls.append(labels)
            return self

        def inc(self) -> None:
            pass

    counter = _Counter()
    monkeypatch.setattr(ai_analyzer, "ALERT_NUMERIC_PARSE_FAILURE_TOTAL", counter)

    res = ai_analyzer.analyze_with_rules(
        {"RuleName": "MemoryUsage", "CurrentValue": "NaN", "Threshold": 100}, "prometheus"
    )

    assert res["summary"] == "🟠 MemoryUsage"
    assert counter.calls == [{"source": "prometheus", "field": "current", "reason": "non_finite"}]
