"""forward_outboxes: a noisy alert rule can be batched into a digest

Two business-threshold alert rules were 56% of a week's volume (187 of 331
alerts), each firing a chat card of its own. An inbound rule with
action=digest now batches those chat deliveries into one card per window. The
outbox row is still written per alert — the per-alert record is the point of
the outbox — but it carries the group it belongs to and waits for the window to
close:

  * digest_key — "<forward_rule_id>:<target_type>:<window start>", indexed so
    the first row claimed can find its siblings with one conditional UPDATE.
  * digest_window_end — when the group is due; next_attempt_at is set to it.

Both nullable: a row without a digest_key is delivered exactly as before.

Revision ID: 0039_forward_outbox_digest
Revises: 0038_da_pseudonym_map
Create Date: 2026-09-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_forward_outbox_digest"
down_revision: str | Sequence[str] | None = "0038_da_pseudonym_map"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("forward_outboxes", sa.Column("digest_key", sa.String(length=160), nullable=True))
    op.add_column("forward_outboxes", sa.Column("digest_window_end", sa.DateTime(), nullable=True))
    op.create_index("ix_forward_outboxes_digest_key", "forward_outboxes", ["digest_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_forward_outboxes_digest_key", table_name="forward_outboxes")
    op.drop_column("forward_outboxes", "digest_window_end")
    op.drop_column("forward_outboxes", "digest_key")
