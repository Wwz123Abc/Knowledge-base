from langchain_core.documents import Document
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.vector_store import acl_scopes, vector_id_for_chunk
from app.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

_BATCH = 500


class DocumentIndexingMixin:
    def rebuild_memory_index(self, db: Session) -> None:
        if self.settings.vector_backend != "memory":
            return
        for chunks in self._chunk_batches(db):
            documents, vector_ids = self._vector_payload(chunks)
            self.vector_store.add_documents(documents, ids=vector_ids)

    def reconcile_vector_index(self, db: Session) -> dict[str, int]:
        # Ingestion writes a document's vectors a moment *before* its chunk rows commit. A
        # reconcile that runs in that gap sees those vectors as orphans and deletes them,
        # leaving a finished document with no vectors at all.
        active = db.scalar(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.status.in_(("queued", "processing")))
        )
        if active:
            raise ValueError(f"仍有 {active} 个入库任务在排队或处理，请等它们结束后再重建索引")
        actual = self.vector_store.list_ids()
        desired: set[str] = set()
        added = 0
        # Batched: loading every chunk of every tenant at once does not scale with the corpus.
        for chunks in self._chunk_batches(db):
            documents, vector_ids = self._vector_payload(chunks)
            desired.update(vector_ids)
            missing = [
                (document, vector_id)
                for document, vector_id in zip(documents, vector_ids, strict=True)
                if vector_id not in actual
            ]
            if missing:
                self.vector_store.add_documents(
                    [item[0] for item in missing], ids=[item[1] for item in missing]
                )
                added += len(missing)
        orphaned = actual - desired
        if orphaned:
            self.vector_store.delete(sorted(orphaned))
        return {
            "expected": len(desired),
            "existing": len(actual),
            "added": added,
            "deleted": len(orphaned),
        }

    @staticmethod
    def _chunk_batches(db: Session):
        offset = 0
        while True:
            batch = list(
                db.scalars(
                    select(KnowledgeChunk)
                    .options(
                        selectinload(KnowledgeChunk.document).selectinload(
                            KnowledgeDocument.acl_entries
                        ),
                        selectinload(KnowledgeChunk.document).selectinload(
                            KnowledgeDocument.knowledge_base_entries
                        ),
                    )
                    .order_by(KnowledgeChunk.id)
                    .offset(offset)
                    .limit(_BATCH)
                )
            )
            if not batch:
                return
            yield batch
            offset += len(batch)

    def _vector_payload(self, chunks: list[KnowledgeChunk]) -> tuple[list[Document], list[str]]:
        documents: list[Document] = []
        vector_ids: list[str] = []
        for chunk in chunks:
            documents.append(
                Document(
                    page_content=chunk.content,
                    metadata={
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "title": chunk.document.title,
                        "department": chunk.document.department,
                        "tenant_id": chunk.tenant_id,
                        "acl_scopes": acl_scopes(chunk.document.access_groups),
                        "knowledge_base_scopes": (
                            chunk.document.knowledge_base_ids or ["__default__"]
                        ),
                        "page_number": chunk.page_number,
                        "section": chunk.section,
                        "position": chunk.position,
                        "embedding_model_version": (
                            chunk.document.embedding_model_version
                            or self.settings.embedding_model_version
                        ),
                    },
                )
            )
            vector_ids.append(vector_id_for_chunk(chunk.id))
        return documents, vector_ids
