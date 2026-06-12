import asyncio
import http.client
import os


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


async def _check_migration_completed() -> None:
    from sqlalchemy import text

    from db.engine import dispose_engine, get_engine, init_engine

    try:
        await init_engine()
        engine = get_engine()
        if engine is None:
            raise SystemExit(1)
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            if result.scalar_one_or_none() is None:
                raise SystemExit(1)
    finally:
        await dispose_engine()


def _check_api() -> None:
    port = int(os.getenv("PORT") or "8000")
    conn = http.client.HTTPConnection("localhost", port, timeout=5)
    try:
        conn.request("GET", "/ready")
        response = conn.getresponse()
        response.read()
        if response.status >= 400:
            raise SystemExit(1)
    finally:
        conn.close()


def main() -> int:
    run_mode = (os.getenv("RUN_MODE") or "").strip().lower()
    if run_mode in {"worker", "scheduler"}:
        asyncio.run(_check_background_process())
    elif run_mode == "migrate":
        asyncio.run(_check_migration_completed())
    else:
        _check_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
