"""Named gateways: the three things that make multi-gateway correct.

Routing to several investigators is mostly plumbing. What is NOT plumbing:

  1. an unknown name must raise, never fall back to the default;
  2. a run must be COLLECTED from the gateway it was submitted to;
  3. each gateway carries its own token.

Those are what these tests hold. The rest of the wiring is exercised by the
existing forwarding and poller suites.
"""

from __future__ import annotations

import json

import pytest

from core.app_context import get_config_manager
from services.analysis.deep_analysis_gateways import (
    DEFAULT_GATEWAY_NAME,
    UnknownGatewayError,
    gateway_registry,
    resolve_gateway,
)


@pytest.fixture(autouse=True)
def _flat_default() -> None:
    cfg = get_config_manager().deep_analysis
    cfg.DEEP_ANALYSIS_PLATFORM = "hookprobe"
    cfg.DEEP_ANALYSIS_GATEWAY_URL = "http://hookprobe:8088"
    cfg.DEEP_ANALYSIS_HTTP_API_URL = "http://hookprobe:8088"
    cfg.DEEP_ANALYSIS_HOOKS_TOKEN = "default-token"
    cfg.DEEP_ANALYSIS_GATEWAYS = ""


def _configure(*entries: dict[str, object]) -> None:
    get_config_manager().deep_analysis.DEEP_ANALYSIS_GATEWAYS = json.dumps(list(entries))


def test_the_flat_settings_are_the_default_gateway() -> None:
    """One gateway needs no name, so the existing settings keep working as-is —
    every rule written before named gateways existed still resolves."""
    gateway = resolve_gateway("")

    assert gateway.name == DEFAULT_GATEWAY_NAME
    assert gateway.platform == "hookprobe"
    assert gateway.gateway_url == "http://hookprobe:8088"
    assert gateway.token == "default-token"
    assert resolve_gateway(None) == gateway


def test_an_unknown_gateway_raises_instead_of_falling_back() -> None:
    """The load-bearing rule. Deleting a gateway from configuration while a rule
    still points at it must fail that delivery, naming the gateway — falling
    back would post an investigation, alert payload included, to a service the
    rule never named, and the only symptom would be a report arriving from
    somewhere unexpected."""
    with pytest.raises(UnknownGatewayError) as caught:
        resolve_gateway("hermes-eu")

    # The message has to name what IS configured, or an operator cannot act.
    assert "hermes-eu" in str(caught.value)
    assert DEFAULT_GATEWAY_NAME in str(caught.value)


def test_each_gateway_keeps_its_own_token_and_dialect() -> None:
    """Two investigators, two credentials. Sharing the default token by accident
    would send one gateway's secret to the other."""
    _configure(
        {"name": "hermes-eu", "platform": "hermes", "url": "https://hermes.internal", "token": "hermes-token"},
        {"name": "probe-2", "url": "http://hookprobe-2:8088"},
    )

    hermes = resolve_gateway("hermes-eu")
    assert hermes.platform == "hermes"
    assert hermes.token == "hermes-token"

    # An omitted field inherits from the default, so a second gateway that
    # shares a token is one short object.
    second = resolve_gateway("probe-2")
    assert second.platform == "hookprobe"
    assert second.token == "default-token"
    assert second.gateway_url == "http://hookprobe-2:8088"


def test_names_are_case_insensitive() -> None:
    """Rules store a lower-cased name, so lookups must not care what was typed."""
    _configure({"name": "Hermes-EU", "url": "https://hermes.internal"})

    assert resolve_gateway("HERMES-eu").name == "hermes-eu"
    assert set(gateway_registry()) == {DEFAULT_GATEWAY_NAME, "hermes-eu"}


def test_a_broken_gateway_list_does_not_take_the_default_offline() -> None:
    """Rules on the default gateway must keep working while someone fixes a
    typo in the JSON. Silently losing every deep analysis over a malformed
    optional setting would be a worse failure than ignoring it."""
    get_config_manager().deep_analysis.DEEP_ANALYSIS_GATEWAYS = "{not json"

    assert resolve_gateway("").gateway_url == "http://hookprobe:8088"
    assert set(gateway_registry()) == {DEFAULT_GATEWAY_NAME}


def test_default_cannot_be_shadowed_by_an_entry() -> None:
    """`default` means the flat settings. An entry claiming that name would make
    two different addresses answer to one name depending on load order."""
    _configure({"name": "default", "url": "http://impostor:9999"})

    assert resolve_gateway("default").gateway_url == "http://hookprobe:8088"


def test_the_default_gateway_leaves_the_websocket_switch_alone() -> None:
    """An empty DEEP_ANALYSIS_HTTP_API_URL selects the legacy WebSocket
    transport for polling. Defaulting it to the trigger URL — a tempting
    convenience — silently disables that path for anyone still on it."""
    cfg = get_config_manager().deep_analysis
    cfg.DEEP_ANALYSIS_HTTP_API_URL = ""

    assert resolve_gateway("").http_api_url == ""

    # Named gateways are the opposite: device auth never existed for them, so
    # HTTP is their only transport and an empty value would just break them.
    _configure({"name": "probe-2", "url": "http://hookprobe-2:8088"})
    assert resolve_gateway("probe-2").http_api_url == "http://hookprobe-2:8088"


def test_the_registry_always_contains_the_default() -> None:
    _configure({"name": "probe-2", "url": "http://hookprobe-2:8088"})

    registry = gateway_registry()
    assert set(registry) == {DEFAULT_GATEWAY_NAME, "probe-2"}


@pytest.mark.asyncio
async def test_polling_asks_the_gateway_the_run_was_submitted_to() -> None:
    """The correctness point that makes multi-gateway work at all.

    A session key is issued BY one gateway and means nothing to another. So the
    poller must resolve its address from the analysis ROW, not from the rule:
    re-reading the rule would send the question to wherever the rule points now,
    and after a rule edit that is a different service — which answers 404 for a
    session it never created, forever.
    """
    from services.analysis import deep_analysis_poll

    _configure({"name": "probe-2", "platform": "hookprobe", "url": "http://hookprobe-2:9099"})

    asked: list[str] = []

    async def _fake_fetch(rec: dict[str, object], *, policy: object) -> dict[str, object]:
        asked.append(str(getattr(policy, "http_api_url", "")))
        return {"status": "pending"}

    original = deep_analysis_poll._fetch_poll_result
    deep_analysis_poll._fetch_poll_result = _fake_fetch  # type: ignore[assignment]
    try:
        for gateway_name, expected in (("probe-2", "http://hookprobe-2:9099"), ("", "http://hookprobe:8088")):
            await deep_analysis_poll._poll_single_record(
                {
                    "id": 1,
                    "gateway_name": gateway_name,
                    "gateway_session_key": "hook:deep-analysis:test",
                    "created_at": None,
                    "status": "pending",
                    "analysis_result": {},
                    "poll_attempts": 0,
                }
            )
            assert asked[-1] == expected, f"{gateway_name or 'default'} was polled at {asked[-1]}"
    finally:
        deep_analysis_poll._fetch_poll_result = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_a_run_whose_gateway_vanished_fails_terminally() -> None:
    """There is nowhere left to ask, so retrying forever is worse than failing:
    the record would sit `pending` and reschedule until it timed out, reporting
    slowness instead of a configuration mistake."""
    from services.analysis import deep_analysis_poll

    result = await deep_analysis_poll._poll_single_record(
        {
            "id": 7,
            "gateway_name": "deleted-gateway",
            "gateway_session_key": "hook:deep-analysis:test",
            "created_at": None,
            "status": "pending",
            "analysis_result": {},
            "poll_attempts": 0,
        }
    )

    assert result["action"] == "update"
    assert result["status"] == "failed"
    assert "deleted-gateway" in str(result["analysis_result"]["root_cause"])


@pytest.mark.asyncio
async def test_the_probe_distinguishes_the_three_ways_a_gateway_breaks() -> None:
    """Configuration breaks in exactly three ways and the probe must tell them
    apart, or it just says "broken" and the operator still has to guess:

      * wrong address  -> unreachable
      * wrong token    -> auth
      * both fine      -> ok

    Verified live against hookprobe before this was written: a made-up session
    answers 404 with a good token and 401 with a bad one.
    """
    import httpx

    from core import http_client
    from services.analysis.deep_analysis_gateways import probe_gateway

    instance = resolve_gateway("")

    cases = {404: "ok", 200: "ok", 401: "auth", 403: "auth", 503: "error"}
    for status, expected in cases.items():

        def handler(request: httpx.Request, code: int = status) -> httpx.Response:
            return httpx.Response(code, json={"detail": "x"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        original = http_client.get_deep_analysis_client
        http_client.get_deep_analysis_client = lambda bound=client: bound  # type: ignore[assignment]
        try:
            result = await probe_gateway(instance)
        finally:
            http_client.get_deep_analysis_client = original  # type: ignore[assignment]
            await client.aclose()
        assert result["state"] == expected, f"HTTP {status} should read as {expected}"
        assert result["ok"] is (expected == "ok")


@pytest.mark.asyncio
async def test_an_unreachable_gateway_reads_as_unreachable_not_a_crash() -> None:
    """A wrong hostname is the most likely mistake, so it must come back as a
    diagnosis rather than propagate as an exception into the API handler."""
    import httpx

    from core import http_client
    from services.analysis.deep_analysis_gateways import probe_gateway

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Temporary failure in name resolution")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    original = http_client.get_deep_analysis_client
    http_client.get_deep_analysis_client = lambda: client  # type: ignore[assignment]
    try:
        result = await probe_gateway(resolve_gateway(""))
    finally:
        http_client.get_deep_analysis_client = original  # type: ignore[assignment]
        await client.aclose()

    assert result["ok"] is False
    assert result["state"] == "unreachable"
    assert "resolution" in result["detail"]


@pytest.mark.asyncio
async def test_an_unconfigured_gateway_says_so_without_a_request() -> None:
    """No address means there is nothing to probe; saying "unreachable" would
    blame the network for an empty setting."""
    from core.app_context import get_config_manager
    from services.analysis.deep_analysis_gateways import probe_gateway

    cfg = get_config_manager().deep_analysis
    cfg.DEEP_ANALYSIS_GATEWAY_URL = ""
    cfg.DEEP_ANALYSIS_HTTP_API_URL = ""

    result = await probe_gateway(resolve_gateway(""))

    assert result["state"] == "unconfigured"


def test_each_gateway_gets_its_own_circuit_breaker() -> None:
    """One shared breaker was an outage path: a gateway going down tripped it and
    every OTHER gateway's deliveries were then rejected too, degrading alerts to
    local AI because someone else's service was unhealthy."""
    from services.forwarding.circuit_breakers import get_deep_analysis_breaker

    default_breaker = get_deep_analysis_breaker("")
    assert get_deep_analysis_breaker("default") is default_breaker
    assert get_deep_analysis_breaker("DEFAULT") is default_breaker

    other = get_deep_analysis_breaker("probe-b")
    assert other is not default_breaker
    assert get_deep_analysis_breaker("probe-b") is other
