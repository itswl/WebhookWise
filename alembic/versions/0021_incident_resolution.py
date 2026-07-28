"""structured incident resolution and recurrence review

Revision ID: 0021_incident_resolution
Revises: 0020_incident_response_loop
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021_incident_resolution"
down_revision: str | Sequence[str] | None = "0020_incident_response_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column(
            "resolution_record",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "incidents",
        sa.Column("resolution_record_updated_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "incident_recurrences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "previous_incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recurring_incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "match_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "recurring_incident_id",
            name="uq_incident_recurrences_recurring_incident",
        ),
    )
    op.create_index(
        op.f("ix_incident_recurrences_previous_incident_id"),
        "incident_recurrences",
        ["previous_incident_id"],
        unique=False,
    )
    op.create_index(
        "ix_incident_recurrences_status_detected",
        "incident_recurrences",
        ["status", "detected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_incident_recurrences_status_detected",
        table_name="incident_recurrences",
    )
    op.drop_index(
        op.f("ix_incident_recurrences_previous_incident_id"),
        table_name="incident_recurrences",
    )
    op.drop_table("incident_recurrences")
    op.drop_column("incidents", "resolution_record_updated_at")
    op.drop_column("incidents", "resolution_record")
