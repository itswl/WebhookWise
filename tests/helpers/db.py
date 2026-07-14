"""Shared in-memory SQLite helpers for DB-backed tests.

Centralizes the engine construction and schema creation that used to be copied
into each DB-backed test module. The pytest entry points are the `db_engine`,
`db_session_factory`, `db_session`, and `db_app_context_session_factory`
fixtures in tests/conftest.py, which build on these helpers.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool


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


async def create_all(engine: AsyncEngine) -> None:
    """Create every ORM table registered on Base.metadata on the given engine."""
    import models  # noqa: F401  (register all models on Base.metadata)
    from db.session import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
