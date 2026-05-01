import pytest

pytest.importorskip("httpx")


@pytest.mark.asyncio
async def test_http_client_injects_trace_headers(monkeypatch):
    import httpx

    from core.http_client import _build_async_client
    from core.trace import set_trace_id

    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["x_request_id"] = request.headers.get("X-Request-Id")
        captured["traceparent"] = request.headers.get("traceparent")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = _build_async_client(transport=transport)
    try:
        set_trace_id("evt-123")
        r = await client.get("https://example.com/test")
        assert r.status_code == 200
    finally:
        await client.aclose()

    assert captured["x_request_id"] == "evt-123"
    assert captured["traceparent"] and captured["traceparent"].startswith("00-")


def test_extract_trace_id_from_headers_prefers_x_request_id():
    from core.trace import extract_trace_id_from_headers

    tid = extract_trace_id_from_headers({"x-request-id": "evt-999"})
    assert tid == "evt-999"


def test_extract_trace_id_from_headers_parses_traceparent():
    from core.trace import extract_trace_id_from_headers

    tid = extract_trace_id_from_headers({"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    assert tid == "4bf92f3577b3"
