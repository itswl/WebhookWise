"""logic sinking: add unique constraints and processing_locks

Consolidate manual migrations from scripts/migrate_db.py and 
scripts/apply_unique_constraint.py into Alembic.

Revision ID: 6a7b8c9d0e1f
Revises: 9c0b7c3e2a11
Create Date: 2026-05-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision: str = "6a7b8c9d0e1f"
down_revision: str | Sequence[str] | None = "9c0b7c3e2a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    
    conn = op.get_bind()
    insp = inspect(conn)
    
    # 1. 确保 webhook_events 字段完整 (来自 migrate_db.py)
    columns = [c["name"] for c in insp.get_columns("webhook_events")]
    if "is_duplicate" not in columns:
        op.add_column("webhook_events", sa.Column("is_duplicate", sa.Integer(), server_default="0"))
    if "duplicate_of" not in columns:
        op.add_column("webhook_events", sa.Column("duplicate_of", sa.Integer(), nullable=True))
    if "duplicate_count" not in columns:
        op.add_column("webhook_events", sa.Column("duplicate_count", sa.Integer(), server_default="1"))
    if "beyond_window" not in columns:
        op.add_column("webhook_events", sa.Column("beyond_window", sa.Integer(), server_default="0"))

    # 2. 创建唯一索引 idx_unique_alert_hash_original (来自 apply_unique_constraint.py)
    # 必须先确保没有冲突数据（逻辑已在脚本中处理，这里假设用户已清理或在迁移中尝试）
    # 为安全起见使用 IF NOT EXISTS (通过 SQL 原生执行)
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_alert_hash_original "
            "ON webhook_events (alert_hash) "
            "WHERE is_duplicate = 0"
        )
    )

    # 3. 创建 processing_locks 表 (来自 migrate_db.py)
    if "processing_locks" not in insp.get_table_names():
        op.create_table(
            "processing_locks",
            sa.Column("alert_hash", sa.String(64), primary_key=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
            sa.Column("worker_id", sa.String(100), nullable=True),
        )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.drop_table("processing_locks")
    op.execute(sa.text("DROP INDEX IF EXISTS idx_unique_alert_hash_original"))
    # 不建议删除 is_duplicate 等核心业务列，因为它们可能已包含数据
