"""maintenance-window occurrence identity columns on silences

The sweep previously identified a materialized occurrence by a comment-string
marker containing only window id + date. That made the identity blind to the
window's SCHEDULE: editing a window mid-occurrence lifted the old silence and
then refused to re-materialize (the marker lookup matched the lifted row), so
the edited window silently never muted again that day. Real columns carry the
identity — including a schedule digest, so an edit produces a NEW identity —
and a partial unique index makes concurrent sweeps race-safe (INSERT + unique
violation → skip instead of duplicate silences).

Revision ID: 0023_mw_occurrence_identity
Revises: 0022_source_onboarding
Create Date: 2026-07-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_mw_occurrence_identity"
down_revision: str | Sequence[str] | None = "0022_source_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("silences", sa.Column("mw_window_id", sa.Integer(), nullable=True))
    op.add_column("silences", sa.Column("mw_occurrence_date", sa.String(length=10), nullable=True))
    op.add_column("silences", sa.Column("mw_schedule_digest", sa.String(length=16), nullable=True))
    op.create_index(
        "uq_silences_mw_occurrence",
        "silences",
        ["mw_window_id", "mw_occurrence_date", "mw_schedule_digest"],
        unique=True,
        postgresql_where=sa.text("mw_window_id IS NOT NULL"),
        sqlite_where=sa.text("mw_window_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_silences_mw_occurrence", table_name="silences")
    op.drop_column("silences", "mw_schedule_digest")
    op.drop_column("silences", "mw_occurrence_date")
    op.drop_column("silences", "mw_window_id")
