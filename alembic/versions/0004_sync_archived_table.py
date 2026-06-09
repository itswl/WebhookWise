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

_TABLE = "archived_webhook_events"

_COLUMNS = [
    ("processing_status", sa.String(length=20)),
    ("retry_count", sa.Integer()),
    ("next_retry_at", sa.DateTime()),
    ("failure_reason", sa.String(length=500)),
    ("error_message", sa.Text()),
    ("prev_alert_id", sa.BigInteger()),
    ("request_id", sa.String(length=64)),
]


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.scalar() is not None


def upgrade() -> None:
    for col_name, col_type in _COLUMNS:
        if not _column_exists(_TABLE, col_name):
            op.add_column(_TABLE, sa.Column(col_name, col_type, nullable=True))


def downgrade() -> None:
    op.drop_column("archived_webhook_events", "request_id")
    op.drop_column("archived_webhook_events", "prev_alert_id")
    op.drop_column("archived_webhook_events", "error_message")
    op.drop_column("archived_webhook_events", "failure_reason")
    op.drop_column("archived_webhook_events", "next_retry_at")
    op.drop_column("archived_webhook_events", "retry_count")
    op.drop_column("archived_webhook_events", "processing_status")
