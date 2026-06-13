"""Drop the redundant forward_outboxes.webhook_event_id index.

webhook_event_id is indexed twice: ix_forward_outboxes_webhook_event_id (from
index=True on the column) and idx_forward_outboxes_event (an explicit Index on
the same single column). They are duplicates; keep the column-level one and
drop the explicit one to remove the redundant write/storage cost.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-13
"""

from alembic import op

revision = "0006_drop_duplicate_outbox_index"
down_revision = "0005_dead_letter_index"
branch_labels = None
depends_on = None

_DUP_INDEX = "idx_forward_outboxes_event"
_COL_INDEX = "ix_forward_outboxes_webhook_event_id"
_TABLE = "forward_outboxes"
_COLUMN = "webhook_event_id"


def upgrade() -> None:
    # IF EXISTS keeps this idempotent across environments that may have already
    # diverged (e.g. a DB created from the ORM without the explicit index).
    op.execute(f"DROP INDEX IF EXISTS {_DUP_INDEX}")


def downgrade() -> None:
    op.create_index(_DUP_INDEX, _TABLE, [_COLUMN], unique=False)
