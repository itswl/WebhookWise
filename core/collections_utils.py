from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def scalar_text_or_empty(value: Any) -> str:
    if value is None or isinstance(value, dict | list):
        return ""
    return " ".join(str(value).splitlines()).strip()


def pick_case_insensitive(mapping: Mapping[str, Any], *keys: str) -> Any | None:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return value
    return None
