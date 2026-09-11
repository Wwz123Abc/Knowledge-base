"""Add durable tool execution results.

Revision ID: 0003_tool_executions
Revises: 0002_connectors
"""

from alembic import op

from app import models

revision = "0003_tool_executions"
down_revision = "0002_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    models.ToolExecution.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    models.ToolExecution.__table__.drop(bind=op.get_bind(), checkfirst=True)
