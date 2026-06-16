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

from sqlalchemy import text

from core.config import get_settings
from db.engine import dispose_engine, get_engine, init_engine, test_db_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Revisions from the pre-squash incremental chain. A database stamped at any of
# these predates the consolidated 0001_baseline and already has the full schema,
# so it must be re-stamped (not re-run) onto the new baseline before upgrading.
_SQUASHED_LEGACY_REVISIONS = frozenset(
    {
        "0001_current_schema",
        "0002_forward_rule_identity_match",
        "0003_server_defaults",
        "0004_sync_archived_table",
        "0005_dead_letter_index",
        "0006_drop_duplicate_outbox_index",
    }
)
_BASELINE_REVISION = "0001_baseline"


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


async def _current_alembic_revision() -> str | None:
    """Read the version stored in alembic_version, or None if not yet stamped."""
    await init_engine(get_settings())
    engine = get_engine()
    if engine is None:
        return None
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT to_regclass('alembic_version')"))
            if not exists:
                return None
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
            return str(version) if version is not None else None
    finally:
        await dispose_engine()


def _alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=PROJECT_ROOT, check=True)  # nosec B603


def _reconcile_squashed_history() -> None:
    """Bridge databases stamped on the pre-squash chain onto the new baseline.

    The incremental migrations 0001_current_schema..0006 were squashed into a
    single 0001_baseline. A database already stamped at one of those revisions
    has the full schema but points at a revision Alembic can no longer find, so
    `upgrade head` would fail. Re-stamp it onto the baseline (metadata only — no
    schema change); empty/fresh databases are left untouched and upgrade normally.
    """
    current = asyncio.run(_current_alembic_revision())
    if current in _SQUASHED_LEGACY_REVISIONS:
        print(f"Detected pre-squash revision '{current}'; re-stamping to '{_BASELINE_REVISION}' (no schema change)")
        # --purge: the old revision no longer exists in the script history, so a
        # plain `stamp` would fail trying to locate it. --purge clears the
        # version table and writes the baseline directly.
        _alembic("stamp", _BASELINE_REVISION, "--purge")


def _run_alembic_upgrade() -> None:
    print("Running Alembic migrations...")
    _alembic("upgrade", "head")


def main() -> int:
    max_retries = int(os.getenv("MIGRATION_DB_MAX_RETRIES", "30"))
    interval_seconds = float(os.getenv("MIGRATION_DB_RETRY_INTERVAL_SECONDS", "2"))

    started = time.time()
    asyncio.run(_wait_for_database(max_retries=max_retries, interval_seconds=interval_seconds))
    _reconcile_squashed_history()
    _run_alembic_upgrade()
    print(f"Database migration complete, took {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
