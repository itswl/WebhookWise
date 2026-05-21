"""Security decision metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter

SECURITY_CHECKS_TOTAL = Counter(
    "security.checks",
    "Security check decision count",
    ("security.check", "security.result"),
)
