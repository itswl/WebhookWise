"""Operational policy objects.

This module is the boundary where operations code reads process configuration.
Task runners, pollers and maintenance jobs receive plain values instead of
reaching into configuration globals directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.app_context import get_default_config


@dataclass(frozen=True, slots=True)
class TaskRuntimePolicy:
    worker_id: str
    webhook_task_slot_lease_seconds: int
    webhook_task_poll_interval_seconds: float
    max_concurrent_webhook_tasks: int
    background_scan_interval_seconds: int
    metrics_refresh_interval_seconds: int
    maintenance_hour: int

    @classmethod
    def from_config(cls, config: Any | None = None) -> TaskRuntimePolicy:
        config = config or get_default_config()
        server = config.server
        tasks = config.tasks
        retry = config.retry
        maintenance = config.maintenance
        return cls(
            worker_id=str(server.WORKER_ID),
            webhook_task_slot_lease_seconds=max(30, int(tasks.WEBHOOK_TASK_SLOT_LEASE_SECONDS or 0)),
            webhook_task_poll_interval_seconds=max(
                0.05,
                float(retry.PROCESSING_LOCK_POLL_INTERVAL_MS or 100) / 1000,
            ),
            max_concurrent_webhook_tasks=int(tasks.MAX_CONCURRENT_WEBHOOK_TASKS or 0),
            background_scan_interval_seconds=max(30, int(tasks.BACKGROUND_SCAN_INTERVAL_SECONDS or 0)),
            metrics_refresh_interval_seconds=max(1, int(tasks.METRICS_REFRESH_INTERVAL_SECONDS or 0)),
            maintenance_hour=max(0, min(23, int(maintenance.MAINTENANCE_HOUR))),
        )


@dataclass(frozen=True, slots=True)
class DataMaintenancePolicy:
    enabled: bool
    retention_days_default: int
    retention_policies: Mapping[str, int]
    source_retention_policies: Mapping[str, int]
    cleanup_keywords: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_config(cls, config: Any | None = None) -> DataMaintenancePolicy:
        config = config or get_default_config()
        maintenance = config.maintenance
        return cls(
            enabled=bool(maintenance.ENABLE_DATA_CLEANUP),
            retention_days_default=int(maintenance.DATA_RETENTION_DAYS_DEFAULT),
            retention_policies=dict(maintenance.RETENTION_POLICIES),
            source_retention_policies=dict(maintenance.SOURCE_RETENTION_POLICIES),
            cleanup_keywords={
                str(field): tuple(str(keyword) for keyword in keywords)
                for field, keywords in maintenance.CLEANUP_KEYWORDS.items()
            },
        )

