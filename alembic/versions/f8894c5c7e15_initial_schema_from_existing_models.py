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


def _add_columns_if_not_exists(table: str, columns: Sequence[str]) -> None:
    for column in columns:
        op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}"))


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
        sa.Column("is_duplicate", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("beyond_window", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "webhook_events",
        (
            "id INTEGER",
            "source VARCHAR(100)",
            "client_ip VARCHAR(50)",
            "timestamp TIMESTAMP WITHOUT TIME ZONE",
            "raw_payload TEXT",
            "headers JSONB",
            "parsed_data JSONB",
            "alert_hash VARCHAR(64)",
            "ai_analysis JSONB",
            "importance VARCHAR(20)",
            "processing_status VARCHAR(20) DEFAULT 'received' NOT NULL",
            "forward_status VARCHAR(20)",
            "is_duplicate BOOLEAN DEFAULT false",
            "duplicate_of INTEGER",
            "duplicate_count INTEGER DEFAULT 1",
            "beyond_window BOOLEAN DEFAULT false",
            "last_notified_at TIMESTAMP WITHOUT TIME ZONE",
            "created_at TIMESTAMP WITHOUT TIME ZONE",
            "updated_at TIMESTAMP WITHOUT TIME ZONE",
        ),
    )
    op.create_index("ix_webhook_events_source", "webhook_events", ["source"], if_not_exists=True)
    op.create_index("ix_webhook_events_timestamp", "webhook_events", ["timestamp"], if_not_exists=True)
    op.create_index("ix_webhook_events_alert_hash", "webhook_events", ["alert_hash"], if_not_exists=True)
    op.create_index("ix_webhook_events_importance", "webhook_events", ["importance"], if_not_exists=True)
    op.create_index("ix_webhook_events_processing_status", "webhook_events", ["processing_status"], if_not_exists=True)
    op.create_index("idx_hash_timestamp", "webhook_events", ["alert_hash", "timestamp"], if_not_exists=True)
    op.create_index("idx_importance_timestamp", "webhook_events", ["importance", "timestamp"], if_not_exists=True)
    op.create_index(
        "idx_duplicate_lookup", "webhook_events", ["alert_hash", "is_duplicate", "timestamp"], if_not_exists=True
    )
    op.create_index("idx_status_created", "webhook_events", ["processing_status", "created_at"], if_not_exists=True)
    op.create_index("idx_source_timestamp_id", "webhook_events", ["source", "timestamp", "id"], if_not_exists=True)

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
        sa.Column("is_duplicate", sa.Boolean(), nullable=True),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("duplicate_count", sa.Integer(), nullable=True),
        sa.Column("beyond_window", sa.Boolean(), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "archived_webhook_events",
        (
            "id INTEGER",
            "source VARCHAR(100)",
            "client_ip VARCHAR(50)",
            "timestamp TIMESTAMP WITHOUT TIME ZONE",
            "raw_payload TEXT",
            "headers JSONB",
            "parsed_data JSONB",
            "alert_hash VARCHAR(64)",
            "ai_analysis JSONB",
            "importance VARCHAR(20)",
            "forward_status VARCHAR(20)",
            "is_duplicate BOOLEAN",
            "duplicate_of INTEGER",
            "duplicate_count INTEGER",
            "beyond_window BOOLEAN",
            "last_notified_at TIMESTAMP WITHOUT TIME ZONE",
            "created_at TIMESTAMP WITHOUT TIME ZONE",
            "updated_at TIMESTAMP WITHOUT TIME ZONE",
            "archived_at TIMESTAMP WITHOUT TIME ZONE",
        ),
    )
    op.create_index("ix_archived_webhook_events_source", "archived_webhook_events", ["source"], if_not_exists=True)
    op.create_index(
        "ix_archived_webhook_events_timestamp", "archived_webhook_events", ["timestamp"], if_not_exists=True
    )
    op.create_index(
        "ix_archived_webhook_events_alert_hash", "archived_webhook_events", ["alert_hash"], if_not_exists=True
    )
    op.create_index(
        "ix_archived_webhook_events_importance", "archived_webhook_events", ["importance"], if_not_exists=True
    )
    op.create_index(
        "ix_archived_webhook_events_archived_at", "archived_webhook_events", ["archived_at"], if_not_exists=True
    )
    op.create_index(
        "idx_archived_hash_timestamp", "archived_webhook_events", ["alert_hash", "timestamp"], if_not_exists=True
    )

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
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "ai_usage_log",
        (
            "id INTEGER",
            "timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
            "model VARCHAR(100)",
            "tokens_in INTEGER DEFAULT 0",
            "tokens_out INTEGER DEFAULT 0",
            "cost_estimate DOUBLE PRECISION DEFAULT 0.0",
            "cache_hit BOOLEAN DEFAULT false",
            "route_type VARCHAR(20)",
            "alert_hash VARCHAR(64)",
            "source VARCHAR(100)",
        ),
    )
    op.create_index("ix_ai_usage_log_timestamp", "ai_usage_log", ["timestamp"], if_not_exists=True)
    op.create_index("ix_ai_usage_log_alert_hash", "ai_usage_log", ["alert_hash"], if_not_exists=True)
    op.create_index("idx_usage_timestamp_route", "ai_usage_log", ["timestamp", "route_type"], if_not_exists=True)

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
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "remediation_execution",
        (
            "id INTEGER",
            "execution_id VARCHAR(64)",
            "runbook_name VARCHAR(200)",
            "trigger_alert_id INTEGER",
            "trigger_alert_hash VARCHAR(64)",
            "status VARCHAR(30) DEFAULT 'pending'",
            "steps_log TEXT DEFAULT '[]'",
            "dry_run BOOLEAN DEFAULT false",
            "started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
            "completed_at TIMESTAMP WITHOUT TIME ZONE",
            "error_message TEXT",
        ),
    )
    op.create_index(
        "ix_remediation_execution_execution_id", "remediation_execution", ["execution_id"], if_not_exists=True
    )
    op.create_index(
        "idx_remediation_status_time", "remediation_execution", ["status", "started_at"], if_not_exists=True
    )

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
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "forward_rules",
        (
            "id INTEGER",
            "name VARCHAR(100)",
            "enabled BOOLEAN DEFAULT true",
            "priority INTEGER DEFAULT 0",
            "match_importance VARCHAR(50) DEFAULT ''",
            "match_duplicate VARCHAR(20) DEFAULT 'all'",
            "match_source VARCHAR(200) DEFAULT ''",
            "target_type VARCHAR(20)",
            "target_url VARCHAR(500) DEFAULT ''",
            "target_name VARCHAR(100) DEFAULT ''",
            "stop_on_match BOOLEAN DEFAULT false",
            "created_at TIMESTAMP WITHOUT TIME ZONE",
            "updated_at TIMESTAMP WITHOUT TIME ZONE",
        ),
    )
    op.create_index("idx_forward_rules_priority", "forward_rules", ["priority"], if_not_exists=True)

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
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "deep_analyses",
        (
            "id INTEGER",
            "webhook_event_id INTEGER",
            "engine VARCHAR(20) DEFAULT 'local'",
            "user_question TEXT DEFAULT ''",
            "analysis_result JSONB",
            "duration_seconds DOUBLE PRECISION DEFAULT 0",
            "created_at TIMESTAMP WITHOUT TIME ZONE",
            "openclaw_run_id VARCHAR(64)",
            "openclaw_session_key VARCHAR(200)",
            "status VARCHAR(20) DEFAULT 'completed'",
        ),
    )
    op.create_index("ix_deep_analyses_webhook_event_id", "deep_analyses", ["webhook_event_id"], if_not_exists=True)
    op.create_index("ix_deep_analyses_openclaw_run_id", "deep_analyses", ["openclaw_run_id"], if_not_exists=True)
    op.create_index("ix_deep_analyses_status", "deep_analyses", ["status"], if_not_exists=True)

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
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "failed_forwards",
        (
            "id INTEGER",
            "webhook_event_id INTEGER",
            "forward_rule_id INTEGER",
            "target_url VARCHAR(500)",
            "target_type VARCHAR(20)",
            "status VARCHAR(20) DEFAULT 'pending'",
            "failure_reason VARCHAR(500)",
            "error_message TEXT",
            "retry_count INTEGER DEFAULT 0",
            "max_retries INTEGER DEFAULT 3",
            "next_retry_at TIMESTAMP WITHOUT TIME ZONE",
            "last_retry_at TIMESTAMP WITHOUT TIME ZONE",
            "forward_data JSONB",
            "forward_headers JSONB",
            "created_at TIMESTAMP WITHOUT TIME ZONE",
            "updated_at TIMESTAMP WITHOUT TIME ZONE",
        ),
    )
    op.create_index("ix_failed_forwards_webhook_event_id", "failed_forwards", ["webhook_event_id"], if_not_exists=True)
    op.create_index("idx_failed_status_retry", "failed_forwards", ["status", "next_retry_at"], if_not_exists=True)
    op.create_index("idx_failed_webhook_event", "failed_forwards", ["webhook_event_id"], if_not_exists=True)

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
        if_not_exists=True,
    )
    _add_columns_if_not_exists(
        "system_configs",
        (
            "key VARCHAR(128)",
            "value TEXT",
            "value_type VARCHAR(16) DEFAULT 'str'",
            "description TEXT",
            "updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now()",
            "updated_by VARCHAR(64) DEFAULT 'system'",
        ),
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
