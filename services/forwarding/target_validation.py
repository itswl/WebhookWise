"""Per-target-type URL validation, shared by the rules API and the catalog.

This used to live in api/v1/forwarding.py, which meant the integration catalog
(services layer) could not reuse it and grew its own http(s)-only check — so a
feishu_relay or feishu_app rule could never be created through guided setup.
One function, one policy per target type.
"""

from __future__ import annotations

from core.url_security import UnsafeTargetUrlError, validate_outbound_url

FEISHU_APP_URL_PREFIX = "feishu-app://"


async def validated_target_url(target_type: str, target_url: object) -> str:
    if target_type == "deep_analysis":
        return str(target_url or "").strip()
    if not isinstance(target_url, str) or not target_url.strip():
        raise UnsafeTargetUrlError("Target URL cannot be empty")
    stripped = target_url.strip()
    if target_type == "feishu_relay":
        # The relay door is PRIVATE infrastructure by design — the public-IP
        # requirement exists to stop user-data-driven exfiltration to internal
        # hosts, while a relay flip already demands the admin write key and
        # the relay hop itself is HMAC-signed. Require plain http(s) shape and
        # skip the public-resolution probe.
        if not stripped.startswith(("http://", "https://")):
            raise UnsafeTargetUrlError("feishu_relay target must be an http(s) URL")
        return stripped
    if target_type == "feishu_app":
        # Not a URL at all: the channel strips the prefix and messages the chat
        # through the fixed-host Feishu app transport. The generic validator
        # rejected the scheme outright, which made this channel impossible to
        # target from a rule saved through the API.
        if not stripped.startswith(FEISHU_APP_URL_PREFIX) or not stripped.removeprefix(FEISHU_APP_URL_PREFIX):
            raise UnsafeTargetUrlError("feishu_app target must look like feishu-app://<chat_id>")
        return stripped
    return await validate_outbound_url(stripped)
