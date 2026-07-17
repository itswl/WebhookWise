import contextlib
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Request
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from core.logger import get_logger

_logger = get_logger("db.session")

# Namespace constant for transaction-scoped advisory locks keyed on a textual
# identity (e.g. an alert hash). Keeping it stable avoids collisions with any
# other advisory-lock use that picks a different namespace.
_ADVISORY_LOCK_NAMESPACE = 0x57484B57  # "WHKW"
_AFTER_COMMIT_ACTIONS_KEY = "webhookwise.after_commit_actions"

AfterCommitAction = Callable[[], Awaitable[None]]


def register_after_commit_action(session: AsyncSession, key: str, action: AfterCommitAction) -> None:
    """Queue one idempotent async side effect for the successful transaction owner.

    Service methods often flush before their API or worker transaction commits. Side
    effects such as cross-process cache invalidation must therefore be delayed until
    the owning dependency/context manager has observed a successful commit. Reusing
    ``key`` coalesces repeated mutations in the same transaction.
    """
    actions = cast(dict[str, AfterCommitAction], session.info.setdefault(_AFTER_COMMIT_ACTIONS_KEY, {}))
    actions[key] = action


async def _run_after_commit_actions(session: AsyncSession) -> None:
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    actions = cast(dict[str, AfterCommitAction], info.pop(_AFTER_COMMIT_ACTIONS_KEY, {}))
    for action in actions.values():
        await action()


def _discard_after_commit_actions(session: AsyncSession) -> None:
    info = getattr(session, "info", None)
    if isinstance(info, dict):
        info.pop(_AFTER_COMMIT_ACTIONS_KEY, None)


def _advisory_lock_classid(key: str) -> int:
    """Map an arbitrary string to a signed 32-bit int for pg_advisory_xact_lock.

    PostgreSQL's two-int advisory lock form takes two int4 values. We pin the
    first to a fixed namespace and derive the second deterministically from the
    key so the same key always maps to the same lock.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=4).digest()
    unsigned = int.from_bytes(digest, "big")
    # Convert to signed 32-bit range expected by int4.
    return unsigned - 0x1_0000_0000 if unsigned >= 0x8000_0000 else unsigned


async def acquire_advisory_xact_lock(session: AsyncSession, key: str) -> None:
    """Take a transaction-scoped Postgres advisory lock for ``key``.

    The lock is held until the surrounding transaction commits or rolls back,
    serialising concurrent workers that operate on the same logical key (e.g.
    the same alert hash) so a read-then-insert sequence stays atomic. On
    non-PostgreSQL backends (or if advisory locks are unavailable) this is a
    best-effort no-op so unit tests on SQLite keep working.
    """
    bind = session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
        return
    classid = _advisory_lock_classid(key)
    stmt = text("SELECT pg_advisory_xact_lock(:ns, :objid)").bindparams(
        bindparam("ns", _ADVISORY_LOCK_NAMESPACE),
        bindparam("objid", classid),
    )
    await session.execute(stmt)


class Base(DeclarativeBase):
    pass


def _app_context_from_request(request: "Request | None") -> object | None:
    from core.app_context import AppContext, get_default_app_context

    default_context = get_default_app_context()
    if request is not None:
        context = getattr(getattr(request.app, "state", None), "app_context", None)
        if isinstance(context, AppContext):
            if context.session_factory is None and default_context is not None:
                return default_context
            return context
    return default_context


async def _ensure_session_factory(request: "Request | None" = None) -> async_sessionmaker[AsyncSession]:
    context = _app_context_from_request(request)
    if context is None:
        raise RuntimeError("default AppContext is not initialized")
    ensure_db = getattr(context, "ensure_db", None)
    if not callable(ensure_db):
        raise RuntimeError("AppContext is missing ensure_db()")
    return cast(async_sessionmaker[AsyncSession], await ensure_db())


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI Depends async generator: only manages the session lifecycle.

    HTTP write endpoints must commit explicitly. This way a route can trigger
    TaskIQ or external notifications only after a successful commit, avoiding the
    unclear transaction/side-effect ordering caused by committing only on
    dependency teardown.
    """
    session_factory = await _ensure_session_factory(request)
    start = time.perf_counter()
    status = "success"
    try:
        from core.observability.tracing import otel_span

        with otel_span("db.session", {"db.operation": "request_session"}):
            async with session_factory() as session:
                try:
                    yield session
                except Exception:
                    _discard_after_commit_actions(session)
                    raise
                else:
                    # A pending transaction means a write endpoint returned
                    # without the explicit commit required by this dependency.
                    # Never publish side effects for data that will roll back on
                    # session close.
                    in_transaction = getattr(session, "in_transaction", None)
                    if callable(in_transaction) and in_transaction():
                        _discard_after_commit_actions(session)
                    else:
                        await _run_after_commit_actions(session)
    except Exception:
        status = "error"
        raise
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels("request_session", status).inc()
        DB_SESSION_DURATION_SECONDS.labels("request_session", status).observe(time.perf_counter() - start)


@asynccontextmanager
async def session_scope(existing_session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """Async database transaction context manager.

    When creating a new session, uses SQLAlchemy 2.0's
    ``async_sessionmaker.begin()``, letting the framework handle commit, rollback
    and close. When an existing_session is passed in, the transaction boundary is
    not taken over; the outer caller is responsible for committing or rolling back.
    """
    start = time.perf_counter()
    operation = "existing_session" if existing_session else "transaction"
    status = "success"
    try:
        from core.observability.tracing import otel_span

        with otel_span("db.session", {"db.operation": operation}):
            if existing_session:
                yield existing_session
            else:
                session_factory = await _ensure_session_factory()
                async with session_factory.begin() as session:
                    try:
                        yield session
                    except Exception:
                        _discard_after_commit_actions(session)
                        raise
                await _run_after_commit_actions(session)
    except Exception:
        status = "error"
        raise
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels(operation, status).inc()
        DB_SESSION_DURATION_SECONDS.labels(operation, status).observe(time.perf_counter() - start)


def dml_rowcount(result: Any) -> int:
    """Rows affected by an executed DML statement.

    session.execute() is typed as returning Result, whose stub no longer
    exposes rowcount (it lives on CursorResult, which DML actually returns at
    runtime); this narrows in one place instead of casting at every call site.
    """
    return int(getattr(result, "rowcount", 0) or 0)


def _is_query_timeout(exc: BaseException) -> bool:
    """True only for a statement_timeout / query cancellation, not arbitrary errors."""
    import asyncio

    from asyncpg.exceptions import QueryCanceledError
    from sqlalchemy.exc import DBAPIError

    if isinstance(exc, asyncio.TimeoutError | QueryCanceledError):
        return True
    return isinstance(exc, DBAPIError) and isinstance(getattr(exc, "orig", None), QueryCanceledError)


async def count_with_timeout(
    session: AsyncSession,
    stmt: Any,
    timeout_ms: int = 2000,
) -> int | None:
    """COUNT query with a statement_timeout safeguard (PostgreSQL-only).

    Returns None **only** when the query is cancelled by the timeout; callers
    should treat None as "count unknown". Any other DB error (connection loss,
    SQL error) propagates rather than being coerced to None — otherwise a real
    failure surfaces on the dashboard as a misleading "0" instead of an error.
    Uses a SAVEPOINT to isolate the timed-out query, preventing a rollback from
    destroying the caller's transaction.
    """
    start = time.perf_counter()
    status = "success"
    try:
        async with session.begin_nested() as nested:
            with contextlib.suppress(Exception):
                await session.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}'"))
            result = await session.execute(stmt)
            value = int(result.scalar() or 0)
            # Roll the SAVEPOINT back instead of releasing it: a COUNT has no
            # data effects to keep, and under PostgreSQL GUC semantics only
            # ROLLBACK TO SAVEPOINT reverts SET LOCAL — releasing would leak
            # the shortened statement_timeout onto every later query in the
            # caller's request-scoped transaction.
            await nested.rollback()
            return value
    except Exception as e:
        if _is_query_timeout(e):
            status = "timeout"
            _logger.warning("COUNT query timed out (%dms): %s", timeout_ms, e)
            return None
        status = "error"
        _logger.error("COUNT query failed (not a timeout): %s", e, exc_info=True)
        raise
    finally:
        from core.observability.metrics import DB_SESSION_DURATION_SECONDS, DB_SESSION_TOTAL

        DB_SESSION_TOTAL.labels("count_query", status).inc()
        DB_SESSION_DURATION_SECONDS.labels("count_query", status).observe(time.perf_counter() - start)


__all__ = [
    "Base",
    "acquire_advisory_xact_lock",
    "count_with_timeout",
    "get_db_session",
    "register_after_commit_action",
    "session_scope",
]
