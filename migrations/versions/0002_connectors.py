"""Add connector synchronization tables.

Revision ID: 0002_connectors
Revises: 0001_initial
"""

from alembic import op

from app import models

revision = "0002_connectors"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


TABLES = (
    models.KnowledgeConnector.__table__,
    models.ConnectorItem.__table__,
    models.ConnectorSyncRun.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind=bind, checkfirst=True)
