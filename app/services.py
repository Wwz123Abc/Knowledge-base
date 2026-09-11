from threading import Lock

from app.config import Settings, get_settings
from app.core.rag import RagService
from app.core.vector_store import EnterpriseVectorStore, build_embeddings
from app.domain.documents.indexing import DocumentIndexingMixin
from app.domain.documents.ingestion import DocumentIngestionMixin
from app.domain.documents.lifecycle import DocumentLifecycleMixin
from app.domain.documents.uploads import DocumentUploadMixin


class DocumentService(
    DocumentUploadMixin,
    DocumentIngestionMixin,
    DocumentLifecycleMixin,
    DocumentIndexingMixin,
):
    def __init__(self, settings: Settings, vector_store: EnterpriseVectorStore | None = None):
        self.settings = settings
        self._vector_store = vector_store

    @property
    def vector_store(self) -> EnterpriseVectorStore:
        if self._vector_store is None:
            self._vector_store = get_vector_store()
        return self._vector_store


_vector_store_lock = Lock()
_vector_store_instance: EnterpriseVectorStore | None = None


def get_vector_store() -> EnterpriseVectorStore:
    global _vector_store_instance
    if _vector_store_instance is None:
        with _vector_store_lock:
            if _vector_store_instance is None:
                settings = get_settings()
                _vector_store_instance = EnterpriseVectorStore(settings, build_embeddings(settings))
    return _vector_store_instance


def get_document_service() -> DocumentService:
    settings = get_settings()
    return DocumentService(settings)


def get_rag_service() -> RagService:
    settings = get_settings()
    return RagService(settings, get_vector_store())
