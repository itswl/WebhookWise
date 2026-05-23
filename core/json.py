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
