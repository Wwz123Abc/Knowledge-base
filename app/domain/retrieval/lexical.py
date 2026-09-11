from __future__ import annotations

from langchain_core.documents import Document
from sqlalchemy import case, exists, literal, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.domain.retrieval.tokens import escape_like, tokenize
from app.models import (
    DocumentAccessGroup,
    DocumentKnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
)


class LexicalSearchMixin:
    def _lexical_search(
        self,
        db: Session,
        query: str,
        tenant_id: str,
        user_groups: list[str],
        knowledge_base_ids: list[str],
    ) -> list[Document]:
        query_tokens = list(dict.fromkeys(tokenize(query)))[:24]
        if not query_tokens:
            return []
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            try:
                sqlite_results = self._sqlite_fts_search(
                    db, query_tokens, tenant_id, user_groups, knowledge_base_ids
                )
                if sqlite_results:
                    return sqlite_results
            except SQLAlchemyError:
                pass
        match_conditions = [
            KnowledgeChunk.content.ilike(f"%{escape_like(token)}%", escape="\\")
            for token in query_tokens
        ]
        lexical_score = sum(
            (case((condition, 1.0), else_=0.0) for condition in match_conditions),
            literal(0.0),
        )
        acl_exists = exists(
            select(DocumentAccessGroup.id).where(
                DocumentAccessGroup.document_id == KnowledgeDocument.id
            )
        )
        if user_groups:
            acl_allowed = or_(
                ~acl_exists,
                exists(
                    select(DocumentAccessGroup.id).where(
                        DocumentAccessGroup.document_id == KnowledgeDocument.id,
                        DocumentAccessGroup.group_name.in_(user_groups),
                    )
                ),
            )
        else:
            acl_allowed = ~acl_exists
        filters = [
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeDocument.status == "ready",
            or_(*match_conditions),
            acl_allowed,
        ]
        if knowledge_base_ids:
            base_exists = exists(
                select(DocumentKnowledgeBase.id).where(
                    DocumentKnowledgeBase.document_id == KnowledgeDocument.id
                )
            )
            filters.append(
                or_(
                    ~base_exists,
                    exists(
                        select(DocumentKnowledgeBase.id).where(
                            DocumentKnowledgeBase.document_id == KnowledgeDocument.id,
                            DocumentKnowledgeBase.knowledge_base_id.in_(knowledge_base_ids),
                        )
                    ),
                )
            )
        rows = list(
            db.execute(
                select(KnowledgeChunk, lexical_score.label("lexical_score"))
                .join(KnowledgeChunk.document)
                .options(
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.acl_entries
                    ),
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.knowledge_base_entries
                    ),
                    selectinload(KnowledgeChunk.document).selectinload(KnowledgeDocument.versions),
                )
                .where(*filters)
                .order_by(lexical_score.desc(), KnowledgeChunk.position)
                .limit(max(self.settings.retrieval_fetch_k * 5, 50))
            )
        )
        scored = [
            (chunk, float(score)) for chunk, score in rows if self._is_effective(chunk.document)
        ][: self.settings.retrieval_fetch_k]
        return [self._to_document(chunk, score) for chunk, score in scored]

    def _sqlite_fts_search(
        self,
        db: Session,
        query_tokens: list[str],
        tenant_id: str,
        user_groups: list[str],
        knowledge_base_ids: list[str],
    ) -> list[Document]:
        match_query = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in query_tokens
        )
        candidates = list(
            db.execute(
                text(
                    """
                    SELECT chunk.id, bm25(knowledge_chunks_fts) AS rank
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks AS chunk
                      ON chunk.rowid = knowledge_chunks_fts.rowid
                    JOIN knowledge_documents AS document
                      ON document.id = chunk.document_id
                    WHERE knowledge_chunks_fts MATCH :match_query
                      AND chunk.tenant_id = :tenant_id
                      AND document.status = 'ready'
                    ORDER BY rank
                    LIMIT :candidate_limit
                    """
                ),
                {
                    "match_query": match_query,
                    "tenant_id": tenant_id,
                    "candidate_limit": max(self.settings.retrieval_fetch_k * 20, 200),
                },
            )
        )
        if not candidates:
            return []
        ids = [str(row[0]) for row in candidates]
        ranks = {str(row[0]): float(row[1]) for row in candidates}
        chunks = list(
            db.scalars(
                select(KnowledgeChunk)
                .options(
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.acl_entries
                    ),
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.knowledge_base_entries
                    ),
                    selectinload(KnowledgeChunk.document).selectinload(KnowledgeDocument.versions),
                )
                .where(KnowledgeChunk.id.in_(ids))
            )
        )
        by_id = {chunk.id: chunk for chunk in chunks}
        allowed_groups = set(user_groups)
        selected_bases = set(knowledge_base_ids)
        authorized = [
            by_id[chunk_id]
            for chunk_id in ids
            if chunk_id in by_id
            and (
                not by_id[chunk_id].document.access_groups
                or bool(set(by_id[chunk_id].document.access_groups) & allowed_groups)
            )
            and (
                not selected_bases
                or not by_id[chunk_id].document.knowledge_base_ids
                or bool(set(by_id[chunk_id].document.knowledge_base_ids) & selected_bases)
            )
            and self._is_effective(by_id[chunk_id].document)
        ][: self.settings.retrieval_fetch_k]
        return [
            self._to_document(chunk, 1.0 / (1.0 + abs(ranks[chunk.id]))) for chunk in authorized
        ]
