"""incident response-loop execution and action receipts

Revision ID: 0020_incident_response_loop
Revises: 0019_incident_intelligence
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020_incident_response_loop"
down_revision: str | Sequence[str] | None = "0019_incident_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_incidents_service_environment_started",
        "incidents",
        [
            sa.text("(correlation_dimensions ->> 'service')"),
            sa.text("(correlation_dimensions ->> 'environment')"),
            sa.text("started_at DESC"),
        ],
        unique=False,
        postgresql_where=sa.text("correlation_dimensions ? 'service' AND alert_count > 0"),
    )
    op.create_table(
        "runbook_executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Integer(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_ref", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "steps",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("effectiveness", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "incident_id",
            "candidate_ref",
            name="uq_runbook_executions_incident_candidate",
        ),
    )
    op.create_index(
        op.f("ix_runbook_executions_incident_id"),
        "runbook_executions",
        ["incident_id"],
        unique=False,
    )
    op.create_index(
        "ix_runbook_executions_incident_status",
        "runbook_executions",
        ["incident_id", "status", "started_at"],
        unique=False,
    )

    op.create_table(
        "integration_action_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "provider",
            "event_id",
            name="uq_integration_action_receipts_provider_event",
        ),
    )
    op.create_index(
        "ix_integration_action_receipts_resource",
        "integration_action_receipts",
        ["resource_type", "resource_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_action_receipts_resource",
        table_name="integration_action_receipts",
    )
    op.drop_table("integration_action_receipts")
    op.drop_index(
        "ix_runbook_executions_incident_status",
        table_name="runbook_executions",
    )
    op.drop_index(
        op.f("ix_runbook_executions_incident_id"),
        table_name="runbook_executions",
    )
    op.drop_table("runbook_executions")
    op.drop_index(
        "ix_incidents_service_environment_started",
        table_name="incidents",
    )
