"""Inbound rules: the same matcher as forwarding, with an action instead of a target

Forwarding has been rule-driven since the beginning. The inbound side grew the
other way — each decision got its own storage, and the newest of them, "which
alerts are never worth a model call", shipped as a comma-separated environment
variable that could only be read over SSH and only matched an exact rule name.

The match columns mirror forward_rules exactly on purpose: `_rule_matches` is
already shared (silences probe it by building a forward-rule snapshot), so
adding a rule type costs a table and no matching logic. match_rule_name is the
one addition, because the alert rule is what an operator actually thinks in.

No backfill. An empty table changes nothing: AI_EXCLUDED_RULES keeps working as
the simple form, and a deployment that never adds a row behaves exactly as it
does today.

Revision ID: 0031_inbound_rules
Revises: 0030_named_gateways
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_inbound_rules"
down_revision = "0030_named_gateways"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("match_event_type", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_importance", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("match_duplicate", sa.String(length=20), nullable=False, server_default="all"),
        sa.Column("match_source", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_project", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_region", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_environment", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("match_payload", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("match_rule_name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_inbound_rules_priority", "inbound_rules", ["priority"])


def downgrade() -> None:
    op.drop_index("idx_inbound_rules_priority", table_name="inbound_rules")
    op.drop_table("inbound_rules")
