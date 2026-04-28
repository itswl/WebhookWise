import asyncio
import os
import threading

from core.config import Config
from core.logger import logger
from core.redis_client import get_redis

_stop_event = threading.Event()

_LEADER_KEY = "pollers:leader"
_LEADER_TTL_SECONDS = 90
_RENEW_INTERVAL_SECONDS = 30


async def _renew_leader(token: str) -> None:
    while not _stop_event.is_set():
        try:
            redis = get_redis()
            current = await redis.get(_LEADER_KEY)
            if (
                current is None
                or (isinstance(current, bytes) and current.decode("utf-8") != token)
                or (isinstance(current, str) and current != token)
            ):
                return
            await redis.expire(_LEADER_KEY, _LEADER_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[Pollers] leader renew failed: {e}")
            return
        await asyncio.sleep(_RENEW_INTERVAL_SECONDS)


def _run_renew(token):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_renew_leader(token))
    except Exception as e:
        logger.error(f"[Pollers] _run_renew error: {e}")
    finally:
        loop.close()


def stop_background_pollers():
    _stop_event.set()


async def start_background_pollers(worker_id: str | None = None) -> bool:
    if not getattr(Config, "ENABLE_POLLERS", True):
        logger.info("[Pollers] disabled by config")
        return False

    worker_id = worker_id or f"{os.getpid()}"

    try:
        redis = get_redis()
    except Exception as e:
        logger.warning(f"[Pollers] redis unavailable, skip starting pollers: {e}")
        return False

    try:
        acquired = await redis.set(_LEADER_KEY, worker_id, nx=True, ex=_LEADER_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"[Pollers] failed to acquire leader lock: {e}")
        return False

    if not acquired:
        logger.info("[Pollers] leader exists, skip starting pollers")
        return False

    threading.Thread(target=_run_renew, args=(worker_id,), daemon=True, name="pollers-leader-renew").start()

    try:
        from services.maintenance_poller import start_maintenance_poller

        start_maintenance_poller()
    except Exception as e:
        logger.warning(f"[Pollers] maintenance poller start failed: {e}")

    try:
        from services.openclaw_poller import start_poller

        start_poller(interval=30)
    except Exception as e:
        logger.warning(f"[Pollers] openclaw poller start failed: {e}")

    # 转发重试 Poller
    if Config.ENABLE_FORWARD_RETRY:
        try:
            from services.forward_retry_poller import start_forward_retry_poller

            start_forward_retry_poller()
        except Exception as e:
            logger.warning(f"[Pollers] forward retry poller start failed: {e}")

    logger.info("[Pollers] started")
    return True
