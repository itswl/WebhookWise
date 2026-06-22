"""add silence_id column to decision_trace (Silence ROI panel)

Flatten the matched silence id out of the ``steps`` JSONB into an indexed
column so the dashboard can answer "how many alerts has each silence rule
suppressed" with a cheap GROUP BY, mirroring the existing skip_code/route
flattening. A partial index (only the silenced rows carry a silence_id) keeps
it small. Backfills historical silenced rows from the steps JSONB so the panel
has data the moment it ships.

Revision ID: 0008_decision_trace_silence_id
Revises: 0007_kb_documents
Create Date: 2026-06-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0008_decision_trace_silence_id"
down_revision: str | Sequence[str] | None = "0007_kb_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decision_trace", sa.Column("silence_id", sa.Integer(), nullable=True))
    # Partial index: only silenced traces carry a silence_id, so indexing the
    # NULL majority would waste space (mirrors idx_silences_active).
    op.create_index(
        "ix_decision_trace_silence_id",
        "decision_trace",
        ["silence_id"],
        unique=False,
        postgresql_where=text("silence_id IS NOT NULL"),
    )
    # Backfill from the steps JSONB so historical silenced rows count toward each
    # rule's ROI immediately. Postgres-only (JSONB functions); other dialects
    # (tests use create_all, never this migration) just start empty.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            text(
                """
                UPDATE decision_trace dt
                SET silence_id = (
                    SELECT (elem ->> 'silence_id')::int
                    FROM jsonb_array_elements(dt.steps) AS elem
                    WHERE elem ->> 'step' = 'silence'
                      AND elem ->> 'silence_id' IS NOT NULL
                    LIMIT 1
                )
                WHERE dt.skip_code = 'silenced'
                  AND dt.silence_id IS NULL
                  AND dt.steps IS NOT NULL
                """
            )
        )


def downgrade() -> None:
    op.drop_index("ix_decision_trace_silence_id", table_name="decision_trace")
    op.drop_column("decision_trace", "silence_id")
