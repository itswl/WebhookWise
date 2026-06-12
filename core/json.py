"""Project-wide JSON helpers backed by orjson."""

from __future__ import annotations

from typing import Any

import orjson

JSONDecodeError = orjson.JSONDecodeError


def dumps_bytes(value: Any, *, sort_keys: bool = False) -> bytes:
    option = orjson.OPT_SORT_KEYS if sort_keys else 0
    return orjson.dumps(value, option=option, default=str)


def dumps(value: Any, *, sort_keys: bool = False, indent: bool = False) -> str:
    option = 0
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS
    if indent:
        option |= orjson.OPT_INDENT_2
    return orjson.dumps(value, option=option, default=str).decode("utf-8")


def loads(data: str | bytes | bytearray | memoryview) -> Any:
    return orjson.loads(data)


def _balanced_span_from(text: str, start: int) -> str | None:
    """Return the balanced bracket span beginning at ``start`` (a ``{`` or ``[``).

    Returns ``None`` if the brackets never close (e.g. truncated JSON).
    """
    pairs = {"{": "}", "[": "]"}
    stack: list[str] = []
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return text[start : idx + 1]
    return None


def extract_balanced_json_text(text: str, *, allow_arrays: bool = True) -> str | None:
    """Extract a balanced JSON object/array embedded in free-form ``text``.

    LLM output routinely wraps the real JSON report in a "thinking" prose
    preamble that itself contains braces/brackets (code snippets, set notation,
    example payloads). Naively taking the *first* balanced span therefore grabs
    that prose fragment instead of the report. To be robust, we scan every
    candidate opening position and return the *last* span that actually parses
    as JSON; if none parse, we fall back to the last balanced span so callers
    that run a repair pass still get the most plausible candidate.
    """
    if not isinstance(text, str):
        return None

    opening_chars = "{[" if allow_arrays else "{"
    last_parseable: str | None = None
    last_balanced: str | None = None
    idx = 0
    length = len(text)
    while idx < length:
        if text[idx] not in opening_chars:
            idx += 1
            continue
        span = _balanced_span_from(text, idx)
        if span is None:
            # Unbalanced from here on (e.g. truncated tail); nothing more to find.
            break
        last_balanced = span
        try:
            orjson.loads(span)
        except orjson.JSONDecodeError:
            pass
        else:
            last_parseable = span
        # Skip past this whole span so nested openers inside it are not treated
        # as separate top-level candidates.
        idx += len(span)
    return last_parseable if last_parseable is not None else last_balanced
