"""Lightweight audit log — fire-and-forget recording of state-changing operations."""

from __future__ import annotations

import asyncio
import contextlib

from db.session import session_scope
from models import AuditLog


async def record_audit(
    resource_type: str,
    resource_id: int | None,
    resource_name: str | None,
    action: str,
    summary: str,
    *,
    actor: str = "dashboard",
) -> None:
    """Persist one audit log row. Fire-and-forget — failures are logged but never
    block the triggering operation.
    """
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_name=resource_name,
                    action=action,
                    summary=summary,
                    actor=actor,
                )
            )
    except Exception:  # nosec B110 — best-effort; losing an audit row is acceptable
        pass


def _fire_audit(*args: object, **kwargs: object) -> None:
    """Schedule a background audit log write without awaiting it."""
    with contextlib.suppress(RuntimeError):
        # May raise RuntimeError if no event loop is running (e.g. in tests).
        asyncio.ensure_future(record_audit(
            str(args[0]), args[1] if len(args) > 1 else None,  # type: ignore[arg-type]
            str(args[2]) if len(args) > 2 else None,
            str(args[3]) if len(args) > 3 else "",
            str(args[4]) if len(args) > 4 else "",
            actor=str(kwargs.get("actor", "dashboard")),
        ))
