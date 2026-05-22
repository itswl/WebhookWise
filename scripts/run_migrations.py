"""Run deployment-time database migrations.

This module is intended to run as a one-shot container/job before application
processes start. Application entrypoints should not own schema migration.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

from db.engine import dispose_engine, init_engine, test_db_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def _wait_for_database(max_retries: int, interval_seconds: float) -> None:
    for attempt in range(1, max_retries + 1):
        await init_engine()
        if await test_db_connection():
            print("数据库连接成功")
            await dispose_engine()
            return
        print(f"等待数据库... ({attempt}/{max_retries})")
        await dispose_engine()
        await asyncio.sleep(interval_seconds)
    raise SystemExit("数据库连接超时，迁移任务失败")


def _run_alembic_upgrade() -> None:
    print("运行 Alembic 迁移...")
    subprocess.run(["alembic", "upgrade", "head"], cwd=PROJECT_ROOT, check=True)


def main() -> int:
    max_retries = int(os.getenv("MIGRATION_DB_MAX_RETRIES", "30"))
    interval_seconds = float(os.getenv("MIGRATION_DB_RETRY_INTERVAL_SECONDS", "2"))

    started = time.time()
    asyncio.run(_wait_for_database(max_retries=max_retries, interval_seconds=interval_seconds))
    _run_alembic_upgrade()
    print(f"数据库迁移完成，用时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
