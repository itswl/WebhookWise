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

import psycopg2

from db.session import dispose_engine, init_engine, test_db_connection

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


def _advance_legacy_logic_sinking_revision() -> None:
    """Patch a historical partially-applied migration marker.

    Older deployments could have the logic-sinking objects already present while
    alembic_version stayed at the previous revision. This keeps that one-off
    compatibility fix out of the long-running API/worker entrypoint.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return

    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.alembic_version')")
        if not cur.fetchone()[0]:
            return

        cur.execute("select version_num from public.alembic_version")
        current = cur.fetchone()[0]
        if current != "9c0b7c3e2a11":
            return

        cur.execute("select to_regclass('public.processing_locks')")
        has_processing_locks = bool(cur.fetchone()[0])
        cur.execute(
            "select 1 from pg_indexes "
            "where schemaname='public' and indexname='idx_unique_alert_hash_original' limit 1"
        )
        has_unique_idx = cur.fetchone() is not None

        if has_processing_locks and has_unique_idx:
            cur.execute("update public.alembic_version set version_num=%s", ("6a7b8c9d0e1f",))
            print("已修正历史 alembic_version 标记: 9c0b7c3e2a11 -> 6a7b8c9d0e1f")
    finally:
        conn.close()


def main() -> int:
    max_retries = int(os.getenv("MIGRATION_DB_MAX_RETRIES", "30"))
    interval_seconds = float(os.getenv("MIGRATION_DB_RETRY_INTERVAL_SECONDS", "2"))

    started = time.time()
    asyncio.run(_wait_for_database(max_retries=max_retries, interval_seconds=interval_seconds))
    _run_alembic_upgrade()
    _advance_legacy_logic_sinking_revision()
    print(f"数据库迁移完成，用时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
