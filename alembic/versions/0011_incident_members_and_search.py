"""Normalize incident membership and add bounded-search indexes.

Revision ID: 0011_incident_members_and_search
Revises: 0010_audit_log
Create Date: 2026-07-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_incident_members_and_search"
down_revision: str | Sequence[str] | None = "0010_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["webhook_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_incident_members_event_id"),
    )
    op.create_index("ix_incident_members_incident_id", "incident_members", ["incident_id"])
    op.create_index("ix_incident_members_event_timestamp", "incident_members", ["event_timestamp"])
    op.create_index(
        "ix_incident_members_incident_timestamp",
        "incident_members",
        ["incident_id", "event_timestamp"],
    )

    # Preserve every existing membership before removing the denormalized JSONB
    # array. Event timestamps provide stable chronological retrieval afterward.
    op.execute(
        text(
            """
            INSERT INTO incident_members (incident_id, event_id, event_timestamp, created_at)
            SELECT i.id, member.event_id::integer, COALESCE(w.timestamp, i.started_at), NOW()
            FROM incidents AS i
            CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(i.member_ids, '[]'::jsonb))
                AS member(event_id)
            JOIN webhook_events AS w ON w.id = member.event_id::integer
            ON CONFLICT (event_id) DO NOTHING
            """
        )
    )

    op.add_column("incidents", sa.Column("summary_status", sa.String(length=20), nullable=True))
    op.add_column(
        "incidents",
        sa.Column("summary_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("incidents", sa.Column("summary_next_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("incidents", sa.Column("summary_last_error", sa.Text(), nullable=True))
    op.execute(
        text(
            """
            UPDATE incidents
            SET summary_status = CASE
                WHEN summary_analysis IS NOT NULL THEN 'completed'
                WHEN status IN ('quiet', 'closed') THEN 'pending'
                ELSE NULL
            END,
            summary_next_attempt_at = CASE
                WHEN summary_analysis IS NULL AND status IN ('quiet', 'closed') THEN NOW()
                ELSE NULL
            END
            """
        )
    )
    op.create_index(
        "ix_incidents_summary_pending",
        "incidents",
        ["summary_next_attempt_at"],
        postgresql_where=text("summary_status IN ('pending', 'retrying', 'processing')"),
    )
    op.drop_column("incidents", "member_ids")

    # Existing ILIKE predicates use leading wildcards. Trigram indexes keep
    # those queries indexable without changing the API's substring semantics.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_webhook_events_search_rule_name ON webhook_events "
        "USING gin (lower(parsed_data->>'RuleName') gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_webhook_events_search_alert_name ON webhook_events "
        "USING gin (lower(parsed_data->>'AlertName') gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_webhook_events_search_alert_name_lower ON webhook_events "
        "USING gin (lower(parsed_data->>'alert_name') gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_webhook_events_search_ai_summary ON webhook_events "
        "USING gin (lower(ai_analysis->>'summary') gin_trgm_ops)"
    )
    op.execute("CREATE INDEX ix_webhook_events_search_source ON webhook_events USING gin (lower(source) gin_trgm_ops)")
    op.execute(
        "CREATE INDEX ix_webhook_events_search_request_id ON webhook_events USING gin (lower(request_id) gin_trgm_ops)"
    )


def downgrade() -> None:
    for index_name in (
        "ix_webhook_events_search_request_id",
        "ix_webhook_events_search_source",
        "ix_webhook_events_search_ai_summary",
        "ix_webhook_events_search_alert_name_lower",
        "ix_webhook_events_search_alert_name",
        "ix_webhook_events_search_rule_name",
    ):
        op.drop_index(index_name, table_name="webhook_events")

    op.add_column(
        "incidents",
        sa.Column("member_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        text(
            """
            UPDATE incidents AS i
            SET member_ids = members.ids
            FROM (
                SELECT incident_id, jsonb_agg(event_id ORDER BY event_timestamp, id) AS ids
                FROM incident_members
                GROUP BY incident_id
            ) AS members
            WHERE members.incident_id = i.id
            """
        )
    )
    op.drop_index("ix_incidents_summary_pending", table_name="incidents")
    op.drop_column("incidents", "summary_last_error")
    op.drop_column("incidents", "summary_next_attempt_at")
    op.drop_column("incidents", "summary_attempts")
    op.drop_column("incidents", "summary_status")
    op.drop_index("ix_incident_members_incident_timestamp", table_name="incident_members")
    op.drop_index("ix_incident_members_event_timestamp", table_name="incident_members")
    op.drop_index("ix_incident_members_incident_id", table_name="incident_members")
    op.drop_table("incident_members")
