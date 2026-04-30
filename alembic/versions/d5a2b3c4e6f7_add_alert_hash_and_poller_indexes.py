"""Add alert_hash column and poller composite indexes.

Covers DDL previously scattered across migrations/migrate_db.py and
core/app.py _ensure_schema(). All operations are idempotent.

Revision ID: d5a2b3c4e6f7
Revises: 8f3991b7992c
Create Date: 2026-04-30
"""

from sqlalchemy import inspect

from alembic import op

revision = "d5a2b3c4e6f7"
down_revision = "8f3991b7992c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    conn = op.get_bind()
    insp = inspect(conn)

    # ── 1. alert_hash 列 ──
    columns = [c["name"] for c in insp.get_columns("webhook_events")]
    if "alert_hash" not in columns:
        op.execute("ALTER TABLE webhook_events " "ADD COLUMN alert_hash VARCHAR(64)")

    # ── 2. idx_alert_hash 索引 ──
    we_indexes = [idx["name"] for idx in insp.get_indexes("webhook_events")]
    if "idx_alert_hash" not in we_indexes:
        op.execute("CREATE INDEX idx_alert_hash " "ON webhook_events(alert_hash)")

    # ── 3. deep_analyses 复合索引（Poller 查询优化）──
    da_indexes = [idx["name"] for idx in insp.get_indexes("deep_analyses")]
    if "idx_deep_analyses_status_created" not in da_indexes:
        op.execute("CREATE INDEX idx_deep_analyses_status_created " "ON deep_analyses(status, created_at)")

    # ── 4. failed_forwards 复合索引（Poller 查询优化）──
    ff_indexes = [idx["name"] for idx in insp.get_indexes("failed_forwards")]
    if "idx_failed_forwards_status_next_retry" not in ff_indexes:
        op.execute("CREATE INDEX idx_failed_forwards_status_next_retry " "ON failed_forwards(status, next_retry_at)")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_failed_forwards_status_next_retry")
    op.execute("DROP INDEX IF EXISTS idx_deep_analyses_status_created")
    op.execute("DROP INDEX IF EXISTS idx_alert_hash")
    op.execute("ALTER TABLE webhook_events " "DROP COLUMN IF EXISTS alert_hash")
