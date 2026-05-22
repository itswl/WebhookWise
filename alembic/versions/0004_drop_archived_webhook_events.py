"""drop unused archived webhook events table

Revision ID: 0004_drop_archived_events
Revises: 0003_drop_system_configs
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_drop_archived_events"
down_revision: str | Sequence[str] | None = "0003_drop_system_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("archived_webhook_events"):
        op.drop_table("archived_webhook_events")


def downgrade() -> None:
    if not _has_table("archived_webhook_events"):
        op.create_table(
            "archived_webhook_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source", sa.String(length=100), nullable=False),
            sa.Column("client_ip", sa.String(length=50), nullable=True),
            sa.Column("timestamp", sa.DateTime(), nullable=False),
            sa.Column("raw_payload", sa.LargeBinary(), nullable=True),
            sa.Column("headers", sa.JSON(), nullable=True),
            sa.Column("parsed_data", sa.JSON(), nullable=True),
            sa.Column("alert_hash", sa.String(length=64), nullable=True),
            sa.Column("ai_analysis", sa.JSON(), nullable=True),
            sa.Column("importance", sa.String(length=20), nullable=True),
            sa.Column("forward_status", sa.String(length=20), nullable=True),
            sa.Column("is_duplicate", sa.Boolean(), nullable=True),
            sa.Column("duplicate_of", sa.Integer(), nullable=True),
            sa.Column("duplicate_count", sa.Integer(), nullable=True),
            sa.Column("beyond_window", sa.Boolean(), nullable=True),
            sa.Column("last_notified_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
        )
        op.create_index("idx_archived_hash_timestamp", "archived_webhook_events", ["alert_hash", "timestamp"])
