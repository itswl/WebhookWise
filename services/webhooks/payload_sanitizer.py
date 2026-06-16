"""Payload sanitization pipeline run before AI analysis."""

from __future__ import annotations

import asyncio
from typing import Any

from core import json
from core.logger import get_logger
from core.sensitive_data import redact_nested
from services.webhooks.policies import PayloadPolicy

logger = get_logger("payload_sanitizer")


def _should_offload(data: object, policy: PayloadPolicy, depth: int = 0) -> bool:
    if depth > 2:
        return False
    if data is None:
        return False
    if isinstance(data, dict):
        if len(data) > 2000:
            return True
        threshold = policy.offload_threshold_bytes
        for n, v in enumerate(data.values()):
            if isinstance(v, (str, bytes, bytearray)) and len(v) >= threshold:
                return True
            if isinstance(v, list) and len(v) > 5000:
                return True
            if isinstance(v, dict) and (len(v) > 2000 or _should_offload(v, policy, depth + 1)):
                return True
            if isinstance(v, list) and depth < 2:
                for item in v[:2000]:
                    if isinstance(item, (dict, list)) and _should_offload(item, policy, depth + 1):
                        return True
            if n >= 2000:
                break
        return False
    if isinstance(data, list):
        if len(data) > 5000:
            return True
        if depth < 2:
            for item in data[:2000]:
                if isinstance(item, (dict, list)) and _should_offload(item, policy, depth + 1):
                    return True
        return False
    return False


async def sanitize_for_ai_async(
    parsed_data: dict[str, Any], *, strip_configured_keys: bool = True, truncate: bool = True
) -> dict[str, Any]:
    if not parsed_data:
        return parsed_data
    policy = PayloadPolicy.from_config()
    if _should_offload(parsed_data, policy):
        res = await asyncio.to_thread(
            sanitize_for_ai,
            parsed_data,
            strip_configured_keys=strip_configured_keys,
            truncate=truncate,
            policy=policy,
        )
        return res
    return sanitize_for_ai(parsed_data, strip_configured_keys=strip_configured_keys, truncate=truncate, policy=policy)


def sanitize_for_ai(
    parsed_data: dict[str, Any],
    *,
    strip_configured_keys: bool = True,
    truncate: bool = True,
    policy: PayloadPolicy | None = None,
) -> dict[str, Any]:
    """Sanitize parsed_data, removing noise fields and truncating oversized content.

    1. Recursively remove the keys listed in AI_PAYLOAD_STRIP_KEYS
    2. If the serialized size exceeds AI_PAYLOAD_MAX_BYTES, truncate large-value fields

    OpenClaw deep analysis can disable strip/truncate, keeping only sensitive-field redaction.
    """
    if not parsed_data:
        return parsed_data

    policy = policy or PayloadPolicy.from_config()
    strip_keys = set(policy.strip_keys) if strip_configured_keys else set()

    # Phase 1: recursively remove noise fields (_strip_keys_recursive is itself
    # non-destructive, so no deepcopy is needed)
    cleaned_obj = redact_nested(_strip_keys_recursive(parsed_data, strip_keys))
    cleaned: dict[str, Any] = cleaned_obj if isinstance(cleaned_obj, dict) else parsed_data

    # Phase 2: check the size, truncate if it exceeds the limit
    if not truncate:
        return cleaned

    max_bytes = policy.max_bytes
    serialized = json.dumps_bytes(cleaned)
    if max_bytes > 0 and len(serialized) > max_bytes:
        logger.info(
            "Payload exceeds the AI input limit (%d > %d bytes), truncating",
            len(serialized),
            max_bytes,
        )
        truncated = _truncate_large_values(cleaned, max_bytes)
        if isinstance(truncated, dict):
            cleaned = truncated

    return cleaned


def _strip_keys_recursive(data: object, strip_keys: set[str], max_depth: int = 20, _depth: int = 0) -> object:
    """Recursively remove the specified keys."""
    if _depth >= max_depth:
        # Exceeded the max depth; truncate and return directly
        if isinstance(data, (dict, list)):
            return {"_truncated": True, "_reason": f"max recursion depth {max_depth}"}
        return data
    if isinstance(data, dict):
        return {
            k: _strip_keys_recursive(v, strip_keys, max_depth, _depth + 1)
            for k, v in data.items()
            if k.lower() not in strip_keys
        }
    if isinstance(data, list):
        return [_strip_keys_recursive(item, strip_keys, max_depth, _depth + 1) for item in data]
    return data


# Lines containing these (case-insensitive) survive string truncation: in a big
# log blob the root-cause signal is usually an error/stack line, which a blind
# head-cut (v[:200]) would discard. Keeping head + tail + matching lines gives
# the AI the highest-signal slice within the same byte budget.
_ERROR_KEYWORDS = (
    "error",
    "panic",
    "fatal",
    "exception",
    "traceback",
    "failed",
    "failure",
    "timeout",
    "timed out",
    "refused",
    "denied",
    "oom",
    "killed",
    "crash",
    "unhealthy",
    "5xx",
    "critical",
    "错误",
    "失败",
    "异常",
    "超时",
)
_STRING_TRUNCATE_THRESHOLD = 200
_STRING_HEAD_CHARS = 600
_STRING_TAIL_CHARS = 400
_MAX_ERROR_LINES = 20
_LIST_HEAD = 8
_LIST_TAIL = 2


def _summarize_large_string(value: str) -> str:
    """Structure-aware string truncation: keep head + tail + error/stack lines
    instead of a blind prefix, so the root-cause signal survives."""
    head = value[:_STRING_HEAD_CHARS]
    tail = value[-_STRING_TAIL_CHARS:] if len(value) > _STRING_HEAD_CHARS + _STRING_TAIL_CHARS else ""

    # Pull out lines that look like errors and aren't already in head/tail.
    head_end, tail_start = _STRING_HEAD_CHARS, len(value) - _STRING_TAIL_CHARS
    error_lines: list[str] = []
    pos = 0
    for line in value.splitlines():
        line_start, pos = pos, pos + len(line) + 1
        if line_start < head_end or (tail and line_start >= tail_start):
            continue  # already represented in head/tail
        low = line.lower()
        if any(kw in low for kw in _ERROR_KEYWORDS):
            error_lines.append(line.strip())
            if len(error_lines) >= _MAX_ERROR_LINES:
                break

    parts = [f"{head}...[truncated, original {len(value)} chars; kept head+tail+error lines]"]
    if error_lines:
        parts.append("[error lines]\n" + "\n".join(error_lines))
    if tail:
        parts.append("[tail]\n" + tail)
    return "\n".join(parts)


def _truncate_large_values(data: object, max_bytes: int, depth: int = 0) -> object:
    """Truncate by value size in descending order until the total is under the
    limit. When truncating, produce a structure-aware summary instead of blindly
    cutting the prefix."""
    if depth > 5:
        # Exceeded the recursion depth; return a summary directly
        return {"_truncated": True, "_reason": "max depth exceeded"}

    if isinstance(data, dict):
        # Sort by the serialized size of each value in descending order
        items_with_size = []
        for k, v in data.items():
            size = len(json.dumps_bytes(v))
            items_with_size.append((k, v, size))
        items_with_size.sort(key=lambda x: x[2], reverse=True)

        result: dict[str, object] = {}
        current_size = 2  # {}
        for k, v, size in items_with_size:
            # Truncate when adding this field whole would blow the budget. Unlike
            # the old guard (which kept the first/largest field whole even when it
            # alone exceeded the budget — silently bypassing the limit), a field
            # that on its own is larger than the budget is also summarized.
            over_budget = current_size + size + len(k) + 4 > max_bytes
            if over_budget and (result or size + len(k) + 4 > max_bytes):
                # Truncate this field: for long strings keep head + tail + error
                # lines; for large dicts recurse with the same smart summary
                # (preserving error lines in nested logs); for large lists keep
                # head and tail.
                if isinstance(v, str) and len(v) > _STRING_TRUNCATE_THRESHOLD:
                    result[k] = _summarize_large_string(v)
                elif isinstance(v, dict):
                    result[k] = _truncate_large_values(v, max(max_bytes // 2, 256), depth + 1)
                elif isinstance(v, list):
                    result[k] = _summarize_large_list(v)
                else:
                    result[k] = v
            else:
                result[k] = v
                current_size += size + len(k) + 4
        return result

    if isinstance(data, list) and len(json.dumps_bytes(data)) > max_bytes:
        return _summarize_large_list(data)

    return data


def _summarize_large_list(data: list[Any]) -> list[Any]:
    """Keep head + tail of a large list with an elision marker, instead of only
    the head — the last items (most recent events) are often the relevant ones."""
    if len(data) <= _LIST_HEAD + _LIST_TAIL + 1:
        return data
    omitted = len(data) - _LIST_HEAD - _LIST_TAIL
    return [
        *data[:_LIST_HEAD],
        {"_truncated": True, "_omitted_items": omitted, "_original_length": len(data)},
        *data[-_LIST_TAIL:],
    ]
