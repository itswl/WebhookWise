"""migrate json columns to jsonb

Revision ID: 582744d1c390
Revises: f8894c5c7e15
Create Date: 2026-04-29 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "582744d1c390"
down_revision: str | Sequence[str] | None = "f8894c5c7e15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Migrate all JSON columns to JSONB for better performance and indexing."""
    # webhook_events
    op.alter_column(
        "webhook_events",
        "headers",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )
    op.alter_column(
        "webhook_events",
        "parsed_data",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )
    op.alter_column(
        "webhook_events",
        "ai_analysis",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )

    # archived_webhook_events
    op.alter_column(
        "archived_webhook_events",
        "headers",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )
    op.alter_column(
        "archived_webhook_events",
        "parsed_data",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )
    op.alter_column(
        "archived_webhook_events",
        "ai_analysis",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )

    # deep_analyses
    op.alter_column(
        "deep_analyses",
        "analysis_result",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )

    # failed_forwards
    op.alter_column(
        "failed_forwards",
        "forward_data",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )
    op.alter_column(
        "failed_forwards",
        "forward_headers",
        type_=postgresql.JSONB(),
        existing_type=sa.JSON(),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Revert JSONB columns back to JSON."""
    # failed_forwards
    op.alter_column(
        "failed_forwards",
        "forward_headers",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )
    op.alter_column(
        "failed_forwards",
        "forward_data",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )

    # deep_analyses
    op.alter_column(
        "deep_analyses",
        "analysis_result",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )

    # archived_webhook_events
    op.alter_column(
        "archived_webhook_events",
        "ai_analysis",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )
    op.alter_column(
        "archived_webhook_events",
        "parsed_data",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )
    op.alter_column(
        "archived_webhook_events",
        "headers",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )

    # webhook_events
    op.alter_column(
        "webhook_events",
        "ai_analysis",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )
    op.alter_column(
        "webhook_events",
        "parsed_data",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )
    op.alter_column(
        "webhook_events",
        "headers",
        type_=sa.JSON(),
        existing_type=postgresql.JSONB(),
        existing_nullable=True,
    )
