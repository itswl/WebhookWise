import contextlib
import inspect
import json
from collections.abc import Awaitable
from typing import Any, TypeVar, cast

import redis.asyncio as redis
from redis.asyncio.client import PubSub

from core.config import Config
from core.logger import logger, mask_url

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """获取全局 Redis 客户端单例"""
    global _redis_client
    if _redis_client is None:
        pool = redis.ConnectionPool.from_url(
            Config.redis.REDIS_URL,
            decode_responses=True,
            max_connections=100,
            socket_connect_timeout=Config.redis.REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=Config.redis.REDIS_SOCKET_TIMEOUT,
            socket_keepalive=True,
            health_check_interval=Config.redis.REDIS_HEALTH_CHECK_INTERVAL,
        )
        _redis_client = redis.Redis(connection_pool=pool)
        logger.info(f"[Redis] 成功初始化连接池: {mask_url(Config.redis.REDIS_URL)}")
    return _redis_client


def init_redis() -> None:
    get_redis()


_T = TypeVar("_T")


async def _await_if_needed(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[_T], value)
    return value


RedisEvalArg = bytes | bytearray | str | int | float | memoryview


async def redis_set_nx_ex(key: str, value: str, ttl_seconds: int) -> bool:
    r = get_redis()
    raw = await r.set(key, value, nx=True, ex=int(ttl_seconds))
    return bool(raw)


async def redis_eval_int(script: str, numkeys: int, *args: RedisEvalArg) -> int:
    r = get_redis()
    raw = await _await_if_needed(r.eval(script, int(numkeys), *args))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def redis_eval_str(script: str, numkeys: int, *args: RedisEvalArg) -> str | None:
    r = get_redis()
    raw = await _await_if_needed(r.eval(script, int(numkeys), *args))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        with contextlib.suppress(Exception):
            return raw.decode("utf-8")
    return str(raw)


async def redis_get_str(key: str) -> str | None:
    r = get_redis()
    raw = await r.get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        with contextlib.suppress(Exception):
            return raw.decode("utf-8")
    if isinstance(raw, str):
        return raw
    return str(raw)


async def redis_setex_str(key: str, ttl_seconds: int, value: str) -> None:
    r = get_redis()
    await r.setex(key, int(ttl_seconds), value)


async def redis_setex_bytes(key: str, ttl_seconds: int, value: bytes) -> None:
    r = get_redis()
    await r.setex(key, int(ttl_seconds), value)


async def redis_delete(key: str) -> int:
    r = get_redis()
    raw = await r.delete(key)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def redis_publish(channel: str, message: str) -> int:
    r = get_redis()
    raw = await r.publish(channel, message)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def redis_incr(key: str) -> int:
    r = get_redis()
    raw = await r.incr(key)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def redis_expire(key: str, ttl_seconds: int) -> bool:
    r = get_redis()
    raw = await r.expire(key, int(ttl_seconds))
    return bool(raw)


async def redis_incr_with_expire(key: str, ttl_seconds: int) -> int:
    val = await redis_incr(key)
    await redis_expire(key, ttl_seconds)
    return val


async def redis_get_json_dict(key: str) -> dict[str, Any] | None:
    raw = await redis_get_str(key)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


async def redis_setex_json(key: str, ttl_seconds: int, payload: dict[str, Any]) -> None:
    await redis_setex_str(key, ttl_seconds, json.dumps(payload))


class RedisPubSub:
    def __init__(self, inner: PubSub) -> None:
        self._inner = inner

    async def subscribe(self, channel: str) -> None:
        await self._inner.subscribe(channel)

    async def unsubscribe(self, channel: str) -> None:
        await self._inner.unsubscribe(channel)

    async def get_message(
        self, *, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> dict[str, Any] | None:
        raw = await self._inner.get_message(
            ignore_subscribe_messages=ignore_subscribe_messages,
            timeout=timeout,
        )
        return raw if isinstance(raw, dict) else None

    async def close(self) -> None:
        await self._inner.close()


def redis_pubsub() -> RedisPubSub:
    r = get_redis()
    return RedisPubSub(r.pubsub())


async def redis_xlen(stream: str) -> int:
    r = get_redis()
    raw = await r.xlen(stream)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def redis_xpending_pending(stream: str, group: str) -> int:
    r = get_redis()
    raw = await r.xpending(stream, group)
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


async def redis_xinfo_group_lag(stream: str, group: str) -> int:
    r = get_redis()
    raw = await r.xinfo_groups(stream)
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


async def dispose_redis() -> None:
    """关闭 Redis 连接池（应用关闭时调用）"""
    global _redis_client
    if _redis_client:
        with contextlib.suppress(Exception):
            await _redis_client.aclose()
        _redis_client = None
    logger.info("[Redis] 当前连接池已关闭")
