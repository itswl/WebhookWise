"""Add a partial index for dead-letter webhook events.

The dead-letter list/count queries filter on processing_status='dead_letter'.
Without an index this is a full sequential scan of webhook_events. A partial
index keyed on id (for the id-desc ordering) covers it cheaply, mirroring the
existing pending partial indexes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-12
"""

from alembic import op

revision = "0005_dead_letter_index"
down_revision = "0004_sync_archived_table"
branch_labels = None
depends_on = None

_INDEX = "idx_webhook_events_dead_letter"
_TABLE = "webhook_events"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        _TABLE,
        ["id"],
        unique=False,
        postgresql_where="processing_status = 'dead_letter'",
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
