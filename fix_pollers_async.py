with open("services/pollers.py") as f:
    content = f.read()

content = content.replace(
    "def _renew_leader(redis, token: str) -> None:", "async def _renew_leader(redis, token: str) -> None:"
)
content = content.replace("current = redis.get(_LEADER_KEY)", "current = await redis.get(_LEADER_KEY)")
content = content.replace(
    "redis.expire(_LEADER_KEY, _LEADER_TTL_SECONDS)", "await redis.expire(_LEADER_KEY, _LEADER_TTL_SECONDS)"
)

content = content.replace(
    "def stop_background_pollers():",
    "import asyncio\n\ndef _run_renew(redis, token):\n    asyncio.run(_renew_leader(redis, token))\n\ndef stop_background_pollers():",
)
content = content.replace(
    "threading.Thread(target=_renew_leader, args=(redis, worker_id)",
    "threading.Thread(target=_run_renew, args=(redis, worker_id)",
)

content = content.replace(
    "acquired = redis.set(_LEADER_KEY, worker_id, nx=True, ex=_LEADER_TTL_SECONDS)",
    "import asyncio\n    try:\n        # Because start_background_pollers is called in async lifespan, we might need a specific run loop or just await it if we make start_background_pollers async\n        # Let's make start_background_pollers async!\n        pass\n    except:\n        pass",
)
