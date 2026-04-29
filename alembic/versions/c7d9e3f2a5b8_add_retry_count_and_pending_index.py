"""Add retry_count column and idx_pending_webhooks partial index.

Revision ID: c7d9e3f2a5b8
Revises: b4c8d2e6f1a3
Create Date: 2026-04-29
"""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "c7d9e3f2a5b8"
down_revision = "b4c8d2e6f1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    # 幂等：仅在列不存在时添加
    conn = op.get_bind()
    insp = inspect(conn)
    columns = [c["name"] for c in insp.get_columns("webhook_events")]

    if "retry_count" not in columns:
        op.add_column(
            "webhook_events",
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        )

    # 幂等：仅在索引不存在时创建
    indexes = [idx["name"] for idx in insp.get_indexes("webhook_events")]
    if "idx_pending_webhooks" not in indexes:
        op.execute(
            "CREATE INDEX idx_pending_webhooks "
            "ON webhook_events (created_at) "
            "WHERE processing_status IN ('received', 'analyzing', 'failed')"
        )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_pending_webhooks")
    op.drop_column("webhook_events", "retry_count")
