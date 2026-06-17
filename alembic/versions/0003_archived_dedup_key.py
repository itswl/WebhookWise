"""add dedup_key to archived_webhook_events

Mirror the live webhook_events.dedup_key onto the archive so a dedup chain can
be reconstructed from archived rows for forensics. Nullable; rows archived
before this column existed stay NULL (no backfill — their dedup_key is simply
not recoverable post-archive).

Revision ID: 0003_archived_dedup_key
Revises: 0002_perf_indexes
Create Date: 2026-06-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_archived_dedup_key"
down_revision: str | Sequence[str] | None = "0002_perf_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("archived_webhook_events", sa.Column("dedup_key", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("archived_webhook_events", "dedup_key")
