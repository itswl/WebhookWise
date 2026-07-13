"""performance indexes for FK lookups and source-filtered dead letters

Adds indexes flagged by review as hot-path sequential scans:
- forward_outboxes.original_event_id (FK, used in the dashboard OR-IN status join)
- webhook_events.duplicate_of (self-FK, used in duplicate lookups)
- rebuild the dead-letter partial index to lead with source so source-filtered
  dead-letter views use it instead of scanning.

Revision ID: 0002_perf_indexes
Revises: 0001_baseline
Create Date: 2026-06-17 00:00:00.000000
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0002_perf_indexes"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_forward_outboxes_original_event_id", "forward_outboxes", ["original_event_id"], unique=False)
    op.create_index("ix_webhook_events_duplicate_of", "webhook_events", ["duplicate_of"], unique=False)

    # Rebuild the dead-letter partial index: was ("id",), now ("source", "id")
    # so a source filter is index-served while id-desc ordering is preserved.
    op.drop_index("idx_webhook_events_dead_letter", table_name="webhook_events")
    op.create_index(
        "idx_webhook_events_dead_letter",
        "webhook_events",
        ["source", "id"],
        unique=False,
        postgresql_where=text("processing_status = 'dead_letter'"),
    )


def downgrade() -> None:
    op.drop_index("idx_webhook_events_dead_letter", table_name="webhook_events")
    op.create_index(
        "idx_webhook_events_dead_letter",
        "webhook_events",
        ["id"],
        unique=False,
        postgresql_where=text("processing_status = 'dead_letter'"),
    )
    op.drop_index("ix_webhook_events_duplicate_of", table_name="webhook_events")
    op.drop_index("ix_forward_outboxes_original_event_id", table_name="forward_outboxes")
