"""create suppressed records

Revision ID: 0008_create_suppressed_records
Revises: 0007_forward_rule_match_payload
Create Date: 2026-05-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_create_suppressed_records"
down_revision: str | Sequence[str] | None = "0007_forward_rule_match_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("suppressed_records"):
        return
    op.create_table(
        "suppressed_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("alert_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False, server_default="standalone"),
        sa.Column("root_cause_event_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("related_alert_ids", _jsonb(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_suppressed_records_created_at", "suppressed_records", ["created_at"])
    op.create_index(
        "idx_suppressed_records_hash_created",
        "suppressed_records",
        ["alert_hash", "created_at"],
    )


def downgrade() -> None:
    if _has_table("suppressed_records"):
        op.drop_table("suppressed_records")

