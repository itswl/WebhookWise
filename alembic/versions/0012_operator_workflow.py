"""Add operator workflow, feedback, and incident correlation state.

Revision ID: 0012_operator_workflow
Revises: 0011_incident_members_and_search
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision: str = "0012_operator_workflow"
down_revision: str | Sequence[str] | None = "0011_incident_members_and_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_columns(table_name: str) -> set[str]:
    if context.is_offline_mode():
        return set()
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _add_columns_if_missing(
    table_name: str,
    columns: Sequence[sa.Column[object]],
) -> None:
    existing = _existing_columns(table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)
            existing.add(column.name)


def _index_exists(table_name: str, index_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(index["name"] == index_name for index in sa.inspect(op.get_bind()).get_indexes(table_name))


def _table_exists(table_name: str) -> bool:
    return not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(table_name)


def _add_workflow_columns(table_name: str, *, archived: bool = False) -> None:
    _add_columns_if_missing(
        table_name,
        [
            sa.Column(
                "workflow_status",
                sa.String(length=20),
                nullable=archived,
                server_default=None if archived else "open",
            ),
            sa.Column("assignee", sa.String(length=100), nullable=True),
            sa.Column("team", sa.String(length=100), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("sla_due_at", sa.DateTime(), nullable=True),
        ],
    )


def upgrade() -> None:
    _add_workflow_columns("webhook_events")
    _add_workflow_columns("archived_webhook_events", archived=True)
    _add_workflow_columns("incidents")
    if not _index_exists("webhook_events", "ix_webhook_events_sla_due_at"):
        op.create_index("ix_webhook_events_sla_due_at", "webhook_events", ["sla_due_at"])
    if not _index_exists("incidents", "ix_incidents_sla_due_at"):
        op.create_index("ix_incidents_sla_due_at", "incidents", ["sla_due_at"])
    if not _index_exists("incidents", "ix_incidents_sla_open"):
        op.create_index(
            "ix_incidents_sla_open",
            "incidents",
            ["sla_due_at"],
            postgresql_where=text("workflow_status NOT IN ('resolved', 'ignored')"),
        )

    _add_columns_if_missing(
        "incidents",
        [
            sa.Column(
                "correlation_dimensions",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=text("'{}'::jsonb"),
            ),
            sa.Column(
                "correlation_confidence",
                sa.Float(),
                nullable=False,
                server_default="0",
            ),
        ],
    )

    if not _table_exists("operational_notes"):
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
    if not _index_exists("operational_notes", "ix_operational_notes_resource"):
        op.create_index(
            "ix_operational_notes_resource",
            "operational_notes",
            ["resource_type", "resource_id", "created_at"],
        )
    if not _table_exists("analysis_feedback"):
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
    if not _index_exists("analysis_feedback", "ix_analysis_feedback_resource"):
        op.create_index(
            "ix_analysis_feedback_resource",
            "analysis_feedback",
            ["resource_type", "resource_id", "created_at"],
        )
    if not _index_exists("analysis_feedback", "ix_analysis_feedback_verdict_created"):
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
