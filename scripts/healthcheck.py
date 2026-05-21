import asyncio
import os
import subprocess
import urllib.request


async def _check_background_process() -> None:
    from core.redis_client import dispose_redis, get_redis
    from db.session import dispose_engine, init_engine, test_db_connection

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


def _check_supervisor() -> None:
    env = dict(os.environ)
    env.setdefault("PYTHONWARNINGS", "ignore:pkg_resources.*:UserWarning")
    output = subprocess.check_output(
        ["supervisorctl", "-c", "/app/supervisord.conf", "status"],
        text=True,
        env=env,
        timeout=5,
    )
    expected = {"api", "worker", "scheduler"}
    running = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "RUNNING":
            running.add(parts[0])
    missing = expected - running
    if missing:
        raise SystemExit(f"supervisor programs not running: {', '.join(sorted(missing))}")


def main() -> int:
    run_mode = (os.getenv("RUN_MODE") or "").strip().lower()
    if run_mode in {"worker", "scheduler"}:
        asyncio.run(_check_background_process())
    elif run_mode == "all":
        _check_supervisor()
        _check_api()
    elif run_mode == "migrate":
        return 0
    else:
        _check_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
