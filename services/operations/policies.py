"""Operational policy objects and concurrency primitives.

This module is the boundary where operations code reads process configuration
and manages Redis-backed concurrency slots.
"""

from __future__ import annotations

import time as time_mod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.app_context import get_config_manager
from core.logger import get_logger
from core.redis_client import redis_eval_int

logger = get_logger("operations.policies")


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
        config = config or get_config_manager()
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
        config = config or get_config_manager()
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


_ACQUIRE_SLOT_LUA = """
local slot_key = KEYS[1]
local ttl = tonumber(ARGV[1])
local max_slots = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', slot_key, '-inf', now)
local current = redis.call('ZCARD', slot_key)
if current >= max_slots then
    return 0
end
local member = now .. ':' .. tostring(current + 1)
redis.call('ZADD', slot_key, now + ttl, member)
redis.call('EXPIRE', slot_key, ttl * 3)
return 1
"""

_RELEASE_SLOT_LUA = """
local slot_key = KEYS[1]
local member = ARGV[1]
redis.call('ZREM', slot_key, member)
return 1
"""


class TaskSlotManager:
    def __init__(self, slot_name: str) -> None:
        self._slot_key = f"task_slot:{slot_name}"
        self._policy: TaskRuntimePolicy | None = None

    def _get_policy(self) -> TaskRuntimePolicy:
        if self._policy is None:
            self._policy = TaskRuntimePolicy.from_config()
        return self._policy

    async def acquire(self) -> str | None:
        policy = self._get_policy()
        if policy.max_concurrent_webhook_tasks <= 0:
            return "unlimited"
        try:
            now = int(time_mod.time())
            result = await redis_eval_int(
                _ACQUIRE_SLOT_LUA,
                1,
                self._slot_key,
                str(policy.webhook_task_slot_lease_seconds),
                str(policy.max_concurrent_webhook_tasks),
                str(now),
            )
            if result and result > 0:
                return f"{now}:{result}"
            return None
        except Exception as e:
            logger.warning("[TaskSlotManager] acquire slot=%s failed: %s", self._slot_key, e)
            return "failopen"

    async def release(self, member: str) -> None:
        try:
            await redis_eval_int(_RELEASE_SLOT_LUA, 1, self._slot_key, member)
        except Exception as e:
            logger.warning("[TaskSlotManager] release slot=%s failed: %s", self._slot_key, e)
