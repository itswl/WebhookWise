"""add AI-judgment quality columns to decision_trace

Flatten three signals from the analysis step so the dashboard can aggregate AI
judgment quality without unpacking JSONB: the analysis route (only "ai" is a
fresh LLM judgment), whether a deterministic rule overrode the AI's importance,
and the degradation reason (NULL when not degraded).

Revision ID: 0006_decision_trace_ai_quality
Revises: 0005_decision_trace
Create Date: 2026-06-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0006_decision_trace_ai_quality"
down_revision: str | Sequence[str] | None = "0005_decision_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decision_trace", sa.Column("route", sa.String(length=20), nullable=True))
    op.add_column(
        "decision_trace",
        sa.Column("importance_override", sa.Boolean(), server_default=text("false"), nullable=False),
    )
    op.add_column("decision_trace", sa.Column("degraded_reason", sa.String(length=200), nullable=True))
    op.create_index("ix_decision_trace_route", "decision_trace", ["route"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_decision_trace_route", table_name="decision_trace")
    op.drop_column("decision_trace", "degraded_reason")
    op.drop_column("decision_trace", "importance_override")
    op.drop_column("decision_trace", "route")
