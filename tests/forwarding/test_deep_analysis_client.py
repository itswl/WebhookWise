"""The deep-analysis gateway reaches private infrastructure; forwards do not."""

import httpx
import pytest


def test_deep_analysis_client_is_not_dns_hardened() -> None:
    """Its target is OPENCLAW_GATEWAY_URL — process configuration naming a
    sidecar on the container network. Under the shared hardened client every
    request died with "target host resolves to a non-public IP", so the whole
    deep-analysis leg failed by design rather than by accident."""
    from core.http_client import get_deep_analysis_client

    client = get_deep_analysis_client()
    transport = client._transport
    assert not hasattr(transport, "_ww_hardened"), "the deep-analysis client must not be hardened"
    # Same instance on repeat calls: a new pool per delivery would leak sockets.
    assert get_deep_analysis_client() is client


def test_the_shared_client_still_refuses_private_targets() -> None:
    """The exemption must not spread. Forward target URLs are written about data
    a webhook sent, so a payload that can steer them is an SSRF primitive."""
    from core.http_client import build_http_client

    shared = build_http_client()
    assert shared._transport is not None
    # The hardening wraps connect; its presence is what this asserts, because a
    # future refactor that "unifies the clients" would silently remove it.
    from core import pinned_dns

    assert hasattr(pinned_dns, "harden_transport_against_rebinding")


@pytest.mark.asyncio
async def test_openclaw_dependencies_use_the_exempt_client() -> None:
    """Wiring, not just availability: the channel has to actually pick it up."""
    from core.http_client import get_deep_analysis_client
    from services.forwarding.circuit_breakers import build_openclaw_forward_dependencies

    deps = build_openclaw_forward_dependencies()
    assert deps.http_client is get_deep_analysis_client()


@pytest.mark.asyncio
async def test_a_private_gateway_url_is_actually_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this fixes, end to end: a request to a private host must
    leave the process instead of raising UnsafeTargetUrlError."""
    from core.http_client import build_http_client

    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    client = build_http_client(transport=httpx.MockTransport(handler))
    response = await client.post("http://hookprobe:8088/hooks/agent", json={})

    assert response.status_code == 200
    assert seen["url"] == "http://hookprobe:8088/hooks/agent"
    await client.aclose()
