"""managed inbound sources and scoped webhook credentials

Revision ID: 0022_source_onboarding
Revises: 0021_incident_resolution
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_source_onboarding"
down_revision: str | Sequence[str] | None = "0021_incident_resolution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=16), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("first_event_at", sa.DateTime(), nullable=True),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("last_request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "event_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_auth_failure_at", sa.DateTime(), nullable=True),
        sa.Column(
            "auth_failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("schema_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "schema_change_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("schema_changed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "public_id",
            name="uq_source_connections_public_id",
        ),
    )
    op.create_index(
        op.f("ix_source_connections_source_type"),
        "source_connections",
        ["source_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_connections_last_event_at"),
        "source_connections",
        ["last_event_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_connections_enabled_last_event",
        "source_connections",
        ["enabled", "last_event_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_connections_active_type",
        "source_connections",
        ["source_type"],
        unique=False,
        postgresql_where=sa.text("enabled = true"),
    )
    op.add_column(
        "webhook_events",
        sa.Column("source_connection_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_webhook_events_source_connection",
        "webhook_events",
        "source_connections",
        ["source_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_webhook_events_source_connection_id"),
        "webhook_events",
        ["source_connection_id"],
        unique=False,
    )
    op.add_column(
        "archived_webhook_events",
        sa.Column("source_connection_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_archived_webhook_events_source_connection_id"),
        "archived_webhook_events",
        ["source_connection_id"],
        unique=False,
    )
    op.add_column(
        "incidents",
        sa.Column("source_connection_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_incidents_source_connection",
        "incidents",
        "source_connections",
        ["source_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_incidents_source_connection_id"),
        "incidents",
        ["source_connection_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_incidents_source_connection_id"),
        table_name="incidents",
    )
    op.drop_constraint(
        "fk_incidents_source_connection",
        "incidents",
        type_="foreignkey",
    )
    op.drop_column("incidents", "source_connection_id")
    op.drop_index(
        op.f("ix_archived_webhook_events_source_connection_id"),
        table_name="archived_webhook_events",
    )
    op.drop_column("archived_webhook_events", "source_connection_id")
    op.drop_index(
        op.f("ix_webhook_events_source_connection_id"),
        table_name="webhook_events",
    )
    op.drop_constraint(
        "fk_webhook_events_source_connection",
        "webhook_events",
        type_="foreignkey",
    )
    op.drop_column("webhook_events", "source_connection_id")
    op.drop_index(
        "ix_source_connections_active_type",
        table_name="source_connections",
    )
    op.drop_index(
        "ix_source_connections_enabled_last_event",
        table_name="source_connections",
    )
    op.drop_index(
        op.f("ix_source_connections_last_event_at"),
        table_name="source_connections",
    )
    op.drop_index(
        op.f("ix_source_connections_source_type"),
        table_name="source_connections",
    )
    op.drop_table("source_connections")
