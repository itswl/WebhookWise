from __future__ import annotations

import contextlib
from collections.abc import Awaitable
from typing import Any, cast

from core import redis_lifecycle
from core.logger import get_logger
from core.redis_metrics import record_redis_operation

logger = get_logger("redis_client")

RedisEvalArg = bytes | bytearray | str | int | float | memoryview


def parse_int(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return None


def coerce_int(raw: object, default: int = 0) -> int:
    parsed = parse_int(raw)
    return default if parsed is None else parsed


def coerce_str(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        with contextlib.suppress(Exception):
            return raw.decode("utf-8")
    if isinstance(raw, str):
        return raw
    return str(raw)


async def redis_set_nx_ex(key: str, value: str, ttl_seconds: int) -> bool:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation("set_nx_ex", r.set(key, value, nx=True, ex=int(ttl_seconds)))
    return bool(raw)


async def redis_eval_int(script: str, numkeys: int, *args: RedisEvalArg) -> int | None:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation(
        "eval",
        cast(Awaitable[object], cast(Any, r).eval(script, int(numkeys), *args)),
    )
    return parse_int(raw)


async def redis_eval_str(script: str, numkeys: int, *args: RedisEvalArg) -> str | None:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation(
        "eval",
        cast(Awaitable[object], cast(Any, r).eval(script, int(numkeys), *args)),
    )
    return coerce_str(raw)


async def redis_get_str(key: str) -> str | None:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation("get", cast(Awaitable[object], r.get(key)))
    return coerce_str(raw)


async def redis_setex_str(key: str, ttl_seconds: int, value: str) -> None:
    r = redis_lifecycle.get_redis()
    await record_redis_operation("setex", r.setex(key, int(ttl_seconds), value))


async def redis_setex_bytes(key: str, ttl_seconds: int, value: bytes) -> None:
    r = redis_lifecycle.get_redis()
    await record_redis_operation("setex", r.setex(key, int(ttl_seconds), value))


async def redis_delete(key: str) -> int:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation("delete", r.delete(key))
    return coerce_int(raw)


async def redis_publish(channel: str, message: str) -> int:
    r = redis_lifecycle.get_redis()
    raw = await record_redis_operation("publish", r.publish(channel, message))
    return coerce_int(raw)


async def redis_incr_with_expire(key: str, ttl_seconds: int) -> int:
    r = redis_lifecycle.get_redis()
    val = coerce_int(await record_redis_operation("incr", r.incr(key)))
    await record_redis_operation("expire", r.expire(key, int(ttl_seconds)))
    return val


async def redis_ping() -> bool:
    try:
        raw = await record_redis_operation("ping", cast(Awaitable[object], redis_lifecycle.get_redis().ping()))
        return bool(raw)
    except Exception as e:
        logger.warning("[Redis] ping 失败: %s", e)
        return False
