"""Add operator workflow, feedback, and incident correlation state.

Revision ID: 0012_operator_workflow
Revises: 0011_incident_members_and_search
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_operator_workflow"
down_revision: str | Sequence[str] | None = "0011_incident_members_and_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_workflow_columns(table_name: str, *, archived: bool = False) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "workflow_status",
            sa.String(length=20),
            nullable=archived,
            server_default=None if archived else "open",
        ),
    )
    op.add_column(table_name, sa.Column("assignee", sa.String(length=100), nullable=True))
    op.add_column(table_name, sa.Column("team", sa.String(length=100), nullable=True))
    op.add_column(table_name, sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column(table_name, sa.Column("resolved_at", sa.DateTime(), nullable=True))
    op.add_column(table_name, sa.Column("sla_due_at", sa.DateTime(), nullable=True))


def upgrade() -> None:
    _add_workflow_columns("webhook_events")
    _add_workflow_columns("archived_webhook_events", archived=True)
    _add_workflow_columns("incidents")
    op.create_index("ix_webhook_events_sla_due_at", "webhook_events", ["sla_due_at"])
    op.create_index("ix_incidents_sla_due_at", "incidents", ["sla_due_at"])
    op.create_index(
        "ix_incidents_sla_open",
        "incidents",
        ["sla_due_at"],
        postgresql_where=text("workflow_status NOT IN ('resolved', 'ignored')"),
    )

    op.add_column(
        "incidents",
        sa.Column(
            "correlation_dimensions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "incidents",
        sa.Column("correlation_confidence", sa.Float(), nullable=False, server_default="0"),
    )

    op.create_table(
        "operational_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operational_notes_resource",
        "operational_notes",
        ["resource_type", "resource_id", "created_at"],
    )
    op.create_table(
        "analysis_feedback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=30), nullable=False),
        sa.Column("corrected_importance", sa.String(length=20), nullable=True),
        sa.Column("corrected_event_type", sa.String(length=100), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_feedback_resource",
        "analysis_feedback",
        ["resource_type", "resource_id", "created_at"],
    )
    op.create_index(
        "ix_analysis_feedback_verdict_created",
        "analysis_feedback",
        ["verdict", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_feedback_verdict_created", table_name="analysis_feedback")
    op.drop_index("ix_analysis_feedback_resource", table_name="analysis_feedback")
    op.drop_table("analysis_feedback")
    op.drop_index("ix_operational_notes_resource", table_name="operational_notes")
    op.drop_table("operational_notes")
    op.drop_column("incidents", "correlation_confidence")
    op.drop_column("incidents", "correlation_dimensions")
    op.drop_index("ix_incidents_sla_open", table_name="incidents")
    op.drop_index("ix_incidents_sla_due_at", table_name="incidents")
    op.drop_index("ix_webhook_events_sla_due_at", table_name="webhook_events")
    for table_name in ("incidents", "archived_webhook_events", "webhook_events"):
        for column_name in (
            "sla_due_at",
            "resolved_at",
            "acknowledged_at",
            "team",
            "assignee",
            "workflow_status",
        ):
            op.drop_column(table_name, column_name)
