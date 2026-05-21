"""Webhook ingress, pipeline, noise, and lifecycle metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Gauge, Histogram

WEBHOOK_RECEIVED_TOTAL = Counter(
    "webhook.received",
    "Total number of webhooks received",
    ("webhook.source", "webhook.status"),
)
WEBHOOK_INGRESS_PAYLOAD_BYTES = Histogram(
    "webhook.ingress.payload.size",
    "Webhook ingress payload size",
    ("webhook.source", "webhook.outcome"),
    unit="By",
)
WEBHOOK_PROCESSING_STATUS_TOTAL = Counter(
    "webhook.processed",
    "Webhook processing status transitions total",
    ("webhook.status",),
)
WEBHOOK_PIPELINE_STEP_TOTAL = Counter(
    "webhook.pipeline.steps",
    "Webhook pipeline step count",
    ("pipeline.step", "webhook.source", "webhook.outcome"),
)
WEBHOOK_PIPELINE_STEP_DURATION_SECONDS = Histogram(
    "webhook.pipeline.step.duration",
    "Webhook pipeline step duration",
    ("pipeline.step", "webhook.source", "webhook.outcome"),
    unit="s",
)
WEBHOOK_PROCESSING_DURATION_SECONDS = Histogram(
    "webhook.processing.duration",
    "Time spent from start of pipeline to finish",
    ("webhook.source", "webhook.outcome"),
    unit="s",
)
WEBHOOK_NOISE_REDUCED_TOTAL = Counter(
    "webhook.suppressed",
    "Number of webhooks evaluated by noise reduction",
    ("webhook.source", "webhook.relation", "webhook.suppressed"),
)
WEBHOOK_NOISE_EVALUATIONS_TOTAL = Counter(
    "webhook.noise.evaluations",
    "Noise-reduction evaluation count",
    ("webhook.source", "webhook.relation", "webhook.suppressed"),
)
WEBHOOK_NOISE_EVALUATION_DURATION_SECONDS = Histogram(
    "webhook.noise.evaluation.duration",
    "Noise-reduction evaluation duration",
    ("webhook.source", "webhook.relation", "webhook.suppressed"),
    unit="s",
)
ALERT_NUMERIC_PARSE_FAILURE_TOTAL = Counter(
    "webhook.parse.failures",
    "Alert numeric field parse failures during rule analysis",
    ("webhook.source", "webhook.field", "error.reason"),
)
WEBHOOK_SEMAPHORE_TIMEOUT_TOTAL = Counter(
    "webhook.semaphore.timeouts",
    "Semaphore acquisition timeout count",
)
WEBHOOK_STORM_SUPPRESSED_TOTAL = Counter(
    "webhook.storm.suppressed",
    "Webhook storm fail-fast suppression count",
    ("webhook.source",),
)
WEBHOOK_RECOVERY_POLLED_TOTAL = Counter("webhook.recovery.polled", "Recovered zombie event count")
WEBHOOK_RUNNING_TASKS = Gauge("webhook.running_tasks", "Currently running webhook processing tasks")
WEBHOOK_DEAD_LETTER_TOTAL = Counter("webhook.dead_letter", "Non-retryable dead letter event count")
WEBHOOK_PROCESSING_STATUS_COUNT = Gauge(
    "webhook.processing.status_count",
    "Webhook event count by processing status",
    ("webhook.status",),
)
WEBHOOK_STUCK_STATUS_COUNT = Gauge(
    "webhook.stuck.status_count",
    "Stuck webhook event count by processing status",
    ("webhook.status",),
)
