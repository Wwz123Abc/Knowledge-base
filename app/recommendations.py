from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthContext
from app.models import KnowledgeChunk, KnowledgeDocument, RetrievalTrace
from app.schemas import RecommendationOut


class RecommendationService:
    def recommend(self, db: Session, auth: AuthContext, limit: int = 6) -> list[RecommendationOut]:
        traces = list(
            db.scalars(
                select(RetrievalTrace)
                .where(
                    RetrievalTrace.tenant_id == auth.tenant_id,
                    RetrievalTrace.user_id == auth.user_id,
                )
                .order_by(RetrievalTrace.created_at.desc())
                .limit(200)
            )
        )
        chunk_counts = Counter(
            chunk_id for trace in traces for chunk_id in trace.retrieved_chunk_ids
        )
        candidate_documents = list(
            db.scalars(
                select(KnowledgeDocument)
                .options(selectinload(KnowledgeDocument.acl_entries))
                .where(
                    KnowledgeDocument.tenant_id == auth.tenant_id,
                    KnowledgeDocument.status == "ready",
                )
                .order_by(KnowledgeDocument.updated_at.desc())
                .limit(max(limit * 20, 100))
            )
        )
        allowed_groups = set(auth.groups)
        scores: Counter[str] = Counter()
        documents: dict[str, KnowledgeDocument] = {}
        for document in candidate_documents:
            if document.access_groups and not set(document.access_groups) & allowed_groups:
                continue
            documents[document.id] = document
            scores[document.id] += 1
            if document.department and document.department in allowed_groups:
                scores[document.id] += 2
        if chunk_counts:
            referenced = db.execute(
                select(KnowledgeChunk.document_id, KnowledgeChunk.id).where(
                    KnowledgeChunk.tenant_id == auth.tenant_id,
                    KnowledgeChunk.id.in_(chunk_counts),
                )
            )
            for document_id, chunk_id in referenced:
                if document_id in documents:
                    scores[document_id] += chunk_counts[str(chunk_id)]
        return [
            RecommendationOut(
                document_id=document_id,
                title=documents[document_id].title,
                department=documents[document_id].department,
                reason="常用知识" if score > 2 else "部门或最新资料",
                score=float(score),
            )
            for document_id, score in scores.most_common(limit)
        ]
