"""Close the model/migration schema drift `alembic check` now guards.

Replaying `alembic upgrade head --sql` against Base.metadata found three
disagreements, none of which any test could see (the default suite runs on
SQLite and never reads a real schema):

  * four columns carried ``index=True`` in the model with no index in any
    migration. Two of them are redundant next to an existing composite and are
    dropped from the model instead (see models/audit_log.py and
    models/incident.py); the other two lead queries that no composite serves,
    so they are created here.
  * eight indexes existed only in migrations. Those are now declared in the
    models — no DDL, the database already has them.
  * ``maintenance_windows.created_at/updated_at`` were NOT NULL in the model and
    nullable in migration 0017. The model is right: both are always written by
    the ORM default. Backfill, then tighten.

Revision ID: 0040_declared_indexes
Revises: 0039_forward_outbox_digest
Create Date: 2026-09-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# The revision id is the file stem truncated: alembic_version.version_num is
# VARCHAR(32) (0030/0032/0038 shortened for the same reason).
revision: str = "0040_declared_indexes"
down_revision: str | Sequence[str] | None = "0039_forward_outbox_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The activity feed's default query filters on nothing and orders by
    # created_at DESC, so ix_audit_log_type_created — which leads with
    # resource_type — cannot serve it.
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    # Change-impact correlation and the service profiles window on started_at
    # with no status predicate, which ix_incidents_status_started cannot serve.
    op.create_index("ix_incidents_started_at", "incidents", ["started_at"])

    # Rows written before this revision may hold NULLs the ORM never produces.
    op.execute("UPDATE maintenance_windows SET created_at = now() WHERE created_at IS NULL")
    op.execute("UPDATE maintenance_windows SET updated_at = now() WHERE updated_at IS NULL")
    op.alter_column("maintenance_windows", "created_at", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("maintenance_windows", "updated_at", existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    op.alter_column("maintenance_windows", "updated_at", existing_type=sa.DateTime(), nullable=True)
    op.alter_column("maintenance_windows", "created_at", existing_type=sa.DateTime(), nullable=True)
    op.drop_index("ix_incidents_started_at", table_name="incidents")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
