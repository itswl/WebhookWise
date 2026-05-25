import pytest

pytest.importorskip("httpx")


@pytest.mark.asyncio
async def test_http_client_injects_trace_headers(monkeypatch):
    import httpx

    from core.http_client import build_http_client
    from core.observability.tracing import reset_fallback_trace_id, set_fallback_trace_id

    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["x_request_id"] = request.headers.get("X-Request-Id")
        captured["traceparent"] = request.headers.get("traceparent")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = build_http_client(transport=transport)
    try:
        token = set_fallback_trace_id("4bf92f3577b34da6a3ce929d0e0e4736")
        r = await client.get("https://example.com/test")
        assert r.status_code == 200
    finally:
        reset_fallback_trace_id(token)
        await client.aclose()

    assert captured["x_request_id"] and len(captured["x_request_id"]) == 32
    assert captured["traceparent"] and captured["traceparent"].startswith("00-")


@pytest.mark.asyncio
async def test_http_client_ignores_proxy_environment(monkeypatch):
    from core.http_client import build_http_client

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("NO_PROXY", "fd00:b51a:cc66:f0::/64")

    client = build_http_client()
    try:
        assert client.trust_env is False
    finally:
        await client.aclose()


def test_trace_context_keeps_request_id_separate_from_trace_id():
    from core.observability.tracing import extract_request_id_from_headers, extract_trace_id_from_headers

    headers = {"x-request-id": "evt-999"}
    assert extract_trace_id_from_headers(headers) == ""
    assert extract_request_id_from_headers(headers) == "evt-999"


def test_extract_trace_id_from_headers_parses_traceparent():
    from core.observability.tracing import extract_trace_id_from_headers

    tid = extract_trace_id_from_headers({"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    assert tid == "4bf92f3577b34da6a3ce929d0e0e4736"
