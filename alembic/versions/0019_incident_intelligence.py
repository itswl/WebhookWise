"""incident intelligence change inputs and feedback

Revision ID: 0019_incident_intelligence
Revises: 0018_archival_dependency_index
Create Date: 2026-07-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019_incident_intelligence"
down_revision: str | Sequence[str] | None = "0018_archival_dependency_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("change_type", sa.String(length=40), nullable=False),
        sa.Column("project", sa.String(length=200), nullable=True),
        sa.Column("environment", sa.String(length=200), nullable=True),
        sa.Column("service", sa.String(length=200), nullable=True),
        sa.Column("region", sa.String(length=200), nullable=True),
        sa.Column("resource_type", sa.String(length=100), nullable=True),
        sa.Column("resource_id", sa.String(length=200), nullable=True),
        sa.Column("version_from", sa.String(length=200), nullable=True),
        sa.Column("version_to", sa.String(length=200), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source", "external_id", name="uq_change_events_source_external_id"),
    )
    op.create_index(op.f("ix_change_events_started_at"), "change_events", ["started_at"], unique=False)
    op.create_index(
        "ix_change_events_service_environment_started",
        "change_events",
        ["service", "environment", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_change_events_project_region_started",
        "change_events",
        ["project", "region", "started_at"],
        unique=False,
    )

    op.create_table(
        "incident_intelligence_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recommendation_type", sa.String(length=30), nullable=False),
        sa.Column("candidate_ref", sa.String(length=500), nullable=False),
        sa.Column("verdict", sa.String(length=30), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "incident_id",
            "recommendation_type",
            "candidate_ref",
            name="uq_incident_intelligence_feedback_candidate",
        ),
    )
    op.create_index(
        op.f("ix_incident_intelligence_feedback_incident_id"),
        "incident_intelligence_feedback",
        ["incident_id"],
        unique=False,
    )
    op.create_index(
        "ix_incident_intelligence_feedback_lookup",
        "incident_intelligence_feedback",
        ["incident_id", "recommendation_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_incident_intelligence_feedback_lookup",
        table_name="incident_intelligence_feedback",
    )
    op.drop_index(
        op.f("ix_incident_intelligence_feedback_incident_id"),
        table_name="incident_intelligence_feedback",
    )
    op.drop_table("incident_intelligence_feedback")

    op.drop_index("ix_change_events_project_region_started", table_name="change_events")
    op.drop_index("ix_change_events_service_environment_started", table_name="change_events")
    op.drop_index(op.f("ix_change_events_started_at"), table_name="change_events")
    op.drop_table("change_events")
