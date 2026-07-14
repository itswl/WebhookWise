"""read-path indexes for list filters, rule delivery health, and source aggregates

Adds indexes flagged by review as unindexed hot read paths:
- webhook_events (source, id) / (processing_status, id) / (importance, id):
  the alert list filters on these exact columns and orders by id DESC; without
  them a selective filter walks the reverse PK discarding rows.
- forward_outboxes (forward_rule_id, updated_at): the forward-rules health
  panel partitions a row_number() window by rule and counts recent failures
  per rule over an otherwise unindexed FK column.
- decision_trace (source, created_at): overview/quality endpoints GROUP BY
  source over a created_at window (the id-ordered trace list is NOT served by
  this index; it paginates via the primary key).

Revision ID: 0014_read_path_indexes
Revises: 0013_noise_reduction_actions
Create Date: 2026-07-14 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_read_path_indexes"
down_revision: str | Sequence[str] | None = "0013_noise_reduction_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("idx_webhook_events_source_id", "webhook_events", ["source", "id"], unique=False)
    op.create_index(
        "idx_webhook_events_processing_status_id", "webhook_events", ["processing_status", "id"], unique=False
    )
    op.create_index("idx_webhook_events_importance_id", "webhook_events", ["importance", "id"], unique=False)
    op.create_index(
        "idx_forward_outboxes_rule_updated", "forward_outboxes", ["forward_rule_id", "updated_at"], unique=False
    )
    op.create_index("ix_decision_trace_source_created_at", "decision_trace", ["source", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_decision_trace_source_created_at", table_name="decision_trace")
    op.drop_index("idx_forward_outboxes_rule_updated", table_name="forward_outboxes")
    op.drop_index("idx_webhook_events_importance_id", table_name="webhook_events")
    op.drop_index("idx_webhook_events_processing_status_id", table_name="webhook_events")
    op.drop_index("idx_webhook_events_source_id", table_name="webhook_events")
