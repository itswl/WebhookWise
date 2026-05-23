"""forward rule match_payload

Revision ID: 0007_forward_rule_match_payload
Revises: 0006_channel_outbox
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_forward_rule_match_payload"
down_revision: str | Sequence[str] | None = "0006_channel_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return False
    return any(col.get("name") == column for col in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_table("forward_rules"):
        return
    with op.batch_alter_table("forward_rules") as batch:
        if not _has_column("forward_rules", "match_payload"):
            batch.add_column(sa.Column("match_payload", sa.String(length=512), server_default="", nullable=False))


def downgrade() -> None:
    if not _has_table("forward_rules"):
        return
    with op.batch_alter_table("forward_rules") as batch:
        if _has_column("forward_rules", "match_payload"):
            batch.drop_column("match_payload")

