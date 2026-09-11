import logging
from pathlib import Path
from uuid import uuid4

from langchain_core.documents import Document
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.cache import get_retrieval_cache
from app.core.loaders import load_document
from app.core.splitter import split_pages
from app.core.vector_store import acl_scopes, vector_id_for_chunk
from app.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

logger = logging.getLogger("rag.ingestion")


class DocumentIngestionMixin:
    def process_job(self, db: Session, job_id: str) -> IngestionJob:
        job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        if not job:
            raise ValueError("入库任务不存在")
        if job.status in {"completed", "cancelled"}:
            return job
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
            raise ValueError("待处理文档不存在")
        if job.cancel_requested:
            job.status = "cancelled"
            document.status = "cancelled"
            db.commit()
            return job

        job.status = "processing"
        job.progress = 10
        job.attempts += 1
        job.error_message = None
        document.status = "processing"
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
                document.status = "cancelled"
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
            job.status = "failed"
            job.error_message = str(exc)[:4000]
            document.status = "failed"
            db.commit()
            return job
