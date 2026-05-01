import asyncio
import os
import urllib.request


async def _check_worker() -> None:
    from core.redis_client import get_redis
    from db.session import init_engine, test_db_connection

    await init_engine()
    ok = await test_db_connection()
    if not ok:
        raise SystemExit(1)
    r = get_redis()
    await r.ping()


def _check_api() -> None:
    urllib.request.urlopen("http://localhost:8000/health", timeout=5).read()


def main() -> int:
    run_mode = (os.getenv("RUN_MODE") or "").strip().lower()
    if run_mode == "worker":
        asyncio.run(_check_worker())
    else:
        _check_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
