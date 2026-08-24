"""
Ecosystem Adapters for WebhookWise.
Handles normalization of various webhook sources into a standard format.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from adapters.declarative import register_declarative_adapters
from adapters.simple_adapters import normalize_level, register_simple_adapters
from contracts.webhook_payload import WebhookData, webhook_data_from_mapping
from core import json
from core.logger import get_logger

logger = get_logger("ecosystem_adapters")

HeadersLike = Mapping[str, Any]

__all__ = [
    "HeadersLike",
    "NormalizedWebhook",
    "_header_get",
    "initialize_adapters",
    "normalize_level",
    "normalize_webhook_event",
    "register_declarative_adapters",
    "register_simple_adapters",
]


@dataclass(frozen=True)
class NormalizedWebhook:
    source: str
    data: WebhookData
    adapter: str


def _header_get(headers: HeadersLike | None, key: str) -> str | None:
    if not headers:
        return None
    value = headers.get(key)
    if value is not None:
        return str(value)
    target = key.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def initialize_adapters() -> None:
    """Initialize built-in adapters during process startup."""
    from adapters.registry import registry

    before = registry.status()["normalizers"]
    register_simple_adapters()
    # Declarative YAML adapters register after the code adapters, so on a detect
    # tie the built-in code adapter wins unless a spec opts into a higher
    # priority. See adapters/declarative.py.
    register_declarative_adapters()
    if registry.status()["normalizers"] != before:
        logger.info("[Adapter] Adapter registration complete")


# Relay-style senders (chat bridges, EventBridge → webhook shims) wrap the real
# document as a JSON *string* under one key. Every detector then sees a
# single-key dict and matches nothing, so the alert lands as source=unknown with
# its structure invisible to identity extraction and to the AI.
_ENVELOPE_KEYS: Final = ("text", "message", "body", "payload", "data")


_MAX_SALVAGE_DEPTH: Final = 3


def _salvage_trailing_object_pair(tail: str, *, depth: int) -> dict[str, Any] | None:
    """Recover the complete pairs of the ONE incomplete pair a cut leaves behind.

    The pairs before the cut are whole; the pair straddling it is not, and it is
    often the one that matters. An AWS Health envelope carries its rule identity
    at `detail.eventTypeCode`, and `detail` is exactly the object the truncation
    lands inside — so stopping at the top level recovers enough to say "this is
    an AWS Health event" and not enough to say WHICH, which is what the
    aws_health adapter needs (`detect` requires both `source` and
    `detail.eventTypeCode`).
    """
    match = re.match(r'\s*"([^"]+)"\s*:\s*(\{.*)\Z', tail, re.S)
    if match is None:
        return None
    inner = _salvage_truncated_object(match.group(2), depth=depth)
    return {match.group(1): inner} if inner else None


def _salvage_truncated_object(text: str, *, depth: int = 0) -> dict[str, Any] | None:
    """Parse the complete pairs of a JSON object that was cut short.

    A relay that truncates its own body at a fixed width leaves valid JSON and
    then stops mid-token. `json.loads` rejects the whole document, so every field
    is lost — including the ones that arrived intact, which for an EventBridge
    envelope are exactly the identifying ones because they come first.

    Observed: an AWS Health event listing many affected RDS entities arrived cut
    to 4000 characters by its sender, so it landed as source=unknown with an
    empty rule name and was rated `low` — an account-level notice, silently
    demoted, four times since 2026-08-05.

    Walks the text tracking string state and depth, and cuts at the last comma
    seen at depth 1: everything before it is a whole number of pairs, so closing
    the brace there parses. The straddling pair is then recovered one level down
    (bounded by _MAX_SALVAGE_DEPTH), because that is where the rule identity
    lives. Returns None when nothing survives, which keeps "truncated beyond
    use" distinguishable from "recovered".
    """
    if not text.startswith("{"):
        return None
    depth_level = 0
    in_string = False
    escaped = False
    cut = -1
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            depth_level += 1
        elif char in "}]":
            depth_level -= 1
        elif char == "," and depth_level == 1:
            cut = index
    salvaged: dict[str, Any] = {}
    if cut >= 0:
        try:
            parsed = json.loads(text[:cut] + "}")
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        salvaged = parsed
        tail = text[cut + 1 :]
    else:
        tail = text[1:]
    if depth < _MAX_SALVAGE_DEPTH:
        pair = _salvage_trailing_object_pair(tail, depth=depth + 1)
        if pair:
            salvaged.update(pair)
    return salvaged or None


def _unwrap_json_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a relay envelope such as SNS's `{"text": "<json>", "subject": …}`.

    Three conditions must hold together, so a real alert that merely *has* a
    "message" field is never gutted:

    1. exactly one key from the relay allowlist holds a JSON-object string;
    2. every other key is a scalar — anything structured means the outer
       document is the alert, not a wrapper;
    3. the inner object is RICHER than the outer one. This is the sharp test:
       a wrapper carries less than what it wraps, so `{"message": "{\\"a\\":1}",
       "severity": "high", "host": "x"}` stays intact while a 3-key SNS
       envelope around a 9-key AWS Health document unwraps.

    Only the matching view is replaced; raw_payload still stores the original.
    """
    candidates = [
        (key, value)
        for key, value in data.items()
        if key in _ENVELOPE_KEYS and isinstance(value, str) and value.strip().startswith("{")
    ]
    if len(candidates) != 1:
        return data
    key, value = candidates[0]
    if any(k != key and isinstance(v, dict | list) for k, v in data.items()):
        return data
    try:
        inner = json.loads(value.strip())
    except ValueError:
        # The sender truncated its own body. Salvage the pairs that did arrive
        # rather than discarding the document whole: the alternative is what
        # production did for three weeks, which is rate an AWS account-level
        # notice `low` because nothing could identify it.
        salvaged = _salvage_truncated_object(value.strip())
        if salvaged is None or len(salvaged) <= len(data):
            logger.warning(
                "[Adapter] %r envelope holds unparseable JSON and nothing complete could be "
                "salvaged (%d chars); the sender is truncating its body",
                key,
                len(value),
            )
            return data
        logger.warning(
            "[Adapter] %r envelope was TRUNCATED by its sender at %d chars; recovered %d "
            "complete top-level field(s) so the alert can still be identified",
            key,
            len(value),
            len(salvaged),
        )
        return salvaged
    if not isinstance(inner, dict) or len(inner) <= len(data):
        return data
    logger.info("[Adapter] Unwrapped %r relay envelope before adapter matching", key)
    return cast(dict[str, Any], inner)


def normalize_webhook_event(
    data: Any,
    source: str | None,
    headers: HeadersLike | None = None,
) -> NormalizedWebhook:
    """Select an adapter based on the source or payload characteristics and output normalized data."""
    from adapters.registry import registry

    if not isinstance(data, dict):
        resolved_source = str(source or _header_get(headers, "X-Webhook-Source") or "unknown").strip().lower()
        return NormalizedWebhook(resolved_source, webhook_data_from_mapping({"raw": data}), "passthrough")

    data = _unwrap_json_envelope(data)

    h_src = str(_header_get(headers, "X-Webhook-Source") or "").strip().lower()
    s_hint = str(source or "").strip().lower() or h_src

    adapter_name = registry.find_adapter_by_source(s_hint) if s_hint else None
    if adapter_name is None:
        adapter_name = registry.find_adapter_by_payload(data)

    if adapter_name is None:
        return NormalizedWebhook(s_hint or "unknown", webhook_data_from_mapping(data, strict=False), "passthrough")

    normalized = registry.normalize(adapter_name, dict(data))

    placeholder_sources = {"unknown", "custom", "default", "generic"}
    source_is_alias = registry.find_adapter_by_source(s_hint) == adapter_name if s_hint else False
    final_source = s_hint if (s_hint and not source_is_alias and s_hint not in placeholder_sources) else adapter_name

    logger.info("[Adapter] Successfully matched adapter: name=%s, final_source=%s", adapter_name, final_source)
    return NormalizedWebhook(final_source, normalized, adapter_name)
