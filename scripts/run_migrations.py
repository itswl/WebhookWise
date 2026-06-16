"""Run deployment-time database migrations.

This module is intended to run as a one-shot container/job before application
processes start. Application entrypoints should not own schema migration.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

from core.config import get_settings
from db.engine import dispose_engine, init_engine, test_db_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _wait_for_database(max_retries: int, interval_seconds: float) -> None:
    config = get_settings()
    for attempt in range(1, max_retries + 1):
        await init_engine(config)
        if await test_db_connection():
            print("Database connection successful")
            await dispose_engine()
            return
        print(f"Waiting for database... ({attempt}/{max_retries})")
        await dispose_engine()
        await asyncio.sleep(interval_seconds)
    raise SystemExit("Database connection timed out, migration task failed")


def _run_alembic_upgrade() -> None:
    print("Running Alembic migrations...")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, check=True)  # nosec B603


def main() -> int:
    max_retries = int(os.getenv("MIGRATION_DB_MAX_RETRIES", "30"))
    interval_seconds = float(os.getenv("MIGRATION_DB_RETRY_INTERVAL_SECONDS", "2"))

    started = time.time()
    asyncio.run(_wait_for_database(max_retries=max_retries, interval_seconds=interval_seconds))
    _run_alembic_upgrade()
    print(f"Database migration complete, took {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
