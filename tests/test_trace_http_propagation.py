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
