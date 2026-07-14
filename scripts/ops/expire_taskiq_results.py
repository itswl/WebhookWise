#!/usr/bin/env python3
"""Apply a bounded TTL to legacy TaskIQ result keys.

The command is dry-run by default. It only touches bare 32-character
hexadecimal keys, which is the result-key format used by TaskIQ in this
deployment, and never deletes them immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections.abc import AsyncIterator

from redis.asyncio import Redis

_TASKIQ_RESULT_KEY = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def is_taskiq_result_key(key: str) -> bool:
    return bool(_TASKIQ_RESULT_KEY.fullmatch(key))


async def _scan_keys(redis: Redis[bytes], *, scan_count: int) -> AsyncIterator[str]:
    async for raw_key in redis.scan_iter(match="*", count=scan_count):
        key = raw_key.decode("utf-8", errors="ignore") if isinstance(raw_key, bytes) else str(raw_key)
        if is_taskiq_result_key(key):
            yield key


async def expire_legacy_results(
    redis: Redis[bytes],
    *,
    ttl_seconds: int,
    scan_count: int,
    apply: bool,
    pipeline_size: int = 1000,
) -> tuple[int, int]:
    """Return ``(matched, changed)`` while preserving existing shorter TTLs."""
    matched = 0
    changed = 0
    batch: list[str] = []

    async def flush() -> None:
        nonlocal changed
        if not batch or not apply:
            batch.clear()
            return
        pipe = redis.pipeline(transaction=False)
        for key in batch:
            pipe.expire(key, ttl_seconds, nx=True)
        results = await pipe.execute()
        changed += sum(1 for result in results if result)
        batch.clear()

    async for key in _scan_keys(redis, scan_count=scan_count):
        matched += 1
        batch.append(key)
        if len(batch) >= pipeline_size:
            await flush()
    await flush()
    return matched, changed


async def _run(args: argparse.Namespace) -> int:
    redis_url = args.redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis = Redis.from_url(redis_url, decode_responses=False)
    try:
        matched, changed = await expire_legacy_results(
            redis,
            ttl_seconds=args.ttl_seconds,
            scan_count=args.scan_count,
            apply=args.apply,
        )
    finally:
        await redis.aclose()
    mode = "applied" if args.apply else "dry-run"
    print(f"mode={mode} matched={matched} changed={changed} ttl_seconds={args.ttl_seconds}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default="", help="Redis URL; defaults to REDIS_URL")
    parser.add_argument("--ttl-seconds", type=int, default=86400)
    parser.add_argument("--scan-count", type=int, default=5000)
    parser.add_argument("--apply", action="store_true", help="Apply EXPIRE NX; otherwise only count matches")
    args = parser.parse_args()
    if args.ttl_seconds <= 0 or args.scan_count <= 0:
        parser.error("--ttl-seconds and --scan-count must be positive")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
