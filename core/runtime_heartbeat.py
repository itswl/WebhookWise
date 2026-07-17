"""Redis-backed liveness heartbeat for non-HTTP runtime processes."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time

from core.logger import get_logger
from core.redis_client import redis_delete, redis_get_str, redis_setex_str

logger = get_logger("runtime_heartbeat")

_tasks: dict[str, asyncio.Task[None]] = {}


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def heartbeat_interval_seconds() -> int:
    return _positive_int_env("RUNTIME_HEARTBEAT_INTERVAL_SECONDS", 10)


def heartbeat_ttl_seconds() -> int:
    return max(heartbeat_interval_seconds() * 3, _positive_int_env("RUNTIME_HEARTBEAT_TTL_SECONDS", 45))


def runtime_heartbeat_key(role: str, *, hostname: str | None = None) -> str:
    normalized_role = role.strip().lower()
    if normalized_role not in {"worker", "scheduler"}:
        raise ValueError(f"Unsupported runtime heartbeat role: {role}")
    node = (hostname or socket.gethostname()).strip().lower() or "unknown"
    return f"webhookwise:runtime-heartbeat:{normalized_role}:{node}"


async def _write_heartbeat(role: str) -> None:
    await redis_setex_str(runtime_heartbeat_key(role), heartbeat_ttl_seconds(), str(time.time()))


async def start_runtime_heartbeat(role: str) -> None:
    """Start one heartbeat loop for the current container and runtime role."""
    existing = _tasks.get(role)
    if existing is not None and not existing.done():
        return

    await _write_heartbeat(role)

    async def _run() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval_seconds())
            try:
                await _write_heartbeat(role)
            except Exception:  # noqa: BLE001 - TTL expiry is the failure signal; the process must stay observable
                logger.warning("[Heartbeat] Failed to refresh role=%s", role, exc_info=True)

    task = asyncio.create_task(_run(), name=f"runtime-heartbeat-{role}")
    _tasks[role] = task

    def _done(completed: asyncio.Task[None]) -> None:
        if _tasks.get(role) is completed:
            _tasks.pop(role, None)
        if not completed.cancelled() and completed.exception() is not None:
            logger.error("[Heartbeat] Loop stopped unexpectedly role=%s error=%s", role, completed.exception())

    task.add_done_callback(_done)


async def stop_runtime_heartbeat(role: str) -> None:
    """Stop the loop and remove its key on graceful shutdown."""
    task = _tasks.pop(role, None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    with contextlib.suppress(Exception):
        await redis_delete(runtime_heartbeat_key(role))


async def runtime_heartbeat_is_fresh(role: str) -> bool:
    """Return whether the role has refreshed its heartbeat within the TTL."""
    raw = await redis_get_str(runtime_heartbeat_key(role))
    if not raw:
        return False
    try:
        age = time.time() - float(raw)
    except ValueError:
        return False
    return -heartbeat_interval_seconds() <= age <= heartbeat_ttl_seconds()
