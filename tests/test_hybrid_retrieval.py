from langchain_core.documents import Document
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.retrieval import HybridRetriever, _blend_candidates
from app.db import Base
from app.models import DocumentAccessGroup, KnowledgeChunk, KnowledgeDocument


class EmptyVectorStore:
    def search(self, *args, **kwargs):
        return []


class ScoredVectorStore:
    def search(self, *args, **kwargs):
        return [
            Document(
                page_content="年假为五天",
                metadata={
                    "chunk_id": "chunk-public",
                    "document_id": "public",
                    "title": "公开制度",
                    "tenant_id": "tenant-a",
                    "vector_score": 0.125,
                },
            )
        ]


def _add_document(db, document_id, title, group, content):
    document = KnowledgeDocument(
        id=document_id,
        tenant_id="tenant-a",
        title=title,
        filename=f"{document_id}.md",
        stored_path=f"{document_id}.md",
        content_hash=document_id.ljust(64, "0"),
        status="ready",
        chunk_count=1,
        access_group=group,
    )
    if group:
        document.acl_entries.append(DocumentAccessGroup(group_name=group))
    document.chunks.append(
        KnowledgeChunk(
            id=f"chunk-{document_id}",
            tenant_id="tenant-a",
            access_group=group,
            position=0,
            content=content,
            vector_id=f"vector-{document_id}",
        )
    )
    db.add(document)


def test_lexical_retrieval_applies_acl_before_scoring():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(vector_backend="memory", retrieval_k=10, retrieval_fetch_k=10)
    retriever = HybridRetriever(settings, EmptyVectorStore())

    with Session(engine) as db:
        _add_document(db, "public", "公开制度", None, "年假为五天")
        _add_document(db, "hr", "人事制度", "hr", "人力资源年假审批")
        _add_document(db, "finance", "财务制度", "finance", "财务人员年假审批")
        db.commit()

        result = retriever.retrieve(db, "年假审批", "tenant-a", ["hr"])
        ids = {doc.metadata["document_id"] for doc in result.documents}

    assert "hr" in ids
    assert "finance" not in ids


def test_database_lexical_search_is_not_truncated_by_legacy_candidate_limit():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        vector_backend="memory",
        retrieval_k=10,
        retrieval_fetch_k=10,
        lexical_candidate_limit=1,
    )
    retriever = HybridRetriever(settings, EmptyVectorStore())

    with Session(engine) as db:
        for index in range(5):
            _add_document(db, f"ordinary-{index}", f"普通资料 {index}", None, "普通通用内容")
        _add_document(db, "last", "目标资料", None, "独有关键词量子审批流程")
        db.commit()

        result = retriever.retrieve(db, "量子审批流程", "tenant-a", [])

    assert result.documents[0].metadata["document_id"] == "last"


def test_vector_score_is_preserved_in_retrieval_trace_scores():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(vector_backend="memory", retrieval_k=3, retrieval_fetch_k=3)
    retriever = HybridRetriever(settings, ScoredVectorStore())

    with Session(engine) as db:
        _add_document(db, "public", "公开制度", None, "年假为五天")
        db.commit()
        result = retriever.retrieve(db, "年假", "tenant-a", [])

    assert result.scores["chunk-public"]["vector_score"] == 0.125


def test_blend_candidates_includes_every_channel_and_deduplicates():
    def doc(chunk_id: str) -> Document:
        return Document(page_content=chunk_id, metadata={"chunk_id": chunk_id})

    fused = [doc(f"fused-{index}") for index in range(12)]
    vector = [doc(f"vector-{index}") for index in range(6)]
    lexical = [doc(f"lexical-{index}") for index in range(6)]

    blended = _blend_candidates(fused, vector, lexical, budget=12)
    ids = {item.metadata["chunk_id"] for item in blended}

    assert "lexical-0" in ids  # channel-specific doc must survive the fused cutoff
    assert "vector-0" in ids
    assert len(ids) == len(blended)  # deduplicated
    assert len(blended) <= 12 * 4 // 3  # latency bound


def test_blend_candidates_does_not_duplicate_overlapping_documents():
    def doc(chunk_id: str) -> Document:
        return Document(page_content=chunk_id, metadata={"chunk_id": chunk_id})

    shared = [doc(f"shared-{index}") for index in range(10)]
    blended = _blend_candidates(shared, shared, shared, budget=12)

    ids = {item.metadata["chunk_id"] for item in blended}
    assert len(ids) == len(blended)  # overlapping channels yield no duplicates
