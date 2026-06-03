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


def extract_balanced_json_text(text: str, *, allow_arrays: bool = True) -> str | None:
    if not isinstance(text, str):
        return None

    start = -1
    opening_chars = "{[" if allow_arrays else "{"
    for idx, char in enumerate(text):
        if char in opening_chars:
            start = idx
            break
    if start < 0:
        return None

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
