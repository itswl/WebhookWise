"""drop redundant webhook_events indexes and orphaned legacy tables

Live pg_stat_user_indexes evidence (2026-07-14 production):
- ix_webhook_events_alert_hash and the legacy idx_alert_hash are single-column
  duplicates of idx_hash_timestamp's leading column — the composite serves
  every alert_hash lookup, so both singles only tax inserts on the
  highest-write table.
- idx_webhook_importance_timestamp was scanned 11 times over the database's
  lifetime and is superseded by idx_webhook_events_importance_id (0014).
- analysis_cache is a dead table (zero code references; superseded by the
  Redis AI cache) and the timezone_fix_backup_20260525_* pair is debris from a
  one-off manual fix. None of the three are alembic-managed (created manually
  or pre-baseline), hence IF EXISTS.

Revision ID: 0015_index_diet_and_debris
Revises: 0014_read_path_indexes
Create Date: 2026-07-14 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_index_diet_and_debris"
down_revision: str | Sequence[str] | None = "0014_read_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Baseline-managed duplicate (from the old index=True on the column).
    op.execute("DROP INDEX IF EXISTS ix_webhook_events_alert_hash")
    # Legacy production-only objects — never present on fresh installs.
    op.execute("DROP INDEX IF EXISTS idx_alert_hash")
    op.execute("DROP INDEX IF EXISTS idx_webhook_importance_timestamp")
    op.execute("DROP TABLE IF EXISTS analysis_cache")
    op.execute("DROP TABLE IF EXISTS timezone_fix_backup_20260525_webhook_events")
    op.execute("DROP TABLE IF EXISTS timezone_fix_backup_20260525_archived_webhook_events")


def downgrade() -> None:
    # Only the alembic-managed index is restored; the legacy objects and
    # orphaned tables are intentionally gone for good.
    op.create_index(op.f("ix_webhook_events_alert_hash"), "webhook_events", ["alert_hash"], unique=False)
