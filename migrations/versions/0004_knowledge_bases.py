"""Add logical knowledge bases and vector entry mapping.

Revision ID: 0004_knowledge_bases
Revises: 0003_tool_executions
"""

from alembic import op

from app import models

revision = "0004_knowledge_bases"
down_revision = "0003_tool_executions"
branch_labels = None
depends_on = None


TABLES = (
    models.KnowledgeBase.__table__,
    models.DocumentKnowledgeBase.__table__,
    models.ChunkVectorEntry.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind=bind, checkfirst=True)
