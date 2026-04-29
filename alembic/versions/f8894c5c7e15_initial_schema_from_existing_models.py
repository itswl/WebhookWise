"""initial schema from existing models

Revision ID: f8894c5c7e15
Revises:
Create Date: 2026-04-29 12:27:32.250687

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8894c5c7e15"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all tables from existing models."""
    # webhook_events
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("client_ip", sa.String(50), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("headers", postgresql.JSONB(), nullable=True),
        sa.Column("parsed_data", postgresql.JSONB(), nullable=True),
        sa.Column("alert_hash", sa.String(64), nullable=True),
        sa.Column("ai_analysis", postgresql.JSONB(), nullable=True),
        sa.Column("importance", sa.String(20), nullable=True),
        sa.Column("processing_status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("forward_status", sa.String(20), nullable=True),
        sa.Column("is_duplicate", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("beyond_window", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_events_source", "webhook_events", ["source"])
    op.create_index("ix_webhook_events_timestamp", "webhook_events", ["timestamp"])
    op.create_index("ix_webhook_events_alert_hash", "webhook_events", ["alert_hash"])
    op.create_index("ix_webhook_events_importance", "webhook_events", ["importance"])
    op.create_index("ix_webhook_events_processing_status", "webhook_events", ["processing_status"])
    op.create_index("idx_hash_timestamp", "webhook_events", ["alert_hash", "timestamp"])
    op.create_index("idx_importance_timestamp", "webhook_events", ["importance", "timestamp"])
    op.create_index("idx_duplicate_lookup", "webhook_events", ["alert_hash", "is_duplicate", "timestamp"])
    op.create_index("idx_status_created", "webhook_events", ["processing_status", "created_at"])
    op.create_index("idx_source_timestamp_id", "webhook_events", ["source", "timestamp", "id"])

    # archived_webhook_events
    op.create_table(
        "archived_webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("client_ip", sa.String(50), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("headers", postgresql.JSONB(), nullable=True),
        sa.Column("parsed_data", postgresql.JSONB(), nullable=True),
        sa.Column("alert_hash", sa.String(64), nullable=True),
        sa.Column("ai_analysis", postgresql.JSONB(), nullable=True),
        sa.Column("importance", sa.String(20), nullable=True),
        sa.Column("forward_status", sa.String(20), nullable=True),
        sa.Column("is_duplicate", sa.Integer(), nullable=True),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=True),
        sa.Column("beyond_window", sa.Integer(), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_archived_webhook_events_source", "archived_webhook_events", ["source"])
    op.create_index("ix_archived_webhook_events_timestamp", "archived_webhook_events", ["timestamp"])
    op.create_index("ix_archived_webhook_events_alert_hash", "archived_webhook_events", ["alert_hash"])
    op.create_index("ix_archived_webhook_events_importance", "archived_webhook_events", ["importance"])
    op.create_index("ix_archived_webhook_events_archived_at", "archived_webhook_events", ["archived_at"])
    op.create_index("idx_archived_hash_timestamp", "archived_webhook_events", ["alert_hash", "timestamp"])

    # ai_usage_log
    op.create_table(
        "ai_usage_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.text("now()"), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("cost_estimate", sa.Float(), nullable=True, server_default="0.0"),
        sa.Column("cache_hit", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("route_type", sa.String(20), nullable=True),
        sa.Column("alert_hash", sa.String(64), nullable=True),
        sa.Column("source", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_usage_log_timestamp", "ai_usage_log", ["timestamp"])
    op.create_index("ix_ai_usage_log_alert_hash", "ai_usage_log", ["alert_hash"])
    op.create_index("idx_usage_timestamp_route", "ai_usage_log", ["timestamp", "route_type"])

    # remediation_execution
    op.create_table(
        "remediation_execution",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("execution_id", sa.String(64), nullable=False),
        sa.Column("runbook_name", sa.String(200), nullable=False),
        sa.Column("trigger_alert_id", sa.Integer(), nullable=True),
        sa.Column("trigger_alert_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(30), nullable=True, server_default="pending"),
        sa.Column("steps_log", sa.Text(), nullable=True, server_default="[]"),
        sa.Column("dry_run", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("now()"), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id"),
    )
    op.create_index("ix_remediation_execution_execution_id", "remediation_execution", ["execution_id"])
    op.create_index("idx_remediation_status_time", "remediation_execution", ["status", "started_at"])

    # forward_rules
    op.create_table(
        "forward_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column("priority", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("match_importance", sa.String(50), nullable=True, server_default="''"),
        sa.Column("match_duplicate", sa.String(20), nullable=True, server_default="'all'"),
        sa.Column("match_source", sa.String(200), nullable=True, server_default="''"),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_url", sa.String(500), nullable=True, server_default="''"),
        sa.Column("target_name", sa.String(100), nullable=True, server_default="''"),
        sa.Column("stop_on_match", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_forward_rules_priority", "forward_rules", ["priority"])

    # deep_analyses
    op.create_table(
        "deep_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("webhook_event_id", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(20), nullable=True, server_default="'local'"),
        sa.Column("user_question", sa.Text(), nullable=True, server_default="''"),
        sa.Column("analysis_result", postgresql.JSONB(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("openclaw_run_id", sa.String(64), nullable=True),
        sa.Column("openclaw_session_key", sa.String(200), nullable=True),
        sa.Column("status", sa.String(20), nullable=True, server_default="'completed'"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deep_analyses_webhook_event_id", "deep_analyses", ["webhook_event_id"])
    op.create_index("ix_deep_analyses_openclaw_run_id", "deep_analyses", ["openclaw_run_id"])
    op.create_index("ix_deep_analyses_status", "deep_analyses", ["status"])

    # failed_forwards
    op.create_table(
        "failed_forwards",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("webhook_event_id", sa.Integer(), nullable=False),
        sa.Column("forward_rule_id", sa.Integer(), nullable=True),
        sa.Column("target_url", sa.String(500), nullable=False),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=True, server_default="'pending'"),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=True, server_default="3"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("last_retry_at", sa.DateTime(), nullable=True),
        sa.Column("forward_data", postgresql.JSONB(), nullable=True),
        sa.Column("forward_headers", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_failed_forwards_webhook_event_id", "failed_forwards", ["webhook_event_id"])
    op.create_index("idx_failed_status_retry", "failed_forwards", ["status", "next_retry_at"])
    op.create_index("idx_failed_webhook_event", "failed_forwards", ["webhook_event_id"])

    # system_configs
    op.create_table(
        "system_configs",
        sa.Column("key", sa.String(128), nullable=False, comment="配置键名（环境变量名）"),
        sa.Column("value", sa.Text(), nullable=False, comment="配置值（统一字符串存储）"),
        sa.Column(
            "value_type", sa.String(16), nullable=False, server_default="str", comment="值类型: str/int/float/bool"
        ),
        sa.Column("description", sa.Text(), nullable=True, comment="配置说明"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=True),
        sa.Column(
            "updated_by",
            sa.String(64),
            server_default="system",
            nullable=True,
            comment="修改来源: api/migration/system",
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table("system_configs")
    op.drop_table("failed_forwards")
    op.drop_table("deep_analyses")
    op.drop_table("forward_rules")
    op.drop_table("remediation_execution")
    op.drop_table("ai_usage_log")
    op.drop_table("archived_webhook_events")
    op.drop_table("webhook_events")
