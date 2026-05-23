"""channel outbox refactor

Revision ID: 0006_channel_outbox
Revises: 0005_create_archived_events
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_channel_outbox"
down_revision: str | Sequence[str] | None = "0005_create_archived_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(col.get("name") == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_table("forward_outboxes"):
        return

    with op.batch_alter_table("forward_outboxes") as batch:
        if not _has_column("forward_outboxes", "channel_name"):
            batch.add_column(sa.Column("channel_name", sa.String(length=32), server_default="", nullable=False))
        if not _has_column("forward_outboxes", "event_type"):
            batch.add_column(sa.Column("event_type", sa.String(length=32), server_default="", nullable=False))
        if not _has_column("forward_outboxes", "formatted_payload"):
            batch.add_column(sa.Column("formatted_payload", _jsonb(), nullable=True))
        batch.alter_column("webhook_event_id", existing_type=sa.Integer(), nullable=True)

    if _has_column("forward_outboxes", "channel_name"):
        op.execute(
            "UPDATE forward_outboxes SET channel_name = target_type "
            "WHERE (channel_name IS NULL OR channel_name = '') AND target_type IS NOT NULL"
        )
    if _has_column("forward_outboxes", "event_type"):
        op.execute(
            "UPDATE forward_outboxes SET event_type = 'webhook_forward' "
            "WHERE (event_type IS NULL OR event_type = '')"
        )


def downgrade() -> None:
    if not _has_table("forward_outboxes"):
        return

    with op.batch_alter_table("forward_outboxes") as batch:
        if _has_column("forward_outboxes", "formatted_payload"):
            batch.drop_column("formatted_payload")
        if _has_column("forward_outboxes", "event_type"):
            batch.drop_column("event_type")
        if _has_column("forward_outboxes", "channel_name"):
            batch.drop_column("channel_name")

