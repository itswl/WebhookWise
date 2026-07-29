"""Real-backend integration tests: actual PostgreSQL + Redis.

Everything else in the suite runs on in-memory SQLite and a mocked Redis;
four escaped bug classes proved those doubles diverge from production in
known ways (VARCHAR length enforcement, FOR UPDATE being a silent no-op on
SQLite, Lua scripts never executing, mock bypass by import style). This
directory runs a SMALL set of semantics tests against the real engines.

Collection is env-gated (nothing is collected without REAL_INFRA_TESTS=1),
so the default suite is unaffected. The CI `integration` job provides
service containers; locally / on the ops host:

    REAL_INFRA_TESTS=1 \
    DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/db \
    REDIS_URL=redis://localhost:6379/15 \
    pytest -q tests/real_infra/
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

collect_ignore_glob = ["*"] if not os.getenv("REAL_INFRA_TESTS") else []

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def _migrated_database_url() -> str:
    """Reset the target database schema and apply the FULL migration chain.

    Running every migration against real Postgres is itself the first test:
    it is exactly what SQLite-backed tests cannot verify (types, index DDL,
    the alembic_version VARCHAR(32) ceiling).
    """
    url = os.environ["DATABASE_URL"]
    import sqlalchemy

    sync_url = url.replace("+asyncpg", "+psycopg2")
    engine = sqlalchemy.create_engine(sync_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("DROP SCHEMA public CASCADE"))
        conn.execute(sqlalchemy.text("CREATE SCHEMA public"))
    engine.dispose()

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, f"alembic upgrade head failed:\n{completed.stdout}\n{completed.stderr}"
    return url


@pytest.fixture
async def pg_engine(_migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_migrated_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=pg_engine, class_=AsyncSession, expire_on_commit=False)
