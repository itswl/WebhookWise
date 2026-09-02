from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any, cast

from core import redis_client
from core.logger import get_logger
from core.redis_client import coerce_int, coerce_str, record_redis_operation

logger = get_logger("redis_streams")


async def redis_xlen(stream: str) -> int:
    raw = await record_redis_operation("xlen", redis_client.get_redis().xlen(stream))
    return coerce_int(raw)


# Per-process backlog cache for the ingress backpressure gate: the hot ingress
# path must not pay a Redis round trip per request, and a slightly stale value
# is fine for a high-water check. Under a burst, at most one probe per stream
# per TTL window is issued (a concurrent-refresh race just does a couple extra,
# both harmless). Returns None when never populated / on error, so callers fail
# open. The metric is the UNCONSUMED backlog (undelivered lag + un-acked
# pending) — the set actually at risk when the stream trims — not total XLEN,
# which sits near MAXLEN permanently on any busy stream once trimming kicks in.
_BACKLOG_CACHE: dict[tuple[str, str], tuple[float, int]] = {}


async def redis_group_backlog_cached(stream: str, group: str, *, ttl_seconds: float = 2.0) -> int | None:
    now = time.monotonic()
    key = (stream, group)
    cached = _BACKLOG_CACHE.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    try:
        pending = await redis_xpending_pending(stream, group)
        lag = await redis_xinfo_group_lag(stream, group)
        value = int(pending) + int(lag)
    except Exception:  # noqa: BLE001 - a backlog probe failure must fail open, never block ingress
        return cached[1] if cached is not None else None
    _BACKLOG_CACHE[key] = (now + ttl_seconds, value)
    return value


def _reset_backlog_cache_for_tests() -> None:
    _BACKLOG_CACHE.clear()


async def redis_xpending_pending(stream: str, group: str) -> int:
    raw = await record_redis_operation(
        "xpending",
        cast(Awaitable[object], cast(Any, redis_client.get_redis()).xpending(stream, group)),
    )
    if isinstance(raw, dict):
        try:
            return int(raw.get("pending") or 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(raw, (list, tuple)) and raw:
        try:
            return int(raw[0] or 0)
        except (TypeError, ValueError, IndexError):
            return 0
    return 0


# A consumer is registered in the group by name and never removed, so every
# worker restart leaves a corpse behind: production measured 127 consumers in
# `webhook-processors`, one of them alive. XINFO CONSUMERS is O(n) in that
# count, and it is on the queue-health path.
#
# One full day is far longer than any deploy gap, and the threshold is a
# constant rather than a setting on purpose: the only way this can hurt is by
# being set too LOW (deleting a consumer whose messages are still pending), and
# an operator has no reason to want that.
_CONSUMER_IDLE_REAP_MS = 24 * 60 * 60 * 1000


async def reap_idle_stream_consumers(stream: str, group: str, *, keep: str = "") -> int:
    """Delete consumers of ``group`` idle past a day with nothing pending.

    Returns how many were removed; 0 covers "none were stale" and "Redis could
    not tell us", which are the same thing to the caller. Every Redis error is
    swallowed: this is startup hygiene, and a worker that cannot start because a
    cleanup failed is strictly worse than a group with extra dead names in it.

    Two guards make the deletion safe:

    * ``pending > 0`` is skipped. Deleting such a consumer discards its
      pending-entry list, and those messages then need XAUTOCLAIM to be
      redelivered — the reaper would be dropping work.
    * ``idle`` — not ``inactive`` — is the liveness signal. ``idle`` counts from
      the last ATTEMPTED read, so a live worker blocking on an empty stream
      keeps it near zero; ``inactive`` counts from the last SUCCESSFUL read and
      grows without bound on a quiet queue, which would make the reaper delete
      the workers that are running. Redis < 7.2 omits ``inactive`` entirely,
      another reason not to depend on it.
    """
    try:
        raw = await record_redis_operation(
            "xinfo_consumers",
            cast(Awaitable[object], cast(Any, redis_client.get_redis()).xinfo_consumers(stream, group)),
        )
    except Exception:  # noqa: BLE001 - no group yet, no stream yet, or no Redis: nothing to reap
        return 0
    if not isinstance(raw, list):
        return 0

    removed = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # coerce_str, not str(): a client built without decode_responses hands
        # back bytes, and str(b"gw-1") would never match `keep`.
        name = coerce_str(entry.get("name")) or ""
        if not name or name == keep:
            continue
        if coerce_int(entry.get("pending"), default=1) > 0:
            continue
        # A reply without `idle` is one we cannot judge; treat it as live.
        if coerce_int(entry.get("idle"), default=0) <= _CONSUMER_IDLE_REAP_MS:
            continue
        try:
            await record_redis_operation(
                "xgroup_delconsumer",
                cast(
                    Awaitable[object],
                    cast(Any, redis_client.get_redis()).xgroup_delconsumer(stream, group, name),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - one stubborn consumer must not strand the rest
            # Say which one, at debug: a reap that silently skips is
            # indistinguishable from a reap that found nothing to do.
            logger.debug("[Redis] Could not reap idle consumer name=%s group=%s: %s", name, group, exc)
            continue
        removed += 1
    return removed


async def redis_xinfo_group_lag(stream: str, group: str) -> int:
    raw = await record_redis_operation(
        "xinfo_groups",
        cast(Awaitable[object], cast(Any, redis_client.get_redis()).xinfo_groups(stream)),
    )
    if not isinstance(raw, list):
        return 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") == group:
            try:
                return int(item.get("lag") or 0)
            except (TypeError, ValueError):
                return 0
    return 0
