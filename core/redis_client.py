from __future__ import annotations

import contextlib
import hashlib
import inspect
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, cast

import redis.asyncio as redis
from redis.exceptions import NoScriptError, RedisError

from core import json
from core.config import AppConfig
from core.logger import get_logger, mask_url

logger = get_logger("redis_client")

# Cache of script SHA1 -> source so we can run scripts via EVALSHA (sending the
# short hash) instead of EVAL (re-sending the full Lua source on every call).
_SCRIPT_SHA_CACHE: dict[str, str] = {}


def _script_sha(script: str) -> str:
    sha = _SCRIPT_SHA_CACHE.get(script)
    if sha is None:
        # Redis identifies cached scripts by their SHA1 (EVALSHA). This is a
        # content id, not a security hash — usedforsecurity=False documents that
        # and satisfies the SHA1 linters.
        sha = hashlib.sha1(script.encode("utf-8"), usedforsecurity=False).hexdigest()
        _SCRIPT_SHA_CACHE[script] = sha
    return sha


async def _eval_script(script: str, numkeys: int, args: tuple[RedisEvalArg, ...]) -> object:
    """Run a Lua script via EVALSHA, transparently loading it on NOSCRIPT.

    Avoids shipping the full Lua source on every call (the EVAL behaviour). The
    SHA is derived locally; on first use (or after a Redis SCRIPT FLUSH) Redis
    raises NOSCRIPT and we fall back to EVAL, which also caches the script
    server-side for subsequent EVALSHA hits.
    """
    client = cast(Any, get_redis())
    sha = _script_sha(script)
    try:
        return await client.evalsha(sha, int(numkeys), *args)
    except NoScriptError:
        return await client.eval(script, int(numkeys), *args)

if TYPE_CHECKING:
    type RedisClient = redis.Redis[Any]  # type: ignore[type-arg, unused-ignore]
else:
    RedisClient = redis.Redis

RedisEvalArg = bytes | bytearray | str | int | float | memoryview
_REDIS_CLOSE_ERRORS = (AttributeError, OSError, RedisError, RuntimeError, TimeoutError, TypeError, ValueError)


def build_redis_client(config: AppConfig | None = None) -> RedisClient:
    if config is None:
        from core.app_context import get_config_manager

        config = get_config_manager()
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


async def dispose_redis() -> None:
    from core.app_context import get_default_app_context

    context = get_default_app_context()
    if context is not None and context.redis_client is not None:
        client = context.redis_client
        context.redis_client = None
        await close_redis_client(client)
        logger.info("[Redis] 当前上下文连接池已关闭")


async def close_redis_client(client: RedisClient) -> None:
    try:
        close_fn = getattr(client, "aclose", None) or getattr(client, "close", None)
        if callable(close_fn):
            result = close_fn()
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)
    except _REDIS_CLOSE_ERRORS as exc:
        logger.debug("[Redis] close suppressed error_type=%s", type(exc).__name__, exc_info=True)
    with contextlib.suppress(AttributeError):
        pool = getattr(client, "connection_pool", None)
        disconnect_fn = getattr(pool, "disconnect", None)
        if callable(disconnect_fn):
            result = disconnect_fn()
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)


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
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode("utf-8")
    if isinstance(raw, str):
        return raw
    return str(raw)


async def record_redis_operation[T](operation: str, awaitable: Awaitable[T]) -> T:
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
    except (RedisError, RuntimeError, TimeoutError, OSError, TypeError, ValueError) as e:
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
    raw = await record_redis_operation("eval", _eval_script(script, numkeys, args))
    return parse_int(raw)


async def redis_eval_str(script: str, numkeys: int, *args: RedisEvalArg) -> str | None:
    raw = await record_redis_operation("eval", _eval_script(script, numkeys, args))
    return coerce_str(raw)


async def redis_eval_int_list(script: str, numkeys: int, *args: RedisEvalArg) -> list[int]:
    raw = await record_redis_operation("eval", _eval_script(script, numkeys, args))
    if not isinstance(raw, (list, tuple)):
        return []
    return [coerce_int(item) for item in raw]


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


# INCR then (re)set TTL in a single round-trip instead of INCR + EXPIRE.
_INCR_WITH_EXPIRE_LUA = """
local v = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
return v
"""


async def redis_incr_with_expire(key: str, ttl_seconds: int) -> int:
    val = await redis_eval_int(_INCR_WITH_EXPIRE_LUA, 1, key, int(ttl_seconds))
    return coerce_int(val)


async def redis_ping() -> bool:
    try:
        raw = await record_redis_operation("ping", cast(Awaitable[object], get_redis().ping()))
        return bool(raw)
    except (RedisError, OSError, TimeoutError, RuntimeError) as e:
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
    "redis_eval_int_list",
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
