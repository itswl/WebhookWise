from __future__ import annotations

import contextlib
import inspect
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeAlias, TypeVar, cast

import redis.asyncio as redis

from core import json
from core.config import UnifiedConfigManager
from core.logger import get_logger, mask_url

logger = get_logger("redis_client")

if TYPE_CHECKING:
    RedisClient: TypeAlias = redis.Redis[Any]  # type: ignore[type-arg, unused-ignore]
else:
    RedisClient = redis.Redis

T = TypeVar("T")

RedisEvalArg = bytes | bytearray | str | int | float | memoryview


def _resolve_config(config: UnifiedConfigManager | None) -> UnifiedConfigManager:
    if config is not None:
        return config
    from core.app_context import get_config_manager

    return get_config_manager()


def build_redis_client(config: UnifiedConfigManager | None = None) -> RedisClient:
    config = _resolve_config(config)
    pool: Any = redis.ConnectionPool.from_url(
        config.redis.REDIS_URL,
        decode_responses=True,
        max_connections=100,
        socket_connect_timeout=config.redis.REDIS_SOCKET_CONNECT_TIMEOUT,
        socket_timeout=config.redis.REDIS_SOCKET_TIMEOUT,
        socket_keepalive=True,
        health_check_interval=config.redis.REDIS_HEALTH_CHECK_INTERVAL,
    )
    client = redis.Redis(connection_pool=pool)
    logger.info("[Redis] 成功初始化连接池: %s", mask_url(config.redis.REDIS_URL))
    return client


def get_redis() -> RedisClient:
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is None:
        raise RuntimeError("default AppContext is not initialized")
    return context.ensure_redis_client()


async def _await_if_needed(value: object) -> None:
    if inspect.isawaitable(value):
        await cast(Awaitable[object], value)


async def dispose_redis() -> None:
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is not None and context.redis_client is not None:
        client = context.redis_client
        context.redis_client = None
        await close_redis_client(client)
        logger.info("[Redis] 当前上下文连接池已关闭")


async def close_redis_client(client: RedisClient) -> None:
    with contextlib.suppress(Exception):
        close_fn = getattr(client, "aclose", None) or getattr(client, "close", None)
        if callable(close_fn):
            await _await_if_needed(close_fn())
    with contextlib.suppress(Exception):
        pool = getattr(client, "connection_pool", None)
        disconnect_fn = getattr(pool, "disconnect", None)
        if callable(disconnect_fn):
            await _await_if_needed(disconnect_fn())


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


async def record_redis_operation(operation: str, awaitable: Awaitable[T]) -> T:
    from core.observability.metrics import REDIS_OPERATION_DURATION_SECONDS, REDIS_OPERATIONS_TOTAL
    from core.observability.tracing import otel_span
    from core.redis_health import mark_redis_failure, mark_redis_success

    start = time.perf_counter()
    status = "success"
    try:
        with otel_span(
            "redis.operation",
            {"db.system": "redis", "db.operation": operation, "redis.operation": operation},
        ):
            result = await awaitable
    except Exception as e:
        status = "error"
        mark_redis_failure(operation, e)
        raise
    else:
        mark_redis_success(operation)
        return result
    finally:
        REDIS_OPERATIONS_TOTAL.labels(operation, status).inc()
        REDIS_OPERATION_DURATION_SECONDS.labels(operation, status).observe(time.perf_counter() - start)


async def redis_set_nx_ex(key: str, value: str, ttl_seconds: int) -> bool:
    raw = await record_redis_operation("set_nx_ex", get_redis().set(key, value, nx=True, ex=int(ttl_seconds)))
    return bool(raw)


async def redis_eval_int(script: str, numkeys: int, *args: RedisEvalArg) -> int | None:
    raw = await record_redis_operation(
        "eval",
        cast(Awaitable[object], cast(Any, get_redis()).eval(script, int(numkeys), *args)),
    )
    return parse_int(raw)


async def redis_eval_str(script: str, numkeys: int, *args: RedisEvalArg) -> str | None:
    raw = await record_redis_operation(
        "eval",
        cast(Awaitable[object], cast(Any, get_redis()).eval(script, int(numkeys), *args)),
    )
    return coerce_str(raw)


async def redis_get_str(key: str) -> str | None:
    raw = await record_redis_operation("get", cast(Awaitable[object], get_redis().get(key)))
    return coerce_str(raw)


async def redis_setex_str(key: str, ttl_seconds: int, value: str) -> None:
    await record_redis_operation("setex", get_redis().setex(key, int(ttl_seconds), value))


async def redis_setex_bytes(key: str, ttl_seconds: int, value: bytes) -> None:
    await record_redis_operation("setex", get_redis().setex(key, int(ttl_seconds), value))


async def redis_delete(key: str) -> int:
    raw = await record_redis_operation("delete", get_redis().delete(key))
    return coerce_int(raw)


async def redis_publish(channel: str, message: str) -> int:
    raw = await record_redis_operation("publish", get_redis().publish(channel, message))
    return coerce_int(raw)


async def redis_incr_with_expire(key: str, ttl_seconds: int) -> int:
    val = coerce_int(await record_redis_operation("incr", get_redis().incr(key)))
    await record_redis_operation("expire", get_redis().expire(key, int(ttl_seconds)))
    return val


async def redis_ping() -> bool:
    try:
        raw = await record_redis_operation("ping", cast(Awaitable[object], get_redis().ping()))
        return bool(raw)
    except Exception as e:
        logger.warning("[Redis] ping 失败: %s", e)
        return False


async def redis_get_json_dict(key: str) -> dict[str, Any] | None:
    raw = await redis_get_str(key)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


async def redis_setex_json(key: str, ttl_seconds: int, payload: dict[str, Any]) -> None:
    await redis_setex_str(key, ttl_seconds, json.dumps(payload))

__all__ = [
    "RedisClient",
    "RedisEvalArg",
    "build_redis_client",
    "close_redis_client",
    "coerce_int",
    "coerce_str",
    "dispose_redis",
    "get_redis",
    "parse_int",
    "record_redis_operation",
    "redis_delete",
    "redis_eval_int",
    "redis_eval_str",
    "redis_get_json_dict",
    "redis_get_str",
    "redis_incr_with_expire",
    "redis_ping",
    "redis_publish",
    "redis_set_nx_ex",
    "redis_setex_bytes",
    "redis_setex_json",
    "redis_setex_str",
]
