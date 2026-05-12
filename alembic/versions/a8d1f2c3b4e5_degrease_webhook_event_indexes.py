"""degrease webhook event indexes

Revision ID: a8d1f2c3b4e5
Revises: 7f8a9b0c1d2e
Create Date: 2026-05-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8d1f2c3b4e5"
down_revision: str | Sequence[str] | None = "7f8a9b0c1d2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET lock_timeout = '5s'"))
    for index_name in (
        "idx_unique_alert_hash_original",
        "idx_importance_timestamp",
        "idx_duplicate_lookup",
        "idx_status_created",
        "idx_status_created_partial",
        "idx_retry_due",
        "idx_source_timestamp_id",
        "ix_webhook_events_source",
        "ix_webhook_events_importance",
        "ix_webhook_events_processing_status",
    ):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {index_name}"))


def downgrade() -> None:
    op.execute(sa.text("SET lock_timeout = '5s'"))
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original "
            "ON webhook_events (alert_hash) WHERE is_duplicate = false"
        )
    )
    op.execute(
        sa.text("CREATE INDEX IF NOT EXISTS idx_importance_timestamp " "ON webhook_events (importance, timestamp)")
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_duplicate_lookup " "ON webhook_events (alert_hash, is_duplicate, timestamp)"
        )
    )
    op.execute(
        sa.text("CREATE INDEX IF NOT EXISTS idx_status_created " "ON webhook_events (processing_status, created_at)")
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_status_created_partial "
            "ON webhook_events (processing_status, created_at DESC) "
            "WHERE processing_status IN ('received', 'analyzing', 'failed')"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_retry_due "
            "ON webhook_events (next_retry_at) WHERE processing_status = 'retry'"
        )
    )
    op.execute(
        sa.text("CREATE INDEX IF NOT EXISTS idx_source_timestamp_id " "ON webhook_events (source, timestamp, id)")
    )
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_webhook_events_source ON webhook_events (source)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_webhook_events_importance ON webhook_events (importance)"))
    op.execute(
        sa.text("CREATE INDEX IF NOT EXISTS ix_webhook_events_processing_status ON webhook_events (processing_status)")
    )
