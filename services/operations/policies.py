"""Operational policy objects.

This module is the boundary where operations code reads process configuration.
Task runners, pollers and maintenance jobs receive plain values instead of
reaching into ``Config`` directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.config import Config


@dataclass(frozen=True, slots=True)
class TaskRuntimePolicy:
    worker_id: str
    webhook_task_slot_lease_seconds: int
    webhook_task_poll_interval_seconds: float
    max_concurrent_webhook_tasks: int
    recovery_scan_interval_seconds: int
    metrics_refresh_interval_seconds: int
    maintenance_hour: int

    @classmethod
    def from_config(cls, config: Any = Config) -> TaskRuntimePolicy:
        server = config.server
        retry = config.retry
        maintenance = config.maintenance
        return cls(
            worker_id=str(server.WORKER_ID),
            webhook_task_slot_lease_seconds=max(30, int(server.WEBHOOK_TASK_SLOT_LEASE_SECONDS or 0)),
            webhook_task_poll_interval_seconds=max(
                0.05,
                float(retry.PROCESSING_LOCK_POLL_INTERVAL_MS or 100) / 1000,
            ),
            max_concurrent_webhook_tasks=int(server.MAX_CONCURRENT_WEBHOOK_TASKS or 0),
            recovery_scan_interval_seconds=max(
                30,
                int(server.RECOVERY_SCAN_INTERVAL_SECONDS or server.RECOVERY_POLLER_INTERVAL_SECONDS),
            ),
            metrics_refresh_interval_seconds=max(1, int(server.METRICS_REFRESH_INTERVAL_SECONDS or 0)),
            maintenance_hour=max(0, min(23, int(maintenance.MAINTENANCE_HOUR))),
        )


@dataclass(frozen=True, slots=True)
class RecoveryScanPolicy:
    stuck_threshold_seconds: int
    scan_interval_seconds: int
    max_retries: int
    batch_size: int = 50

    @classmethod
    def from_config(
        cls,
        *,
        stuck_threshold_seconds: int | None = None,
        batch_size: int = 50,
        config: Any = Config,
    ) -> RecoveryScanPolicy:
        return cls(
            stuck_threshold_seconds=int(
                stuck_threshold_seconds
                if stuck_threshold_seconds is not None
                else config.server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS
            ),
            scan_interval_seconds=max(1, int(config.server.RECOVERY_SCAN_INTERVAL_SECONDS)),
            max_retries=int(config.retry.WEBHOOK_RETRY_MAX_RETRIES),
            batch_size=batch_size,
        )


@dataclass(frozen=True, slots=True)
class DataMaintenancePolicy:
    enabled: bool
    archive_days_default: int
    retention_policies: Mapping[str, int]
    source_retention_policies: Mapping[str, int]
    cleanup_keywords: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_config(cls, config: Any = Config) -> DataMaintenancePolicy:
        maintenance = config.maintenance
        return cls(
            enabled=bool(maintenance.ENABLE_ARCHIVE_CLEANUP),
            archive_days_default=int(maintenance.ARCHIVE_DAYS_DEFAULT),
            retention_policies=dict(maintenance.RETENTION_POLICIES),
            source_retention_policies=dict(maintenance.SOURCE_RETENTION_POLICIES),
            cleanup_keywords={
                str(field): tuple(str(keyword) for keyword in keywords)
                for field, keywords in maintenance.CLEANUP_KEYWORDS.items()
            },
        )


@dataclass(frozen=True, slots=True)
class MetricsPollPolicy:
    stuck_threshold_seconds: int
    webhook_mq_queue: str
    webhook_mq_consumer_group: str

    @classmethod
    def from_config(cls, config: Any = Config) -> MetricsPollPolicy:
        server = config.server
        return cls(
            stuck_threshold_seconds=int(server.RECOVERY_POLLER_STUCK_THRESHOLD_SECONDS),
            webhook_mq_queue=str(server.WEBHOOK_MQ_QUEUE),
            webhook_mq_consumer_group=str(server.WEBHOOK_MQ_CONSUMER_GROUP),
        )


@dataclass(frozen=True, slots=True)
class DeadLetterNotificationPolicy:
    target_url: str

    @classmethod
    def from_config(cls, config: Any = Config) -> DeadLetterNotificationPolicy:
        return cls(target_url=str(config.ai.FORWARD_URL or ""))


@dataclass(frozen=True, slots=True)
class FeishuNotificationPolicy:
    timeout_seconds: int

    @classmethod
    def from_config(cls, config: Any = Config) -> FeishuNotificationPolicy:
        return cls(timeout_seconds=max(1, int(config.ai.FEISHU_WEBHOOK_TIMEOUT)))
