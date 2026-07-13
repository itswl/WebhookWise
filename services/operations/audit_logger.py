"""Transactional activity-log helpers for state-changing operations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from models import AuditLog


def add_audit(
    session: AsyncSession,
    resource_type: str,
    resource_id: int | None,
    resource_name: str | None,
    action: str,
    summary: str,
    *,
    actor: str = "dashboard",
) -> AuditLog:
    """Add an activity row to the caller's business transaction."""
    record = AuditLog(
        resource_type=resource_type[:20],
        resource_id=resource_id,
        resource_name=resource_name[:200] if resource_name else None,
        action=action[:20],
        summary=summary[:500],
        actor=actor[:100],
    )
    session.add(record)
    return record
