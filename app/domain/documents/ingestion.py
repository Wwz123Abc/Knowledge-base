import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from langchain_core.documents import Document
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.cache import get_retrieval_cache
from app.core.loaders import load_document
from app.core.splitter import split_pages
from app.core.vector_store import acl_scopes, vector_id_for_chunk
from app.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

logger = logging.getLogger("rag.ingestion")

# A job that has been "processing" for longer than this without any progress write belongs to
# a worker that died; another delivery of the task may take it over.
_STALE_CLAIM = timedelta(minutes=60)
MAX_AUTOMATIC_ATTEMPTS = 3
_TRANSIENT_MODULES = {"httpx", "httpcore", "httpx2", "httpcore2", "openai", "requests", "urllib3"}
_TRANSIENT_NAME_PARTS = ("Connect", "Timeout", "RateLimit", "ServiceUnavailable", "Network")
_TRANSIENT_TEXT = ("network is unreachable", "temporary failure", "timed out", "connection reset")


def is_transient_error(exc: BaseException) -> bool:
    """Network-ish failures worth retrying on their own (a bad file is not one of them)."""
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    module = type(exc).__module__.split(".")[0]
    name = type(exc).__name__
    if module in _TRANSIENT_MODULES and any(part in name for part in _TRANSIENT_NAME_PARTS):
        return True
    return isinstance(exc, OSError) and any(text in str(exc).lower() for text in _TRANSIENT_TEXT)


class DocumentIngestionMixin:
    def process_job(self, db: Session, job_id: str, retry_transient: bool = True) -> IngestionJob:
        job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        if not job:
            raise ValueError("入库任务不存在")
        if job.status in {"completed", "cancelled"}:
            return job
        if not self._claim_job(db, job_id):
            # Another worker already owns this job (a duplicate delivery or a double retry
            # click); processing it twice would write every chunk twice.
            return db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        document = db.scalar(
            select(KnowledgeDocument)
            .options(
                selectinload(KnowledgeDocument.acl_entries),
                selectinload(KnowledgeDocument.chunks).selectinload(KnowledgeChunk.vector_entries),
                selectinload(KnowledgeDocument.knowledge_base_entries),
            )
            .where(KnowledgeDocument.id == job.document_id)
        )
        if not document:
            job.status = "failed"
            job.error_message = "待处理文档不存在"
            db.commit()
            raise ValueError("待处理文档不存在")
        if job.cancel_requested:
            job.status = "cancelled"
            self._settle_document(document, "cancelled")
            db.commit()
            return job

        job.progress = 10
        job.attempts += 1
        job.error_message = None
        self._settle_document(document, "processing")
        db.commit()

        try:
            stored_path = Path(document.stored_path)
            pages = load_document(stored_path, self.settings)
            if not pages:
                raise ValueError("文件中没有提取到可用文字；扫描版 PDF 请先进行 OCR")
            job.progress = 35
            db.commit()
            chunks = split_pages(
                pages=pages,
                base_metadata={
                    "document_id": document.id,
                    "title": document.title,
                    "department": document.department,
                    "tenant_id": document.tenant_id,
                    "embedding_model_version": self.settings.embedding_model_version,
                },
                chunk_size=self.settings.chunk_size,
                chunk_overlap=self.settings.chunk_overlap,
            )
            vector_documents: list[Document] = []
            vector_ids: list[str] = []
            chunk_models: list[KnowledgeChunk] = []
            scopes = acl_scopes(document.access_groups)
            base_scopes = document.knowledge_base_ids or ["__default__"]
            for item in chunks:
                chunk_id = str(uuid4())
                item.metadata["chunk_id"] = chunk_id
                vector_id = vector_id_for_chunk(chunk_id)
                # One vector row per chunk: ACL groups and knowledge bases are
                # carried as arrays in metadata instead of fanning the same
                # embedding out into one duplicate row per (scope, kb)
                # combination, which used to multiply embedding cost, storage
                # and index size by len(scopes) * len(base_scopes).
                scoped_document = Document(
                    page_content=item.page_content,
                    metadata={
                        **item.metadata,
                        "acl_scopes": scopes,
                        "knowledge_base_scopes": base_scopes,
                    },
                )
                vector_documents.append(scoped_document)
                vector_ids.append(vector_id)
                chunk_model = KnowledgeChunk(
                    id=chunk_id,
                    document_id=document.id,
                    tenant_id=document.tenant_id,
                    access_group=document.access_group,
                    position=int(item.metadata["position"]),
                    page_number=item.metadata.get("page_number"),
                    section=item.metadata.get("section"),
                    content=item.page_content,
                    vector_id=vector_id,
                )
                chunk_models.append(chunk_model)
            if job.cancel_requested:
                job.status = "cancelled"
                self._settle_document(document, "cancelled")
                db.commit()
                return job
            job.progress = 65
            db.commit()
            old_vector_ids = [
                entry.vector_id for old in document.chunks for entry in old.vector_entries
            ]
            if not old_vector_ids:
                old_vector_ids = [old.vector_id for old in document.chunks]
            self.vector_store.add_documents(vector_documents, ids=vector_ids)
            try:
                for old in document.chunks:
                    db.delete(old)
                db.add_all(chunk_models)
                document.chunk_count = len(chunk_models)
                document.embedding_model_version = self.settings.embedding_model_version
                document.status = "ready"
                job.status = "completed"
                job.progress = 100
                db.commit()
                get_retrieval_cache().invalidate_tenant(job.tenant_id)
            except Exception:
                db.rollback()
                self.vector_store.delete(vector_ids)
                raise
            self._reconcile_scopes_changed_meanwhile(db, document, vector_ids, scopes, base_scopes)
            if old_vector_ids:
                try:
                    self.vector_store.delete(old_vector_ids)
                except Exception:
                    logger.exception(
                        "Old vectors could not be removed; reconciliation will clean them",
                        extra={"document_id": document.id},
                    )
            db.refresh(job)
            return job
        except Exception as exc:
            db.rollback()
            job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
            document = db.scalar(
                select(KnowledgeDocument).where(KnowledgeDocument.id == job.document_id)
            )
            if (
                retry_transient
                and is_transient_error(exc)
                and job.attempts < MAX_AUTOMATIC_ATTEMPTS
            ):
                # Back to the queue; the task layer schedules the next attempt.
                job.status = "queued"
                job.error_message = f"临时错误，稍后自动重试：{exc}"[:4000]
                self._settle_document(document, "queued")
                db.commit()
                return job
            job.status = "failed"
            job.error_message = str(exc)[:4000]
            self._settle_document(document, "failed")
            db.commit()
            return job

    @staticmethod
    def _claim_job(db: Session, job_id: str) -> bool:
        """Atomically move a queued (or abandoned) job to 'processing'; False if it's taken."""
        stale_before = datetime.now(UTC) - _STALE_CLAIM
        result = db.execute(
            update(IngestionJob)
            .where(
                IngestionJob.id == job_id,
                or_(
                    IngestionJob.status == "queued",
                    and_(
                        IngestionJob.status == "processing",
                        IngestionJob.updated_at < stale_before,
                    ),
                ),
            )
            .values(status="processing")
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return result.rowcount == 1

    @staticmethod
    def _settle_document(document: KnowledgeDocument, status: str) -> None:
        # A document that already has indexed chunks keeps serving them while a new version
        # is processed, and keeps doing so if that processing fails or is cancelled; only a
        # document with nothing indexed yet takes on the transient status.
        if not document.chunk_count:
            document.status = status

    def _reconcile_scopes_changed_meanwhile(
        self,
        db: Session,
        document: KnowledgeDocument,
        vector_ids: list[str],
        scopes: list[str],
        base_scopes: list[str],
    ) -> None:
        """The job reads a document's ACL/knowledge bases once, at the start. If an admin
        changed them while it ran, the vectors it just wrote carry the old values; correct
        them now, otherwise the vector channel would keep honoring a permission that was
        already revoked."""
        db.expire(document, ["acl_entries", "knowledge_base_entries"])
        current_scopes = acl_scopes(document.access_groups)
        current_bases = document.knowledge_base_ids or ["__default__"]
        if current_scopes == scopes and current_bases == base_scopes:
            return
        try:
            self.vector_store.update_metadata(
                vector_ids,
                {"acl_scopes": current_scopes, "knowledge_base_scopes": current_bases},
            )
            get_retrieval_cache().invalidate_tenant(document.tenant_id)
        except Exception:
            logger.exception(
                "Could not apply access changes made during indexing",
                extra={"document_id": document.id},
            )
