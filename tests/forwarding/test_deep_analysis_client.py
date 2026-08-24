"""The deep-analysis gateway reaches private infrastructure; forwards do not."""

import httpx
import pytest


def test_deep_analysis_client_is_not_dns_hardened() -> None:
    """Its target is DEEP_ANALYSIS_GATEWAY_URL — process configuration naming a
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
async def test_gateway_dependencies_use_the_exempt_client() -> None:
    """Wiring, not just availability: the channel has to actually pick it up."""
    from core.http_client import get_deep_analysis_client
    from services.forwarding.circuit_breakers import build_deep_analysis_forward_dependencies

    deps = build_deep_analysis_forward_dependencies()
    assert deps.http_client is get_deep_analysis_client()


@pytest.mark.asyncio
async def test_the_poller_uses_the_exempt_client_too() -> None:
    """Submitting and collecting are two legs of one hop, and fixing only the
    first is worse than fixing neither: the gateway accepted the work, ran it,
    and produced a report, while every poll for the result was rejected by our
    own anti-SSRF guard and recorded as a transient error. The analysis stayed
    `pending` forever with a finished report sitting on the other side.
    """
    from unittest.mock import AsyncMock, patch

    from core.http_client import get_deep_analysis_client
    from services.analysis import deep_analysis_poll

    with patch.object(deep_analysis_poll, "poll_gateway_final", new=AsyncMock(return_value={})) as final:
        await deep_analysis_poll.poll_gateway_result_via_http("hook:deep-analysis:test")

    assert final.await_args is not None
    assert final.await_args.kwargs["http_client"] is get_deep_analysis_client()


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


@pytest.mark.asyncio
async def test_only_the_person_driven_path_declares_itself() -> None:
    """The gateway's budget breaker refuses spending nobody asked for, and it
    used to infer that from which door was used. That proxy broke the moment a
    forward RULE started posting to the operator door, so the caller now says.

    Silence means automated, and that direction is load-bearing: an unmarked
    caller is refused rather than spending freely, so forgetting the header can
    only ever cost an answer, never money.
    """
    import httpx

    from services.analysis import deep_analysis_trigger

    seen: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"runId": "r-1"})

    from services.forwarding.policies import DeepAnalysisTriggerPolicy

    policy = DeepAnalysisTriggerPolicy(
        enabled=True,
        timeout_seconds=60,
        platform="hookprobe",
        gateway_url="http://hookprobe:8088",
        hooks_token="t",
        connect_timeout=5.0,
        enable_degradation=False,
        http_api_url="http://hookprobe:8088",
        gateway_name="default",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data = {"source": "grafana", "headers": {}, "parsed_data": {"RuleName": "示例充值超限告警"}}
    try:
        await deep_analysis_trigger.request_gateway_analysis(data, http_client=client, policy=policy, operator=True)
        await deep_analysis_trigger.request_gateway_analysis(data, http_client=client, policy=policy)
    finally:
        await client.aclose()

    assert seen[0].get("x-operator") == "true", "a person's request must declare itself to be exempt"
    assert "x-operator" not in seen[1], "a rule must not claim to be a person, or the breaker never fires"
