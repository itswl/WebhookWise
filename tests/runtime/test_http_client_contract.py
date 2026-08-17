"""Outbound HTTP clients are owned by core/http_client — nowhere else.

The feishu_relay channel used to build a throwaway ``httpx.AsyncClient()`` per
delivery: unpooled (a TCP+TLS handshake per alert), ``trust_env`` left True
(the only outbound path honouring HTTP(S)_PROXY from the environment), no
trace headers, and never closed. The fix routed it through the shared
internal-hop client; this contract stops the next ad-hoc client at review
time instead of in production.
"""

import re
from pathlib import Path

import pytest

from tests.helpers.paths import PROJECT_ROOT

_PRODUCTION_DIRS = ("api", "services", "core", "adapters", "models", "schemas", "db", "contracts")
_OWNER = Path("core") / "http_client.py"


def test_only_core_http_client_constructs_async_clients() -> None:
    offenders: dict[str, list[int]] = {}
    for directory in _PRODUCTION_DIRS:
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py")):
            relative = path.relative_to(PROJECT_ROOT)
            if relative == _OWNER:
                continue
            hits = [
                lineno
                for lineno, line in enumerate(path.read_text().splitlines(), start=1)
                if re.search(r"httpx\.AsyncClient\(", line) and not line.lstrip().startswith("#")
            ]
            if hits:
                offenders[str(relative)] = hits
    assert offenders == {}, f"ad-hoc httpx.AsyncClient constructions crept back: {offenders}"


@pytest.mark.asyncio
async def test_internal_hop_client_closes_on_shutdown() -> None:
    """The module-owned client must be closable (and re-creatable) — every
    graceful shutdown used to leak its open connections."""
    from core import http_client

    client = http_client.get_deep_analysis_client()
    assert not client.is_closed

    await http_client.aclose_deep_analysis_client()
    assert client.is_closed

    reopened = http_client.get_deep_analysis_client()
    assert reopened is not client and not reopened.is_closed
    await http_client.aclose_deep_analysis_client()
