"""drop database-backed configuration table

Revision ID: 0003_drop_system_configs
Revises: 0002_pluralize_tables
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_drop_system_configs"
down_revision: str | Sequence[str] | None = "0002_pluralize_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("system_configs"):
        op.drop_table("system_configs")


def downgrade() -> None:
    if not _has_table("system_configs"):
        op.create_table(
            "system_configs",
            sa.Column("key", sa.String(length=128), primary_key=True, comment="配置键名（环境变量名）"),
            sa.Column("value", sa.Text(), nullable=False, comment="配置值（统一字符串存储）"),
            sa.Column("value_type", sa.String(length=16), nullable=False, server_default="str", comment="值类型"),
            sa.Column("description", sa.Text(), nullable=True, comment="配置说明"),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_by", sa.String(length=64), server_default="system", nullable=False),
        )
