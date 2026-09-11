"""Track per-item failures during a connector sync run instead of aborting the whole
batch on the first error (P19).

Revision ID: 0009_connector_sync_failed_count
Revises: 0008_user_role_assignments
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_connector_sync_failed_count"
down_revision = "0008_user_role_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # See 0006_embedding_model_version: 0001_initial builds every table from the
    # CURRENT model definitions, so a fresh database already has this column and this
    # step is a no-op there — only a database migrated before this field existed needs
    # the ADD COLUMN.
    existing_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("connector_sync_runs")
    }
    if "failed_count" not in existing_columns:
        op.add_column(
            "connector_sync_runs",
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("connector_sync_runs", "failed_count")
