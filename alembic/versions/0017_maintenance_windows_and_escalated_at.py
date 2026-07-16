"""maintenance windows table + incidents.escalated_at

Maintenance windows are recurring silence schedules: a scheduler sweep
materializes each active occurrence into a normal expiring Silence row, so all
existing suppression accounting keeps working on plain silences.

incidents.escalated_at marks when the SLA-breach escalation notification was
queued for an incident, making the breach visible without joining the outbox.

Revision ID: 0017_maintenance_windows_and_escalated_at
Revises: 0016_kb_document_status
Create Date: 2026-07-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_maintenance_windows_and_escalated_at"
down_revision: str | Sequence[str] | None = "0016_kb_document_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("match_source", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_importance", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("match_event_type", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_project", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_region", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_environment", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_payload", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("days_of_week", sa.String(length=20), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("comment", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.add_column("incidents", sa.Column("escalated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "escalated_at")
    op.drop_table("maintenance_windows")
