"""Forwarding and outbox metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Gauge, Histogram

FORWARD_DELIVERY_TOTAL = Counter(
    "forward.delivery",
    "Forward delivery attempt count",
    ("forward.target_type", "forward.status"),
)
FORWARD_DELIVERY_DURATION_SECONDS = Histogram(
    "forward.delivery.duration",
    "Forward delivery attempt duration",
    ("forward.target_type", "forward.status"),
    unit="s",
)
FORWARD_OUTBOX_RECORDS_TOTAL = Counter(
    "forward.outbox.records",
    "Forwarding outbox lifecycle count",
    ("forward.target_type", "forward.status"),
)
FORWARD_OUTBOX_PROCESS_DURATION_SECONDS = Histogram(
    "forward.outbox.process.duration",
    "Forwarding outbox processing duration",
    ("forward.target_type", "forward.status"),
    unit="s",
)
FORWARD_OUTBOX_BACKLOG_AGE_SECONDS = Gauge(
    "forward.outbox.backlog.age",
    "Age of the oldest active forwarding outbox record",
    ("forward.target_type", "forward.status"),
    unit="s",
)
