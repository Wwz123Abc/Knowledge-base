"""Track the embedding model used for each indexed document.

Revision ID: 0006_embedding_model_version
Revises: 0005_lexical_search
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_embedding_model_version"
down_revision = "0005_lexical_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # 0001_initial builds every table from the CURRENT model definitions (Base.metadata.
    # create_all), and embedding_model_version is now a permanent field on KnowledgeDocument
    # — so on a database that's migrating fresh from empty, 0001 already created this column
    # and this step must be a no-op. Only databases that were incrementally migrated before
    # this field existed on the model actually need the ADD COLUMN.
    existing_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("knowledge_documents")
    }
    if "embedding_model_version" not in existing_columns:
        op.add_column(
            "knowledge_documents",
            sa.Column("embedding_model_version", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("knowledge_documents", "embedding_model_version")
