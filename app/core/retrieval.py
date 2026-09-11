from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from langchain_core.documents import Document
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.core.rerank import build_reranker
from app.domain.retrieval.lexical import LexicalSearchMixin
from app.domain.retrieval.statute import is_effective_document
from app.domain.retrieval.tokens import tokenize
from app.models import KnowledgeChunk, KnowledgeDocument


@dataclass(slots=True)
class RetrievalResult:
    documents: list[Document]
    scores: dict[str, dict[str, float]]


class HybridRetriever(LexicalSearchMixin):
    def __init__(self, settings: Settings, vector_store):
        self.settings = settings
        self.vector_store = vector_store

    def retrieve(
        self,
        db: Session,
        query: str,
        tenant_id: str,
        user_groups: list[str],
        knowledge_base_ids: list[str] | None = None,
    ) -> RetrievalResult:
        # Vector search opens its own SQLAlchemy engine/connection (see
        # EnterpriseVectorStore) and never touches `db`, so it's safe to run on a
        # background thread while lexical search uses `db` on this one — a
        # Session isn't thread-safe, so the shared `db` must stay single-threaded.
        # This turns two sequential DB round-trips into one, since neither
        # depends on the other's result.
        with ThreadPoolExecutor(max_workers=1) as executor:
            vector_future = executor.submit(
                self.vector_store.search,
                query=query,
                tenant_id=tenant_id,
                user_groups=user_groups,
                k=self.settings.retrieval_fetch_k,
                fetch_k=self.settings.retrieval_fetch_k,
                knowledge_base_ids=knowledge_base_ids,
            )
            lexical_docs = self._lexical_search(
                db, query, tenant_id, user_groups, knowledge_base_ids or []
            )
            vector_docs = vector_future.result()
        vector_docs = self._filter_effective_documents(db, vector_docs, tenant_id)
        fused, scores = self._rrf(vector_docs, lexical_docs)
        for document in vector_docs:
            chunk_id = str(document.metadata.get("chunk_id"))
            vector_score = document.metadata.get("vector_score")
            if vector_score is not None:
                scores.setdefault(chunk_id, {})["vector_score"] = float(vector_score)
        reranker = build_reranker(
            self.settings.reranker_provider,
            tokenize,
            settings=self.settings,
        )
        blend = _blend_candidates(fused, vector_docs, lexical_docs, self.settings.rerank_k)
        injected = self._statute_injected_documents(db, query, tenant_id, user_groups, fused)
        blend_ids = {str(doc.metadata["chunk_id"]) for doc in blend}
        blend.extend(doc for doc in injected if str(doc.metadata["chunk_id"]) not in blend_ids)
        reranked = reranker.rerank(query, blend)
        for doc in reranked:
            chunk_id = str(doc.metadata["chunk_id"])
            scores.setdefault(chunk_id, {})["rerank"] = float(doc.metadata.get("rerank_score", 0.0))
        injected_ids = {str(doc.metadata["chunk_id"]) for doc in injected}
        # Exact 第N条 matches are authoritative: keep them in the context even if
        # the cross-encoder's truncated window ranked them lower.
        remaining = [doc for doc in reranked if str(doc.metadata["chunk_id"]) not in injected_ids]
        diverse, overflow = _cap_per_document(remaining, self.settings.max_chunks_per_document)
        ordered = [*injected, *diverse, *overflow]
        return RetrievalResult(
            documents=ordered[: self.settings.retrieval_k],
            scores=scores,
        )

    def _filter_effective_documents(
        self, db: Session, documents: list[Document], tenant_id: str
    ) -> list[Document]:
        chunk_ids = {str(doc.metadata.get("chunk_id")) for doc in documents}
        if not chunk_ids:
            return []
        # Single joined query: a chunk_id only survives if it still exists (the
        # JOIN drops stale vector hits after a delete) and its document is
        # currently effective, replacing two separate round trips.
        chunks = (
            db.scalars(
                select(KnowledgeChunk)
                .join(KnowledgeChunk.document)
                .options(joinedload(KnowledgeChunk.document).joinedload(KnowledgeDocument.versions))
                .where(
                    KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.id.in_(chunk_ids),
                )
            )
            .unique()
            .all()
        )
        effective_chunk_ids = {chunk.id for chunk in chunks if self._is_effective(chunk.document)}
        return [
            doc for doc in documents if str(doc.metadata.get("chunk_id")) in effective_chunk_ids
        ]

    def _statute_injected_documents(
        self,
        db: Session,
        query: str,
        tenant_id: str,
        user_groups: list[str],
        candidate_documents: list[Document],
    ) -> list[Document]:
        """Return exact 第N条 matches (and the max-article chunk for count queries).

        Statute-style queries ("第300条是什么") defeat both semantic search (300 vs
        三百) and the top-k fused cutoff; an exact-phrase lookup guarantees the
        right chunk reaches the cross-encoder. The caller keeps the returned
        documents in the final context regardless of rerank order.
        """
        from app.domain.retrieval import statute

        chunks = statute.find_article_chunks(
            db, tenant_id, statute.article_phrases(query), user_groups
        )
        if not chunks and statute.is_count_query(query):
            document_ids = {str(doc.metadata.get("document_id")) for doc in candidate_documents}
            max_chunk = statute.find_max_article_chunk(db, tenant_id, list(document_ids))
            if max_chunk is not None:
                chunks = [max_chunk]
        return [self._to_document(chunk, 0.0) for chunk in chunks]

    @staticmethod
    def _is_effective(document: KnowledgeDocument) -> bool:
        return is_effective_document(document)

    @staticmethod
    def _to_document(chunk: KnowledgeChunk, lexical_score: float) -> Document:
        return Document(
            page_content=chunk.content,
            metadata={
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "title": chunk.document.title,
                "tenant_id": chunk.tenant_id,
                "page_number": chunk.page_number,
                "section": chunk.section,
                "position": chunk.position,
                "lexical_score": lexical_score,
            },
        )

    def _rrf(
        self, vector_docs: list[Document], lexical_docs: list[Document], constant: int = 60
    ) -> tuple[list[Document], dict[str, dict[str, float]]]:
        documents: dict[str, Document] = {}
        totals: dict[str, float] = {}
        details: dict[str, dict[str, float]] = {}
        for source, docs, weight in (
            ("vector", vector_docs, self.settings.hybrid_vector_weight),
            ("lexical", lexical_docs, self.settings.hybrid_lexical_weight),
        ):
            for rank, doc in enumerate(docs, start=1):
                chunk_id = str(doc.metadata.get("chunk_id"))
                documents.setdefault(chunk_id, doc)
                score = weight / (constant + rank)
                totals[chunk_id] = totals.get(chunk_id, 0.0) + score
                details.setdefault(chunk_id, {})[source] = score
        ordered = sorted(
            documents.values(), key=lambda doc: totals[str(doc.metadata["chunk_id"])], reverse=True
        )
        for doc in ordered:
            chunk_id = str(doc.metadata["chunk_id"])
            doc.metadata["retrieval_score"] = totals[chunk_id]
            details[chunk_id]["fused"] = totals[chunk_id]
        return ordered, details


def _blend_candidates(
    fused: list[Document],
    vector_docs: list[Document],
    lexical_docs: list[Document],
    budget: int,
) -> list[Document]:
    """Build the rerank input from every retrieval channel.

    RRF ordering can push a channel's best documents past the fused cutoff (e.g.
    statute-style queries where the semantic and lexical signals disagree). Blend
    the fused list with the top of each channel so lexically-obvious answers
    still reach the cross-encoder, while bounding the input size for latency.
    """
    fused_count = budget * 2 // 3
    channel_count = budget // 3
    merged: dict[str, Document] = {}
    for document in [
        *fused[:fused_count],
        *vector_docs[:channel_count],
        *lexical_docs[:channel_count],
    ]:
        merged.setdefault(str(document.metadata.get("chunk_id")), document)
    return list(merged.values())[: budget * 4 // 3]


def _cap_per_document(
    documents: list[Document], limit: int
) -> tuple[list[Document], list[Document]]:
    """Limit how many chunks from the same source document can fill the
    context. Without this, a single large table (every row reranked near-
    identically since they share a repeated header) can occupy the entire
    top-K and crowd out chunks from other, equally relevant documents.

    Returns (kept, overflow) still in rerank order, so a caller can append
    overflow to backfill the budget if diverse candidates run short.
    """
    counts: dict[str, int] = {}
    kept: list[Document] = []
    overflow: list[Document] = []
    for document in documents:
        document_id = str(document.metadata.get("document_id"))
        if counts.get(document_id, 0) < limit:
            kept.append(document)
            counts[document_id] = counts.get(document_id, 0) + 1
        else:
            overflow.append(document)
    return kept, overflow
