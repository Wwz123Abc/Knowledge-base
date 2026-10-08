from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth import AuthContext
from app.db import Base
from app.models import AuditLog, KnowledgeChunk, KnowledgeDocument, RetrievalTrace
from app.recommendations import RecommendationService
from scripts.prune_history import prune_older_than


def test_prune_removes_only_rows_older_than_the_cutoff():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add_all(
            [
                RetrievalTrace(
                    tenant_id="t",
                    user_id="u",
                    original_query=f"q{index}",
                    created_at=now - timedelta(days=days),
                )
                for index, days in enumerate((400, 200, 10, 1))
            ]
        )
        db.commit()
        cutoff = now - timedelta(days=180)

        assert prune_older_than(db, RetrievalTrace, cutoff, dry_run=True) == 2
        assert db.scalar(select(func.count()).select_from(RetrievalTrace)) == 4  # nothing deleted

        assert prune_older_than(db, RetrievalTrace, cutoff) == 2
        assert db.scalar(select(func.count()).select_from(RetrievalTrace)) == 2
        assert prune_older_than(db, AuditLog, cutoff) == 0


def test_recommendations_only_include_documents_the_user_may_read():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for title, groups in (("公开手册", []), ("人事细则", ["hr"]), ("财务细则", ["finance"])):
            document = KnowledgeDocument(
                tenant_id="t",
                title=title,
                filename=f"{title}.md",
                stored_path="x",
                content_hash=title,
                status="ready",
            )
            db.add(document)
            db.flush()
            db.add(
                KnowledgeChunk(
                    document_id=document.id,
                    tenant_id="t",
                    position=0,
                    content=title,
                    vector_id=title,
                )
            )
            from app.models import DocumentAccessGroup

            db.add_all(
                DocumentAccessGroup(document_id=document.id, group_name=group) for group in groups
            )
        db.commit()
        auth = AuthContext("u", "测试", "t", ("hr",), ("user",))
        titles = {item.title for item in RecommendationService().recommend(db, auth, 10)}
        assert titles == {"公开手册", "人事细则"}
