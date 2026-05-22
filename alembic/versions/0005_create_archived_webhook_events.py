"""create archived webhook events table

Revision ID: 0005_create_archived_events
Revises: 0004_drop_archived_events
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_create_archived_events"
down_revision: str | Sequence[str] | None = "0004_drop_archived_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _jsonb() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    if _has_table("archived_webhook_events"):
        return

    op.create_table(
        "archived_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("client_ip", sa.String(length=50), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_payload", sa.LargeBinary(), nullable=True),
        sa.Column("headers", _jsonb(), nullable=True),
        sa.Column("parsed_data", _jsonb(), nullable=True),
        sa.Column("alert_hash", sa.String(length=64), nullable=True),
        sa.Column("ai_analysis", _jsonb(), nullable=True),
        sa.Column("importance", sa.String(length=20), nullable=True),
        sa.Column("processing_status", sa.String(length=20), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("forward_status", sa.String(length=20), nullable=True),
        sa.Column("prev_alert_id", sa.BigInteger(), nullable=True),
        sa.Column("is_duplicate", sa.Boolean(), nullable=True),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=True),
        sa.Column("beyond_window", sa.Boolean(), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_archived_webhook_events_request_id", "archived_webhook_events", ["request_id"])
    op.create_index("ix_archived_webhook_events_timestamp", "archived_webhook_events", ["timestamp"])
    op.create_index("ix_archived_webhook_events_alert_hash", "archived_webhook_events", ["alert_hash"])
    op.create_index("ix_archived_webhook_events_archived_at", "archived_webhook_events", ["archived_at"])
    op.create_index("idx_archived_hash_timestamp", "archived_webhook_events", ["alert_hash", "timestamp"])


def downgrade() -> None:
    if _has_table("archived_webhook_events"):
        op.drop_table("archived_webhook_events")
