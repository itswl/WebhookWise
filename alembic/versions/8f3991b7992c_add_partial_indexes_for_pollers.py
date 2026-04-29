"""add partial indexes for pollers

Revision ID: 8f3991b7992c
Revises: c7d9e3f2a5b8
Create Date: 2026-04-29 22:42:33.229496

"""

from typing import Sequence, Union

from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f3991b7992c"
down_revision: Union[str, Sequence[str], None] = "c7d9e3f2a5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET lock_timeout = '5s'")

    conn = op.get_bind()
    insp = inspect(conn)

    # 1. idx_deep_analyses_pending
    deep_indexes = [idx["name"] for idx in insp.get_indexes("deep_analyses")]
    if "idx_deep_analyses_pending" not in deep_indexes:
        op.execute(
            "CREATE INDEX idx_deep_analyses_pending " "ON deep_analyses (created_at) " "WHERE status = 'pending'"
        )

    # 2. idx_failed_forwards_pending
    failed_indexes = [idx["name"] for idx in insp.get_indexes("failed_forwards")]
    if "idx_failed_forwards_pending" not in failed_indexes:
        op.execute(
            "CREATE INDEX idx_failed_forwards_pending "
            "ON failed_forwards (next_retry_at) "
            "WHERE status IN ('pending', 'retrying')"
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_deep_analyses_pending")
    op.execute("DROP INDEX IF EXISTS idx_failed_forwards_pending")
