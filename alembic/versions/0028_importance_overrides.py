"""importance_overrides: make a correction apply to the next occurrence

Correcting an alert's importance changed that one row and nothing else. The
same condition fired again an hour later and the model called it `low` again,
because nothing in the analysis path had ever read a correction — zero
references, checked. The feedback surface promised teaching and delivered a
row in a table.

Keyed on alert_hash, the identity a condition keeps across occurrences, so one
correction covers every future firing of that thing and nothing else.

Deliberately not few-shot in the prompt: with a handful of samples that
teaches nothing, and with many it would move judgements on alerts nobody
corrected, in a way nobody could trace back to a decision.

Revision ID: 0028_importance_overrides
Revises: 0027_workflow_transitions
Create Date: 2026-08-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_importance_overrides"
down_revision: str | Sequence[str] | None = "0027_workflow_transitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "importance_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_hash", sa.String(length=64), nullable=False),
        sa.Column("importance", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("alert_name", sa.String(length=200), nullable=True),
        sa.Column("origin_event_id", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=100), server_default="dashboard", nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_applied_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # One override per condition: a second correction updates the first rather
    # than racing it, so which one wins is never a question of insert order.
    op.create_index(
        "ix_importance_overrides_alert_hash",
        "importance_overrides",
        ["alert_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_importance_overrides_alert_hash", table_name="importance_overrides")
    op.drop_table("importance_overrides")
