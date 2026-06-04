"""Best-effort guards for optional observability integrations."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger("webhook_service.observability")


@contextmanager
def suppress_observability_error(operation: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        logger.debug(
            "[OTEL] suppressed observability error operation=%s error_type=%s",
            operation,
            type(exc).__name__,
            exc_info=True,
        )
