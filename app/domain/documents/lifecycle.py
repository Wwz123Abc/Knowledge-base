import logging
from pathlib import Path

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.cache import get_retrieval_cache
from app.core.vector_store import acl_scopes
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
        self,
        db: Session,
        tenant_id: str,
        offset: int = 0,
        limit: int = 100,
        visible_to_groups: list[str] | None = None,
    ) -> list[KnowledgeDocument]:
        statement = select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant_id)
        if visible_to_groups is not None:
            # Someone without document.manage must not learn that a document they can't read
            # exists (its title, file name and access groups are themselves sensitive): same
            # "no ACL entries, or any overlap with the caller's groups" rule retrieval uses,
            # and only finished documents (a manager's queued/failed uploads aren't theirs).
            has_acl = exists(
                select(DocumentAccessGroup.id).where(
                    DocumentAccessGroup.document_id == KnowledgeDocument.id
                )
            )
            in_groups = exists(
                select(DocumentAccessGroup.id).where(
                    DocumentAccessGroup.document_id == KnowledgeDocument.id,
                    DocumentAccessGroup.group_name.in_(visible_to_groups),
                )
            )
            statement = statement.where(
                KnowledgeDocument.status == "ready", or_(~has_acl, in_groups)
            )
        return list(
            db.scalars(
                statement.options(
                    selectinload(KnowledgeDocument.acl_entries),
                    selectinload(KnowledgeDocument.knowledge_base_entries),
                )
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
                    document.knowledge_base_entries.append(
                        DocumentKnowledgeBase(knowledge_base_id=base_id)
                    )
        groups_changed = access_groups is not None and old_groups != document.access_groups
        bases_changed = (
            knowledge_base_ids is not None and old_base_ids != document.knowledge_base_ids
        )
        metadata_changed = old_title != document.title or old_department != document.department
        scope_changed = groups_changed or bases_changed
        if scope_changed or metadata_changed:
            # Vectors carry their ACL groups, knowledge bases and title as metadata, so a
            # change is applied to them in place — no re-embedding, and no window in which the
            # document is offline (it used to be re-queued and unsearchable until the job
            # finished, and a change made *while* a job ran was silently lost).
            # Vectors first, DB commit second: if the vector store is unavailable the whole
            # edit is rolled back rather than leaving the two stores disagreeing about who may
            # read the document.
            payload: dict = {}
            if metadata_changed:
                payload.update({"title": document.title, "department": document.department})
            if scope_changed:
                payload.update(
                    {
                        "acl_scopes": acl_scopes(document.access_groups),
                        "knowledge_base_scopes": document.knowledge_base_ids or ["__default__"],
                    }
                )
            vector_ids = [
                entry.vector_id for chunk in document.chunks for entry in chunk.vector_entries
            ]
            if not vector_ids:
                vector_ids = [chunk.vector_id for chunk in document.chunks]
            if vector_ids:
                try:
                    self.vector_store.update_metadata(vector_ids, payload)
                except Exception as exc:
                    db.rollback()
                    logger.exception("Vector metadata update failed; edit rolled back")
                    raise RuntimeError("向量索引暂时不可用，修改未生效，请稍后重试") from exc
        db.commit()
        db.refresh(document)
        if scope_changed or metadata_changed:
            get_retrieval_cache().invalidate_tenant(tenant_id)
        return document, None

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
            # Only the newest version decides whether the document is live. Expiring or
            # re-publishing an old version is bookkeeping and must not take the whole
            # document (whose current version is fine) on or off line.
            latest = max(item.version_number for item in version.document.versions)
            if version.version_number == latest:
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

    def fail_job(self, db: Session, job_id: str, message: str) -> None:
        """Record that a job could not even be queued, so it can be retried from the UI."""
        job = db.scalar(select(IngestionJob).where(IngestionJob.id == job_id))
        if job is None:
            return
        job.status = "failed"
        job.error_message = message[:4000]
        document = db.get(KnowledgeDocument, job.document_id)
        if document is not None and not document.chunk_count:
            document.status = "failed"
        db.commit()

    def cancel_job(self, db: Session, job_id: str, tenant_id: str) -> IngestionJob | None:
        job = self.get_job(db, job_id, tenant_id)
        if not job or job.status in {"completed", "failed", "cancelled"}:
            return None
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            document = db.get(KnowledgeDocument, job.document_id)
            if document and not document.chunk_count:
                document.status = "cancelled"
        db.commit()
        db.refresh(job)
        return job
