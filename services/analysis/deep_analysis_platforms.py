"""The deep-analysis gateway dialects, as data.

The gateway layer is neutral: WebhookWise posts an investigation request to
whatever runs at DEEP_ANALYSIS_GATEWAY_URL, and DEEP_ANALYSIS_PLATFORM names
which dialect that thing speaks. Keeping the dialects in one table is what stops
a product name from leaking back into the layer — the code used to branch on the
string "hermes" in two separate places and then hard-code "openclaw" as the
answer everywhere else, so swapping the gateway left the wrong name on every
record and in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

AuthStyle = Literal["bearer", "hmac_signature"]


@dataclass(frozen=True, slots=True)
class GatewayDialect:
    """How one gateway product expects an agent request to be made."""

    name: str
    agent_path: str
    auth: AuthStyle


# hookprobe implements OpenClaw's contract deliberately, so it shares the
# dialect instead of getting a near-identical copy of it.
_BEARER_HOOKS: Final = ("openclaw", "hookprobe")

DEEP_ANALYSIS_DIALECTS: Final[dict[str, GatewayDialect]] = {
    **{name: GatewayDialect(name=name, agent_path="/hooks/agent", auth="bearer") for name in _BEARER_HOOKS},
    "hermes": GatewayDialect(name="hermes", agent_path="/webhooks/agent", auth="hmac_signature"),
}

DEEP_ANALYSIS_PLATFORMS: Final = frozenset(DEEP_ANALYSIS_DIALECTS)

DEFAULT_PLATFORM: Final = "openclaw"


def resolve_dialect(platform: str) -> GatewayDialect:
    """Resolve a configured platform name to its dialect. Never raises.

    An unrecognised name falls back to the bearer dialect rather than failing:
    the likely reason for one is a gateway that implements the same contract and
    is simply newer than this table, and taking deep analysis offline over a
    label would be the worse outcome. The name is preserved either way, so the
    record and the card still say what was actually configured.
    """
    key = platform.strip().lower()
    known = DEEP_ANALYSIS_DIALECTS.get(key)
    if known is not None:
        return known
    return GatewayDialect(name=key or DEFAULT_PLATFORM, agent_path="/hooks/agent", auth="bearer")


def configured_deep_analysis_platform() -> str:
    """The platform name to record on rows and show in the dashboard."""
    from core.app_context import get_config_manager

    configured = str(get_config_manager().deep_analysis.DEEP_ANALYSIS_PLATFORM or "").strip().lower()
    return configured or DEFAULT_PLATFORM
