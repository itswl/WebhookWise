"""Scheduler and worker task metrics."""

from __future__ import annotations

from core.observability.metrics.base import Counter, Gauge, Histogram

SCHEDULED_TASK_RUNS_TOTAL = Counter(
    "scheduler.task.runs",
    "Scheduled task execution count",
    ("scheduler.task.name", "scheduler.task.status"),
)
SCHEDULED_TASK_LAST_SUCCESS_UNIXTIME = Gauge(
    "scheduler.task.last_success_unixtime",
    "Last successful scheduled task execution unix time",
    ("scheduler.task.name",),
    unit="s",
)
SCHEDULED_TASK_LAG_SECONDS = Gauge(
    "scheduler.task.lag",
    "Scheduled task lag relative to its expected interval",
    ("scheduler.task.name",),
    unit="s",
)
SCHEDULED_TASK_DURATION_SECONDS = Histogram(
    "scheduler.task.duration",
    "Scheduled task duration",
    ("scheduler.task.name",),
    unit="s",
)
WORKER_TASKS_TOTAL = Counter(
    "worker.task.runs",
    "Worker task execution count",
    ("worker.task.name", "worker.task.status"),
)
WORKER_TASK_DURATION_SECONDS = Histogram(
    "worker.task.duration",
    "Worker task execution duration",
    ("worker.task.name", "worker.task.status"),
    unit="s",
)
