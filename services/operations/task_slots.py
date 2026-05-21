"""Redis-backed global task slot management."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.redis_lua import (
    TASK_SLOT_ACQUIRE,
    TASK_SLOT_RELEASE,
    TASK_SLOT_RENEW,
)


def slot_times(lease_seconds: int) -> tuple[int, int, int]:
    now_ms = int(time.time() * 1000)
    lease_ms = int(lease_seconds * 1000)
    # Keep the key slightly longer than one member lease so Redis can clean old slots on the next acquire.
    key_ttl_ms = lease_ms + 30_000
    return now_ms, now_ms + lease_ms, key_ttl_ms


@dataclass(frozen=True, slots=True)
class TaskSlotManager:
    key: str
    eval_int: Callable[..., Awaitable[int | None]]
    logger: logging.Logger

    async def acquire(self, token: str, limit: int, lease_seconds: int) -> bool:
        now_ms, expires_at_ms, key_ttl_ms = slot_times(lease_seconds)
        acquired = await self.eval_int(
            TASK_SLOT_ACQUIRE,
            1,
            self.key,
            now_ms,
            limit,
            expires_at_ms,
            token,
            key_ttl_ms,
        )
        return acquired == 1

    async def renew_until_cancelled(self, token: str, lease_seconds: int) -> None:
        interval = max(1.0, lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            _now_ms, expires_at_ms, key_ttl_ms = slot_times(lease_seconds)
            renewed = await self.eval_int(
                TASK_SLOT_RENEW,
                1,
                self.key,
                token,
                expires_at_ms,
                key_ttl_ms,
            )
            if renewed != 1:
                self.logger.warning("[Tasks] Redis 全局并发令牌续期失败，可能已失去 slot token=%s", token)
                return

    async def release(self, token: str) -> None:
        await self.eval_int(TASK_SLOT_RELEASE, 1, self.key, token)
