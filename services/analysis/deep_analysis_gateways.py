"""Named deep-analysis gateways, so different rules can reach different ones.

One gateway needs no name, which is why the flat `DEEP_ANALYSIS_GATEWAY_URL` /
`_HTTP_API_URL` / `_HOOKS_TOKEN` / `_PLATFORM` settings ARE the gateway called
`default`. `DEEP_ANALYSIS_GATEWAYS` adds more, and a forward rule names which
one it wants (empty = default).

Only addressing and dialect vary per instance. Polling cadence, timeouts,
stability thresholds and degradation stay global: those are operational policy
about how patiently WebhookWise waits, not properties of the thing it waits on.

The load-bearing rule here is that an UNKNOWN name raises. A gateway name that
silently fell back to the default would send an investigation — with alert
payload in it — to a different operator's service than the rule asked for, and
the only symptom would be a report arriving from the wrong place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from core.logger import get_logger
from services.analysis.deep_analysis_platforms import DEFAULT_PLATFORM

logger = get_logger("analysis.deep_analysis_gateways")

DEFAULT_GATEWAY_NAME: Final = "default"


class UnknownGatewayError(ValueError):
    """A rule names a gateway that is not configured.

    Deliberately an error and not a fallback: see the module docstring.
    """


@dataclass(frozen=True, slots=True)
class GatewayInstance:
    """One deep-analysis gateway: where it is, what dialect it speaks."""

    name: str
    platform: str
    gateway_url: str
    http_api_url: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.gateway_url.strip())


def _default_instance() -> GatewayInstance:
    from core.app_context import get_config_manager

    cfg = get_config_manager().deep_analysis
    return GatewayInstance(
        name=DEFAULT_GATEWAY_NAME,
        platform=str(cfg.DEEP_ANALYSIS_PLATFORM or DEFAULT_PLATFORM).strip().lower(),
        gateway_url=str(cfg.DEEP_ANALYSIS_GATEWAY_URL or "").strip(),
        # Faithful to configuration, with NO fallback to gateway_url: an empty
        # DEEP_ANALYSIS_HTTP_API_URL is the switch that selects the legacy
        # WebSocket transport for polling. Defaulting it would silently disable
        # that path for anyone still on it.
        http_api_url=str(cfg.DEEP_ANALYSIS_HTTP_API_URL or "").strip(),
        token=str(cfg.DEEP_ANALYSIS_HOOKS_TOKEN or cfg.DEEP_ANALYSIS_GATEWAY_TOKEN or ""),
    )


def _parse_extra(raw: str, default: GatewayInstance) -> dict[str, GatewayInstance]:
    """Parse DEEP_ANALYSIS_GATEWAYS. Never raises: a malformed list must not take
    deep analysis offline for the rules that use the default gateway.

    Entries inherit any field they omit from the default instance, so adding a
    second gateway that shares a token is one short object.
    """
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed: Any = json.loads(text)
    except ValueError as error:
        logger.error("[DeepAnalysis] DEEP_ANALYSIS_GATEWAYS is not valid JSON, ignoring it: %s", error)
        return {}
    if not isinstance(parsed, list):
        logger.error("[DeepAnalysis] DEEP_ANALYSIS_GATEWAYS must be a JSON array, ignoring it")
        return {}

    instances: dict[str, GatewayInstance] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            logger.error("[DeepAnalysis] Ignoring a non-object entry in DEEP_ANALYSIS_GATEWAYS")
            continue
        name = str(entry.get("name") or "").strip().lower()
        if not name:
            logger.error("[DeepAnalysis] Ignoring a DEEP_ANALYSIS_GATEWAYS entry with no name")
            continue
        if name == DEFAULT_GATEWAY_NAME:
            logger.error("[DeepAnalysis] '%s' is reserved for the flat settings; ignoring", DEFAULT_GATEWAY_NAME)
            continue
        gateway_url = str(entry.get("url") or entry.get("gateway_url") or default.gateway_url).strip()
        instances[name] = GatewayInstance(
            name=name,
            platform=str(entry.get("platform") or default.platform).strip().lower(),
            gateway_url=gateway_url,
            # Named gateways DO default to their own url, unlike the default
            # instance: the WebSocket transport needs device credentials that
            # only ever existed for the default gateway, so HTTP is the only
            # transport available here and an empty value would just break them.
            http_api_url=str(entry.get("http_api_url") or gateway_url).strip(),
            token=str(entry.get("token") or default.token),
        )
    return instances


def gateway_registry() -> dict[str, GatewayInstance]:
    """Every configured gateway, keyed by name. `default` is always present."""
    from core.app_context import get_config_manager

    default = _default_instance()
    registry = {DEFAULT_GATEWAY_NAME: default}
    registry.update(_parse_extra(str(get_config_manager().deep_analysis.DEEP_ANALYSIS_GATEWAYS or ""), default))
    return registry


def resolve_gateway(name: str | None) -> GatewayInstance:
    """Resolve a rule's gateway name. Empty means the default.

    Raises UnknownGatewayError for a name that is not configured — deleting a
    gateway from configuration while a rule still points at it must surface as a
    failed delivery naming the gateway, not as an investigation quietly sent
    somewhere else.
    """
    registry = gateway_registry()
    key = (name or "").strip().lower() or DEFAULT_GATEWAY_NAME
    instance = registry.get(key)
    if instance is None:
        raise UnknownGatewayError(
            f"deep-analysis gateway {key!r} is not configured (known: {', '.join(sorted(registry))})"
        )
    return instance


async def probe_gateway(instance: GatewayInstance, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Check a gateway is reachable and the token works, WITHOUT starting a run.

    Asks for a session that cannot exist. The three answers are exactly the three
    ways this configuration breaks, and they are distinguishable:

      404 (or 2xx)  -> reachable, token accepted           -> ok
      401 / 403     -> reachable, token rejected           -> auth
      connect error -> address wrong or service down       -> unreachable

    Deliberately not POST /hooks/agent: probing by starting a real investigation
    would cost money per click and put junk reports in the ledger. Deliberately
    not /healthz either — that answers "is something alive at this URL", not "will
    MY credential work against the contract I depend on".
    """
    import httpx

    if not instance.gateway_url and not instance.http_api_url:
        return {"ok": False, "state": "unconfigured", "detail": "no gateway address configured"}

    base = (instance.http_api_url or instance.gateway_url).rstrip("/")
    url = f"{base}/sessions/ww-connectivity-probe/final"
    headers = {"Authorization": f"Bearer {instance.token}"} if instance.token else {}

    from core.http_client import get_deep_analysis_client

    try:
        response = await get_deep_analysis_client().get(url, headers=headers, timeout=timeout_seconds)
    except httpx.HTTPError as error:
        return {"ok": False, "state": "unreachable", "detail": f"{type(error).__name__}: {error}"}

    if response.status_code in (401, 403):
        return {"ok": False, "state": "auth", "detail": f"the gateway rejected the token ({response.status_code})"}
    if response.status_code >= 500:
        return {"ok": False, "state": "error", "detail": f"the gateway returned {response.status_code}"}
    # 404 is the expected healthy answer: it means the contract is there and the
    # credential was accepted, and only the made-up session is missing.
    return {"ok": True, "state": "ok", "detail": f"reachable, token accepted (HTTP {response.status_code})"}
