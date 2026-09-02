"""Shared engine helpers for DB-backed tests.

Centralizes the engine construction and schema creation that used to be copied
into each DB-backed test module. The pytest entry points are the `db_engine`,
`db_session_factory`, `db_session`, and `db_app_context_session_factory`
fixtures in tests/conftest.py, which build on these helpers.

Two backends, selected by one environment variable:

* unset (the default, and what the gate and every laptop run) — in-memory
  SQLite, one fresh database per test.
* ``WW_TEST_DATABASE_URL`` set to an asyncpg URL — the real PostgreSQL the CI
  `integration` job stands up. The advisory lock in db/session.py, the
  ``ON CONFLICT DO UPDATE`` upsert in services/kb/store.py and every
  ``FOR UPDATE SKIP LOCKED`` claim are no-ops or silent lies on SQLite; on this
  path they actually execute.

The PostgreSQL path builds the schema ONCE per session into a schema of its
own, named after the pytest-xdist worker. That keeps `public` free for
``alembic upgrade head`` in the same job and the same database, and keeps
parallel workers from truncating each other's rows.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool


def test_database_url() -> str | None:
    """The PostgreSQL URL this run was pointed at, or None for the SQLite default."""
    return (os.environ.get("WW_TEST_DATABASE_URL") or "").strip() or None


def postgres_test_schema() -> str:
    """The PostgreSQL schema this process owns.

    One schema per pytest-xdist worker (`gw0`, `gw1`, …; `main` when the suite
    runs single-process). Workers share one database, so a shared schema would
    make the between-tests TRUNCATE delete another worker's fixtures mid-test.
    """
    worker = (os.environ.get("PYTEST_XDIST_WORKER") or "main").strip() or "main"
    return f"ww_test_{worker}"


def _sync_url(url: str) -> str:
    """The synchronous driver URL for DDL run outside the event loop."""
    return url.replace("+asyncpg", "+psycopg2")


def make_memory_engine() -> AsyncEngine:
    """Return an in-memory SQLite async engine that shares one connection.

    StaticPool + check_same_thread=False keeps every session in a test bound to
    the same in-memory database; a fresh connection would otherwise see an empty
    schema.
    """
    return create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def make_postgres_engine(url: str, schema: str) -> AsyncEngine:
    """Return an async engine pinned to this worker's PostgreSQL schema.

    NullPool because the engine is function-scoped and asyncpg connections
    belong to the event loop that opened them: a pooled connection handed to
    the next test's loop raises instead of running the test.

    The search path is this schema and nothing else — deliberately not
    ``schema, public``. A table missing here would otherwise resolve to
    `public`, and the tests would pass against the migrated schema they are not
    supposed to touch.
    """
    return create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )


async def create_all(engine: AsyncEngine) -> None:
    """Create every ORM table registered on Base.metadata on the given engine."""
    import models  # noqa: F401  (register all models on Base.metadata)
    from db.session import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def create_postgres_schema(url: str) -> str:
    """Drop and rebuild this worker's schema, then create every ORM table in it.

    Synchronous on purpose: it runs once per session, and a session-scoped async
    fixture would need its own event loop while the suite runs function-scoped
    ones. Returns the schema name.
    """
    import models  # noqa: F401  (register all models on Base.metadata)
    from db.session import Base

    schema = postgres_test_schema()
    engine = sqlalchemy.create_engine(_sync_url(url), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            # webhook_events carries six pg_trgm search indexes (migration 0011).
            # The extension is database-wide; it lands in public, which is why
            # public has to be on the DDL search path below.
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET search_path TO "{schema}", public'))
            # checkfirst=False, and only here: the same database's `public` may
            # already hold a full migrated schema (the CI job runs `alembic
            # upgrade head` against it), and a has_table() probe would resolve
            # through the search path, find every table, and skip creating any
            # of them — leaving the whole suite silently pointed at `public`.
            # The schema was just dropped, so there is nothing to check for.
            Base.metadata.create_all(conn, checkfirst=False)
    finally:
        engine.dispose()
    return schema


async def ensure_webhook_events(session: AsyncSession, *event_ids: int | None) -> None:
    """Materialize the ``webhook_events`` rows the caller is about to point at.

    SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys=ON`` is
    issued per connection, so a test could file an outbox record or a decision
    trace against an event id that never existed and still pass. PostgreSQL
    rejects it, and is right to: those columns carry real FKs in production.
    Call this before seeding rows that reference an event id by hand.
    """
    from models import WebhookEvent

    await _ensure_parents(session, WebhookEvent, "webhook_events", event_ids, source="test")


async def ensure_forward_rules(session: AsyncSession, *rule_ids: int | None) -> None:
    """Materialize the ``forward_rules`` rows the caller is about to point at.

    Same reason as `ensure_webhook_events`: forward_outboxes.forward_rule_id is
    a real foreign key, invisible on SQLite.
    """
    from models import ForwardRule

    for rule_id in {int(v) for v in rule_ids if v is not None}:
        await _ensure_parents(
            session,
            ForwardRule,
            "forward_rules",
            (rule_id,),
            name=f"test-rule-{rule_id}",
            target_type="webhook",
        )


async def _ensure_parents(
    session: AsyncSession,
    model: type[Any],
    table: str,
    ids: Sequence[int | None],
    **defaults: Any,
) -> None:
    wanted = sorted({int(value) for value in ids if value is not None})
    created = False
    for row_id in wanted:
        if await session.get(model, row_id) is not None:
            continue
        session.add(model(id=row_id, **defaults))
        created = True
    if not created:
        return
    await session.flush()
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # An explicit id leaves the identity sequence behind it, so the next
        # insert without one would collide. Nothing here reads the sequence,
        # so advance it past whatever was just planted.
        await session.execute(
            text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT COALESCE(MAX(id), 1) FROM {table}))")
        )


async def truncate_all(engine: AsyncEngine) -> None:
    """Empty every ORM table, restoring the fresh-database-per-test isolation.

    The SQLite path gets a brand-new in-memory database per test; recreating a
    real schema that often costs seconds per test, so PostgreSQL gets one
    TRUNCATE of every table instead — one statement, one round trip, and
    RESTART IDENTITY so the id sequences a test asserts on start from 1 again.
    """
    import models  # noqa: F401  (register all models on Base.metadata)
    from db.session import Base

    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    if not tables:
        return
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
