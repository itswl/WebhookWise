from __future__ import annotations

import contextlib
import inspect
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeAlias, cast

import redis.asyncio as redis

from core.config import UnifiedConfigManager
from core.logger import get_logger, mask_url

logger = get_logger("redis_client")

if TYPE_CHECKING:
    RedisClient: TypeAlias = redis.Redis[Any]  # type: ignore[type-arg, unused-ignore]
else:
    RedisClient = redis.Redis


def _resolve_config(config: UnifiedConfigManager | None) -> UnifiedConfigManager:
    if config is not None:
        return config
    from core.app_context import get_default_config

    return get_default_config()


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
    """Return the Redis client owned by the current AppContext."""
    from core.app_context import get_or_create_default_app_context

    context = get_or_create_default_app_context()
    return context.ensure_redis_client()


async def _await_if_needed(value: object) -> None:
    if inspect.isawaitable(value):
        await cast(Awaitable[object], value)


async def dispose_redis() -> None:
    """Close the Redis client owned by the current AppContext."""
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
