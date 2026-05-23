"""add dedup_key to webhook_events

Revision ID: 0009_add_dedup_key
Revises: 0008_create_suppressed_records
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_add_dedup_key"
down_revision: str | Sequence[str] | None = "0008_create_suppressed_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("dedup_key", sa.String(length=64), nullable=True))
    op.create_index("idx_dedup_key_timestamp", "webhook_events", ["dedup_key", "timestamp"])
    op.execute(sa.text("UPDATE webhook_events SET dedup_key = alert_hash WHERE dedup_key IS NULL"))


def downgrade() -> None:
    op.drop_index("idx_dedup_key_timestamp", table_name="webhook_events")
    op.drop_column("webhook_events", "dedup_key")
