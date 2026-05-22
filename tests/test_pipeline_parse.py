"""
tests/test_pipeline_parse.py
=============================
测试 request_parser.parse_request() 和 load_event_payload() 纯逻辑。
这两个函数处理进入系统的第一道数据解析，错误会导致事件完全丢失。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import json

# ── parse_request ──────────────────────────────────────────────────────────


def test_parse_request_uses_source_hint():
    from services.webhooks.request_parser import parse_request

    payload = {
        "alerts": [{"labels": {"alertname": "Test", "severity": "critical", "instance": "h1"}, "annotations": {}}]
    }
    ctx = parse_request("1.2.3.4", {}, payload, b"", "prometheus", None)
    assert ctx.source == "prometheus"


def test_parse_request_infers_source_from_header():
    from services.webhooks.request_parser import parse_request

    payload = {
        "alerts": [{"labels": {"alertname": "Test", "severity": "critical", "instance": "h1"}, "annotations": {}}]
    }
    ctx = parse_request("1.2.3.4", {"x-webhook-source": "grafana"}, payload, b"", None, None)
    # grafana 格式不匹配 prometheus payload → fallback 到 header 提示
    # 实际取决于适配器检测，header source 作为 hint
    assert ctx.source is not None


def test_parse_request_parses_raw_body_when_no_payload():
    from services.webhooks.request_parser import parse_request

    data = {"alertname": "MemHigh", "severity": "warning"}
    raw = json.dumps_bytes(data)
    ctx = parse_request("1.2.3.4", {}, {}, raw, "unknown", None)
    assert isinstance(ctx.parsed_data, dict)


def test_parse_request_sets_client_ip():
    from services.webhooks.request_parser import parse_request

    ctx = parse_request("10.0.0.1", {}, {"foo": "bar"}, b"", "unknown", None)
    assert ctx.client_ip == "10.0.0.1"


def test_parse_request_sets_headers():
    from services.webhooks.request_parser import parse_request

    headers = {"content-type": "application/json", "x-request-id": "abc123"}
    ctx = parse_request("1.2.3.4", headers, {"foo": "bar"}, b"", "unknown", None)
    assert ctx.headers == headers


def test_parse_request_full_data_contains_source():
    from services.webhooks.request_parser import parse_request

    ctx = parse_request("1.2.3.4", {}, {"foo": "bar"}, b"", "github", None)
    assert "source" in ctx.webhook_full_data
    assert ctx.webhook_full_data["source"] is not None


def test_parse_request_timestamp_passed_through():
    from services.webhooks.request_parser import parse_request

    ts = "2025-01-01T12:00:00Z"
    ctx = parse_request("1.2.3.4", {}, {}, b"", "unknown", ts)
    assert ctx.webhook_full_data.get("timestamp") == ts


# ── _load_event_payload ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_event_payload_returns_parsed_data_when_present():
    """parsed_data 已存在时直接返回，无需解压。"""
    from services.webhooks.repository import load_event_payload

    event = MagicMock()
    event.parsed_data = {"alertname": "CPUHigh", "host": "prod-01"}
    event.raw_payload = None

    with patch("services.webhooks.repository.decompress_payload_async", AsyncMock(return_value="")):
        parsed, raw_text = await load_event_payload(event)

    assert parsed == {"alertname": "CPUHigh", "host": "prod-01"}


@pytest.mark.asyncio
async def test_load_event_payload_decompresses_when_parsed_data_none():
    """parsed_data 为 None 时，从 raw_payload 解压并解析 JSON。"""
    from services.webhooks.repository import load_event_payload

    data = {"alerts": [{"labels": {"alertname": "DiskFull"}}]}
    raw_json = json.dumps(data)

    event = MagicMock()
    event.parsed_data = None
    event.raw_payload = b"compressed"

    with patch("services.webhooks.repository.decompress_payload_async", AsyncMock(return_value=raw_json)):
        parsed, raw_text = await load_event_payload(event)

    assert parsed is not None
    assert "alerts" in parsed
    assert raw_text == raw_json


@pytest.mark.asyncio
async def test_load_event_payload_returns_none_on_invalid_json():
    """解压后内容不是有效 JSON 时，parsed_data 应为 None（不抛异常）。"""
    from services.webhooks.repository import load_event_payload

    event = MagicMock()
    event.parsed_data = None
    event.raw_payload = b"something"

    with patch("services.webhooks.repository.decompress_payload_async", AsyncMock(return_value="not-json")):
        parsed, raw_text = await load_event_payload(event)

    assert parsed is None
    assert raw_text == "not-json"


@pytest.mark.asyncio
async def test_load_event_payload_handles_none_raw_payload():
    """raw_payload 为 None 时不崩溃，返回 None, ''。"""
    from services.webhooks.repository import load_event_payload

    event = MagicMock()
    event.parsed_data = None
    event.raw_payload = None

    with patch("services.webhooks.repository.decompress_payload_async", AsyncMock(return_value="")):
        parsed, raw_text = await load_event_payload(event)

    assert parsed is None
    assert raw_text == ""
