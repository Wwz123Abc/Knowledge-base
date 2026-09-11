from langchain_core.documents import Document
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.vector_store import acl_scopes, vector_id_for_chunk
from app.models import KnowledgeChunk, KnowledgeDocument


class DocumentIndexingMixin:
    def rebuild_memory_index(self, db: Session) -> None:
        if self.settings.vector_backend != "memory":
            return
        chunks = list(
            db.scalars(
                select(KnowledgeChunk).options(
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.acl_entries
                    ),
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.knowledge_base_entries
                    ),
                )
            )
        )
        if not chunks:
            return
        documents, vector_ids = self._vector_payload(chunks)
        self.vector_store.add_documents(documents, ids=vector_ids)

    def reconcile_vector_index(self, db: Session) -> dict[str, int]:
        chunks = list(
            db.scalars(
                select(KnowledgeChunk).options(
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.acl_entries
                    ),
                    selectinload(KnowledgeChunk.document).selectinload(
                        KnowledgeDocument.knowledge_base_entries
                    ),
                )
            )
        )
        documents, desired_ids = self._vector_payload(chunks)
        desired = set(desired_ids)
        actual = self.vector_store.list_ids()
        missing = desired - actual
        orphaned = actual - desired
        if missing:
            selected = [
                (document, vector_id)
                for document, vector_id in zip(documents, desired_ids, strict=True)
                if vector_id in missing
            ]
            self.vector_store.add_documents(
                [item[0] for item in selected], ids=[item[1] for item in selected]
            )
        if orphaned:
            self.vector_store.delete(sorted(orphaned))
        return {
            "expected": len(desired),
            "existing": len(actual),
            "added": len(missing),
            "deleted": len(orphaned),
        }

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
