"""OTel-first observability entrypoints for API, worker, and scheduler."""

from __future__ import annotations

from typing import Any

from core.observability.logging import setup_logging
from core.observability.metrics import setup_metrics
from core.observability.profiling import setup_profiling
from core.observability.tracing import setup_tracing


def setup_observability(app: Any | None = None, *, service_name: str | None = None) -> None:
    setup_tracing(app, service_name=service_name)
    setup_metrics(app)
    setup_logging(service_name=service_name)
    setup_profiling(service_name=service_name)


def setup_observability_worker(*, service_name: str | None = None) -> None:
    setup_observability(None, service_name=service_name)


def setup_observability_scheduler(*, service_name: str | None = None) -> None:
    setup_observability(None, service_name=service_name)
