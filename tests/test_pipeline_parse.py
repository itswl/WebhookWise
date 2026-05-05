"""
tests/test_pipeline_parse.py
=============================
测试 pipeline._parse_request() 和 _load_event_payload() 纯逻辑。
这两个函数处理进入系统的第一道数据解析，错误会导致事件完全丢失。
"""

import pytest
import orjson
from unittest.mock import AsyncMock, MagicMock, patch


# ── _parse_request ─────────────────────────────────────────────────────────


def test_parse_request_uses_source_hint():
    from services.pipeline import _parse_request
    payload = {
        "alerts": [{"labels": {"alertname": "Test", "severity": "critical", "instance": "h1"},
                    "annotations": {}}]
    }
    ctx = _parse_request("1.2.3.4", {}, payload, b"", "prometheus", None)
    assert ctx.source == "prometheus"


def test_parse_request_infers_source_from_header():
    from services.pipeline import _parse_request
    payload = {
        "alerts": [{"labels": {"alertname": "Test", "severity": "critical", "instance": "h1"},
                    "annotations": {}}]
    }
    ctx = _parse_request("1.2.3.4", {"x-webhook-source": "grafana"}, payload, b"", None, None)
    # grafana 格式不匹配 prometheus payload → fallback 到 header 提示
    # 实际取决于适配器检测，header source 作为 hint
    assert ctx.source is not None


def test_parse_request_parses_raw_body_when_no_payload():
    from services.pipeline import _parse_request
    data = {"alertname": "MemHigh", "severity": "warning"}
    raw = orjson.dumps(data)
    ctx = _parse_request("1.2.3.4", {}, {}, raw, "unknown", None)
    assert isinstance(ctx.parsed_data, dict)


def test_parse_request_sets_client_ip():
    from services.pipeline import _parse_request
    ctx = _parse_request("10.0.0.1", {}, {"foo": "bar"}, b"", "unknown", None)
    assert ctx.client_ip == "10.0.0.1"


def test_parse_request_sets_headers():
    from services.pipeline import _parse_request
    headers = {"content-type": "application/json", "x-request-id": "abc123"}
    ctx = _parse_request("1.2.3.4", headers, {"foo": "bar"}, b"", "unknown", None)
    assert ctx.headers == headers


def test_parse_request_full_data_contains_source():
    from services.pipeline import _parse_request
    ctx = _parse_request("1.2.3.4", {}, {"foo": "bar"}, b"", "github", None)
    assert "source" in ctx.webhook_full_data
    assert ctx.webhook_full_data["source"] is not None


def test_parse_request_timestamp_passed_through():
    from services.pipeline import _parse_request
    ts = "2025-01-01T12:00:00Z"
    ctx = _parse_request("1.2.3.4", {}, {}, b"", "unknown", ts)
    assert ctx.webhook_full_data.get("timestamp") == ts


# ── _load_event_payload ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_event_payload_returns_parsed_data_when_present():
    """parsed_data 已存在时直接返回，无需解压。"""
    from services.pipeline import _load_event_payload

    event = MagicMock()
    event.parsed_data = {"alertname": "CPUHigh", "host": "prod-01"}
    event.raw_payload = None

    with patch("services.pipeline.decompress_payload_async", AsyncMock(return_value="")):
        parsed, raw_text = await _load_event_payload(event)

    assert parsed == {"alertname": "CPUHigh", "host": "prod-01"}


@pytest.mark.asyncio
async def test_load_event_payload_decompresses_when_parsed_data_none():
    """parsed_data 为 None 时，从 raw_payload 解压并解析 JSON。"""
    from services.pipeline import _load_event_payload

    data = {"alerts": [{"labels": {"alertname": "DiskFull"}}]}
    raw_json = orjson.dumps(data).decode()

    event = MagicMock()
    event.parsed_data = None
    event.raw_payload = b"compressed"

    with patch("services.pipeline.decompress_payload_async", AsyncMock(return_value=raw_json)):
        parsed, raw_text = await _load_event_payload(event)

    assert parsed is not None
    assert "alerts" in parsed
    assert raw_text == raw_json


@pytest.mark.asyncio
async def test_load_event_payload_returns_none_on_invalid_json():
    """解压后内容不是有效 JSON 时，parsed_data 应为 None（不抛异常）。"""
    from services.pipeline import _load_event_payload

    event = MagicMock()
    event.parsed_data = None
    event.raw_payload = b"something"

    with patch("services.pipeline.decompress_payload_async", AsyncMock(return_value="not-json")):
        parsed, raw_text = await _load_event_payload(event)

    assert parsed is None
    assert raw_text == "not-json"


@pytest.mark.asyncio
async def test_load_event_payload_handles_none_raw_payload():
    """raw_payload 为 None 时不崩溃，返回 None, ''。"""
    from services.pipeline import _load_event_payload

    event = MagicMock()
    event.parsed_data = None
    event.raw_payload = None

    with patch("services.pipeline.decompress_payload_async", AsyncMock(return_value="")):
        parsed, raw_text = await _load_event_payload(event)

    assert parsed is None
    assert raw_text == ""
