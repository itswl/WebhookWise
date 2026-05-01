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
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_status_created_partial "
                "ON webhook_events (processing_status, created_at DESC) "
                "WHERE processing_status IN ('received', 'analyzing', 'failed')"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_failed_status_retry_partial "
                "ON failed_forwards (status, next_retry_at) "
                "WHERE status IN ('pending', 'retrying')"
            )
        )


def downgrade() -> None:
    """Remove partial indexes."""
    op.execute(sa.text("DROP INDEX IF EXISTS idx_status_created_partial"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_failed_status_retry_partial"))
