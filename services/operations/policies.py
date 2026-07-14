"""Operational policy objects built from static configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.app_context import get_config_manager


@dataclass(frozen=True, slots=True)
class TaskRuntimePolicy:
    worker_id: str
    background_scan_interval_seconds: int
    metrics_refresh_interval_seconds: int
    maintenance_hour: int

    @classmethod
    def from_config(cls) -> TaskRuntimePolicy:
        cfg = get_config_manager()
        return cls(
            worker_id=str(cfg.server.WORKER_ID),
            background_scan_interval_seconds=max(30, int(cfg.tasks.BACKGROUND_SCAN_INTERVAL_SECONDS or 0)),
            metrics_refresh_interval_seconds=max(1, int(cfg.tasks.METRICS_REFRESH_INTERVAL_SECONDS or 0)),
            maintenance_hour=max(0, min(23, int(cfg.maintenance.MAINTENANCE_HOUR))),
        )


@dataclass(frozen=True, slots=True)
class DataMaintenancePolicy:
    enabled: bool
    retention_days_default: int
    retention_policies: Mapping[str, int]
    source_retention_policies: Mapping[str, int]
    cleanup_keywords: Mapping[str, tuple[str, ...]]
    archive_retention_days: int = 90
    terminal_outbox_retention_days: int = 30
    ai_usage_retention_days: int = 90
    incident_auto_close_days: int = 7

    @classmethod
    def from_config(cls) -> DataMaintenancePolicy:
        cfg = get_config_manager().maintenance
        return cls(
            enabled=bool(cfg.ENABLE_DATA_CLEANUP),
            retention_days_default=int(cfg.DATA_RETENTION_DAYS_DEFAULT),
            retention_policies=dict(cfg.RETENTION_POLICIES),
            source_retention_policies=dict(cfg.SOURCE_RETENTION_POLICIES),
            cleanup_keywords={
                str(field): tuple(str(keyword) for keyword in keywords)
                for field, keywords in cfg.CLEANUP_KEYWORDS.items()
            },
            archive_retention_days=int(cfg.ARCHIVE_RETENTION_DAYS),
            terminal_outbox_retention_days=int(cfg.TERMINAL_OUTBOX_RETENTION_DAYS),
            ai_usage_retention_days=int(cfg.AI_USAGE_RETENTION_DAYS),
            incident_auto_close_days=int(cfg.INCIDENT_AUTO_CLOSE_DAYS),
        )
