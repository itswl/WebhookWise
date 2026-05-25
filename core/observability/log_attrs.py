"""Helpers for canonical OpenTelemetry log attributes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.observability.attributes import normalize_attributes


def log_extra(attributes: Mapping[str, Any] | None = None, /, **kwargs: Any) -> dict[str, str | bool | int | float]:
    merged: dict[str, Any] = {}
    if attributes:
        merged.update(attributes)
    merged.update(kwargs)
    return normalize_attributes(merged)
