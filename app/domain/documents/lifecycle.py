import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.cache import get_retrieval_cache
from app.models import (
    DocumentAccessGroup,
    DocumentKnowledgeBase,
    DocumentVersion,
    IngestionJob,
    KnowledgeChunk,
    KnowledgeDocument,
)

logger = logging.getLogger("rag.ingestion")


class DocumentLifecycleMixin:
    def list_documents(
        self, db: Session, tenant_id: str, offset: int = 0, limit: int = 100
    ) -> list[KnowledgeDocument]:
        return list(
            db.scalars(
                select(KnowledgeDocument)
                .options(
                    selectinload(KnowledgeDocument.acl_entries),
                    selectinload(KnowledgeDocument.knowledge_base_entries),
                )
                .where(KnowledgeDocument.tenant_id == tenant_id)
                .order_by(KnowledgeDocument.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )

    def delete_document(self, db: Session, document_id: str, tenant_id: str) -> bool:
        document = db.scalar(
            select(KnowledgeDocument)
            .options(
                selectinload(KnowledgeDocument.chunks).selectinload(KnowledgeChunk.vector_entries),
                selectinload(KnowledgeDocument.acl_entries),
                selectinload(KnowledgeDocument.versions),
                selectinload(KnowledgeDocument.knowledge_base_entries),
            )
            .where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.tenant_id == tenant_id,
            )
        )
        if not document:
            return False
        vector_ids = [
            entry.vector_id for chunk in document.chunks for entry in chunk.vector_entries
        ]
        if not vector_ids:
            vector_ids = [chunk.vector_id for chunk in document.chunks]
        stored_paths = [Path(version.stored_path) for version in document.versions]
        db.delete(document)
        db.commit()
        if vector_ids:
            try:
                self.vector_store.delete(vector_ids)
            except Exception:
                logger.exception(
                    "Document vectors could not be removed; reconciliation will clean them",
                    extra={"document_id": document_id},
                )
        for stored_path in stored_paths:
            try:
                stored_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Deleted document file could not be removed: %s", stored_path)
        get_retrieval_cache().invalidate_tenant(tenant_id)
        return True

    def update_document(
        self,
        db: Session,
        document_id: str,
        tenant_id: str,
        title: str | None,
        department: str | None,
        access_groups: list[str] | None,
        knowledge_base_ids: list[str] | None,
    ) -> tuple[KnowledgeDocument, IngestionJob | None] | None:
        document = db.scalar(
            select(KnowledgeDocument)
            .options(
                selectinload(KnowledgeDocument.acl_entries),
                selectinload(KnowledgeDocument.knowledge_base_entries),
                selectinload(KnowledgeDocument.chunks).selectinload(KnowledgeChunk.vector_entries),
            )
            .where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.tenant_id == tenant_id,
            )
        )
        if not document:
            return None
        old_title = document.title
        old_department = document.department
        old_groups = document.access_groups
        old_base_ids = document.knowledge_base_ids
        original_status = document.status
        reindex_job: IngestionJob | None = None
        if title is not None:
            document.title = title.strip()
        if department is not None:
            document.department = department.strip() or None
        if access_groups is not None:
            # NOT .clear() + .extend(): when the new set overlaps the old one (e.g. keeping
            # "hr" while adding "finance"), SQLAlchemy's flush can emit the INSERT for the
            # row that's logically "the same" before the DELETE of the row it's replacing,
            # tripping the (document_id, group_name) unique constraint. Diffing instead means
            # unchanged entries are never touched, so there's nothing to race.
            cleaned = list(dict.fromkeys(item.strip() for item in access_groups if item.strip()))
            existing_by_name = {entry.group_name: entry for entry in document.acl_entries}
            for group_name, entry in list(existing_by_name.items()):
                if group_name not in cleaned:
                    document.acl_entries.remove(entry)
            for group_name in cleaned:
                if group_name not in existing_by_name:
                    document.acl_entries.append(DocumentAccessGroup(group_name=group_name))
            document.access_group = cleaned[0] if cleaned else None
        if knowledge_base_ids is not None:
            from app.knowledge_bases import KnowledgeBaseService

            valid_ids = KnowledgeBaseService().validate_ids(db, tenant_id, knowledge_base_ids)
            existing_bases = {
                entry.knowledge_base_id: entry for entry in document.knowledge_base_entries
            }
            for base_id, entry in list(existing_bases.items()):
                if base_id not in valid_ids:
                    document.knowledge_base_entries.remove(entry)
            for base_id in valid_ids:
                if base_id not in existing_bases:
                    document.knowledge_base_entries.append(DocumentKnowledgeBase(knowledge_base_id=base_id))
        groups_changed = access_groups is not None and old_groups != document.access_groups
        bases_changed = (
            knowledge_base_ids is not None and old_base_ids != document.knowledge_base_ids
        )
        metadata_changed = old_title != document.title or old_department != document.department
        scope_changed = groups_changed or bases_changed
        if original_status == "ready" and scope_changed:
            document.status = "queued"
            reindex_job = IngestionJob(
                tenant_id=tenant_id,
                document_id=document.id,
                status="queued",
                progress=0,
                created_by="system-routing-update",
            )
            db.add(reindex_job)
        db.commit()
        db.refresh(document)
        if original_status == "ready" and metadata_changed and not scope_changed:
            vector_ids = [
                entry.vector_id for chunk in document.chunks for entry in chunk.vector_entries
            ]
            if not vector_ids:
                vector_ids = [chunk.vector_id for chunk in document.chunks]
            try:
                self.vector_store.update_metadata(
                    vector_ids,
                    {"title": document.title, "department": document.department},
                )
            except Exception:
                logger.exception("Vector metadata update failed; queuing a repair reindex")
                document.status = "queued"
                reindex_job = IngestionJob(
                    tenant_id=tenant_id,
                    document_id=document.id,
                    status="queued",
                    progress=0,
                    created_by="system-metadata-repair",
                )
                db.add(reindex_job)
                db.commit()
        if reindex_job:
            db.refresh(reindex_job)
        if scope_changed or metadata_changed:
            get_retrieval_cache().invalidate_tenant(tenant_id)
        return document, reindex_job

    def list_versions(self, db: Session, document_id: str, tenant_id: str) -> list[DocumentVersion]:
        return list(
            db.scalars(
                select(DocumentVersion)
                .join(DocumentVersion.document)
                .where(
                    DocumentVersion.document_id == document_id,
                    KnowledgeDocument.tenant_id == tenant_id,
                )
                .order_by(DocumentVersion.version_number.desc())
            )
        )

    def update_version(
        self,
        db: Session,
        version_id: str,
        tenant_id: str,
        status: str | None,
        owner_id: str | None,
        valid_from,
        valid_until,
    ) -> DocumentVersion | None:
        version = db.scalar(
            select(DocumentVersion)
            .join(DocumentVersion.document)
            .where(
                DocumentVersion.id == version_id,
                KnowledgeDocument.tenant_id == tenant_id,
            )
        )
        if not version:
            return None
        if valid_from and valid_until and valid_until <= valid_from:
            raise ValueError("有效期结束时间必须晚于开始时间")
        if status is not None:
            version.status = status
            if status == "expired":
                version.document.status = "expired"
            elif status == "published" and version.document.status == "expired":
                version.document.status = "ready"
        if owner_id is not None:
            version.owner_id = owner_id.strip() or None
        if valid_from is not None:
            version.valid_from = valid_from
        if valid_until is not None:
            version.valid_until = valid_until
        db.commit()
        db.refresh(version)
        if status is not None or valid_from is not None or valid_until is not None:
            get_retrieval_cache().invalidate_tenant(tenant_id)
        return version

    def get_job(self, db: Session, job_id: str, tenant_id: str) -> IngestionJob | None:
        return db.scalar(
            select(IngestionJob).where(
                IngestionJob.id == job_id,
                IngestionJob.tenant_id == tenant_id,
            )
        )

    def retry_job(self, db: Session, job_id: str, tenant_id: str) -> IngestionJob | None:
        job = self.get_job(db, job_id, tenant_id)
        if not job or job.status not in {"failed", "cancelled"}:
            return None
        job.status = "queued"
        job.progress = 0
        job.error_message = None
        job.cancel_requested = False
        db.commit()
        db.refresh(job)
        return job

    def cancel_job(self, db: Session, job_id: str, tenant_id: str) -> IngestionJob | None:
        job = self.get_job(db, job_id, tenant_id)
        if not job or job.status in {"completed", "failed", "cancelled"}:
            return None
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            document = db.get(KnowledgeDocument, job.document_id)
            if document:
                document.status = "cancelled"
        db.commit()
        db.refresh(job)
        return job
