"""Add database-native lexical search indexes.

Revision ID: 0005_lexical_search
Revises: 0004_knowledge_bases
"""

from alembic import op

revision = "0005_lexical_search"
down_revision = "0004_knowledge_bases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_content_trgm
            ON knowledge_chunks USING gin (content gin_trgm_ops)
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
            USING fts5(
                content,
                content='knowledge_chunks',
                content_rowid='rowid',
                tokenize='unicode61'
            )
            """
        )
        op.execute("INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts) VALUES('rebuild')")
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_insert
            AFTER INSERT ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_delete
            AFTER DELETE ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content)
                VALUES ('delete', old.rowid, old.content);
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS knowledge_chunks_fts_update
            AFTER UPDATE OF content ON knowledge_chunks BEGIN
                INSERT INTO knowledge_chunks_fts(knowledge_chunks_fts, rowid, content)
                VALUES ('delete', old.rowid, old.content);
                INSERT INTO knowledge_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
            END
            """
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_content_trgm")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS knowledge_chunks_fts_update")
        op.execute("DROP TRIGGER IF EXISTS knowledge_chunks_fts_delete")
        op.execute("DROP TRIGGER IF EXISTS knowledge_chunks_fts_insert")
        op.execute("DROP TABLE IF EXISTS knowledge_chunks_fts")
