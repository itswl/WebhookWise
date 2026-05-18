"""Domain-level operational signals built on events and metrics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.observability.events import emit_event
from core.observability.metrics import OBSERVABILITY_SIGNAL_TOTAL


def record_signal(name: str, state: str, attributes: Mapping[str, Any] | None = None) -> None:
    attrs = dict(attributes or {})
    attrs["signal.name"] = name
    attrs["signal.state"] = state
    OBSERVABILITY_SIGNAL_TOTAL.labels(name, state).inc()
    emit_event(f"{name}.{state}", attrs, body=f"{name} signal changed to {state}")
