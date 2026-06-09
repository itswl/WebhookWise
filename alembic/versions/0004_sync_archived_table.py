"""Sync archived_webhook_events schema with webhook_events.

Adds missing columns that were added to webhook_events after the archived
table was initially created.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_sync_archived_table"
down_revision = "0003_server_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("archived_webhook_events", sa.Column("processing_status", sa.String(length=20), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("retry_count", sa.Integer(), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("next_retry_at", sa.DateTime(), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("failure_reason", sa.String(length=500), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("prev_alert_id", sa.BigInteger(), nullable=True))
    op.add_column("archived_webhook_events", sa.Column("request_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("archived_webhook_events", "request_id")
    op.drop_column("archived_webhook_events", "prev_alert_id")
    op.drop_column("archived_webhook_events", "error_message")
    op.drop_column("archived_webhook_events", "failure_reason")
    op.drop_column("archived_webhook_events", "next_retry_at")
    op.drop_column("archived_webhook_events", "retry_count")
    op.drop_column("archived_webhook_events", "processing_status")
