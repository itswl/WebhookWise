"""add partial indexes for recovery poller and failed forwards

Uses CREATE INDEX CONCURRENTLY to avoid blocking concurrent reads/writes.
CONCURRENTLY cannot run inside a transaction block, so we COMMIT the
implicit Alembic transaction before executing, then BEGIN a new one
so Alembic can record the migration version normally.

Revision ID: e1a4f7b3d926
Revises: d5a2b3c4e6f7
Create Date: 2026-04-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a4f7b3d926"
down_revision: str | Sequence[str] | None = "d5a2b3c4e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add partial indexes using CONCURRENTLY to avoid locking tables."""
    # Exit the implicit transaction — CONCURRENTLY cannot run inside a txn
    op.execute(sa.text("COMMIT"))

    # RecoveryPoller 僵尸事件扫描优化
    # 覆盖 processing_status IN ('received','analyzing','failed') 的行，
    # 按 created_at DESC 排序，加速 ORDER BY + LIMIT 查询
    op.execute(
        sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_status_created_partial "
            "ON webhook_events (processing_status, created_at DESC) "
            "WHERE processing_status IN ('received', 'analyzing', 'failed')"
        )
    )

    # 转发失败重试查询优化
    # 覆盖 status IN ('pending','retrying') 的行，按 next_retry_at 排序
    op.execute(
        sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_failed_status_retry_partial "
            "ON failed_forwards (status, next_retry_at) "
            "WHERE status IN ('pending', 'retrying')"
        )
    )

    # Re-enter transaction for Alembic version-table bookkeeping
    op.execute(sa.text("BEGIN"))


def downgrade() -> None:
    """Remove partial indexes."""
    op.execute(sa.text("DROP INDEX IF EXISTS idx_status_created_partial"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_failed_status_retry_partial"))
