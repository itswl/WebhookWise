import asyncio
import os
import urllib.request


async def _check_background_process() -> None:
    from core.redis_client import dispose_redis, get_redis
    from db.engine import dispose_engine, init_engine, test_db_connection

    try:
        await init_engine()
        ok = await test_db_connection()
        if not ok:
            raise SystemExit(1)
        r = get_redis()
        await r.ping()
    finally:
        await dispose_redis()
        await dispose_engine()


def _check_api() -> None:
    port = int(os.getenv("PORT") or "8000")
    urllib.request.urlopen(f"http://localhost:{port}/ready", timeout=5).read()


def main() -> int:
    run_mode = (os.getenv("RUN_MODE") or "").strip().lower()
    if run_mode in {"worker", "scheduler"}:
        asyncio.run(_check_background_process())
    elif run_mode == "migrate":
        return 0
    else:
        _check_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
