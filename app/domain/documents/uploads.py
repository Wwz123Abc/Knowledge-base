import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.loaders import SUPPORTED_EXTENSIONS
from app.models import (
    DocumentAccessGroup,
    DocumentKnowledgeBase,
    DocumentVersion,
    IngestionJob,
    KnowledgeDocument,
)
from app.security import validate_stored_file


class DocumentUploadMixin:
    def stage_upload(
        self,
        db: Session,
        upload: UploadFile,
        title: str | None,
        department: str | None,
        access_groups: list[str],
        tenant_id: str,
        user_id: str,
        knowledge_base_ids: list[str] | None = None,
    ) -> tuple[KnowledgeDocument, IngestionJob]:
        filename = Path(upload.filename or "document").name
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError("仅支持 PDF、DOCX、PPTX、图片、Markdown 和 TXT 文件")

        stored_path = self.settings.upload_dir / f"{uuid4()}{suffix}"
        content_hash = hashlib.sha256()
        size = 0
        with stored_path.open("wb") as target:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_upload_mb * 1024 * 1024:
                    target.close()
                    stored_path.unlink(missing_ok=True)
                    raise ValueError(f"文件不能超过 {self.settings.max_upload_mb} MB")
                content_hash.update(chunk)
                target.write(chunk)

        try:
            validate_stored_file(stored_path, self.settings)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise

        digest = content_hash.hexdigest()
        duplicate = db.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.content_hash == digest,
            )
        )
        if duplicate:
            stored_path.unlink(missing_ok=True)
            raise ValueError("该文件内容已存在于知识库")

        document = KnowledgeDocument(
            tenant_id=tenant_id,
            title=(title or Path(filename).stem).strip(),
            filename=filename,
            stored_path=str(stored_path),
            content_type=upload.content_type or "application/octet-stream",
            department=(department or "").strip() or None,
            access_group=access_groups[0] if access_groups else None,
            content_hash=digest,
            status="queued",
        )
        db.add(document)
        db.flush()

        cleaned_groups = list(
            dict.fromkeys(group.strip() for group in access_groups if group.strip())
        )
        from app.knowledge_bases import KnowledgeBaseService

        try:
            valid_base_ids = KnowledgeBaseService().validate_ids(
                db, tenant_id, knowledge_base_ids or []
            )
        except Exception:
            # Every earlier failure path in this method unlinks the file it just wrote
            # before raising; this one didn't, so an invalid/disabled knowledge_base_id
            # left an orphaned file in uploads/ on every failed attempt (the DB row
            # itself rolls back fine since it was only flushed, not committed).
            stored_path.unlink(missing_ok=True)
            raise
        db.add_all(
            [
                DocumentAccessGroup(document_id=document.id, group_name=group)
                for group in cleaned_groups
            ]
        )
        db.add_all(
            [
                DocumentKnowledgeBase(document_id=document.id, knowledge_base_id=base_id)
                for base_id in valid_base_ids
            ]
        )
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash=digest,
            stored_path=str(stored_path),
            status="published",
            owner_id=user_id,
            published_by=user_id,
        )
        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=document.id,
            status="queued",
            progress=0,
            created_by=user_id,
        )
        db.add_all([version, job])
        db.commit()
        db.refresh(document)
        db.refresh(job)
        return document, job

    def stage_new_version(
        self,
        db: Session,
        document_id: str,
        upload: UploadFile,
        tenant_id: str,
        user_id: str,
    ) -> tuple[KnowledgeDocument, IngestionJob]:
        document = db.scalar(
            select(KnowledgeDocument)
            .options(selectinload(KnowledgeDocument.versions))
            .where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.tenant_id == tenant_id,
            )
        )
        if not document:
            raise ValueError("文档不存在")
        filename = Path(upload.filename or document.filename).name
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError("仅支持 PDF、DOCX、PPTX、图片、Markdown 和 TXT 文件")
        stored_path = self.settings.upload_dir / f"{uuid4()}{suffix}"
        digest, _ = self._write_upload(upload, stored_path)
        try:
            validate_stored_file(stored_path, self.settings)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        if digest == document.content_hash:
            stored_path.unlink(missing_ok=True)
            raise ValueError("新版本内容与当前版本相同")
        duplicate_version = db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.content_hash == digest,
            )
        )
        if duplicate_version:
            stored_path.unlink(missing_ok=True)
            raise ValueError("该内容已存在于历史版本中")
        next_version = max((item.version_number for item in document.versions), default=0) + 1
        version = DocumentVersion(
            document_id=document.id,
            version_number=next_version,
            content_hash=digest,
            stored_path=str(stored_path),
            status="published",
            owner_id=user_id,
            published_by=user_id,
        )
        document.filename = filename
        document.stored_path = str(stored_path)
        document.content_type = upload.content_type or document.content_type
        document.content_hash = digest
        document.status = "queued"
        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=document.id,
            status="queued",
            progress=0,
            created_by=user_id,
        )
        db.add_all([version, job])
        db.commit()
        db.refresh(document)
        db.refresh(job)
        return document, job

    def _write_upload(self, upload: UploadFile, stored_path: Path) -> tuple[str, int]:
        content_hash = hashlib.sha256()
        size = 0
        with stored_path.open("wb") as target:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_upload_mb * 1024 * 1024:
                    target.close()
                    stored_path.unlink(missing_ok=True)
                    raise ValueError(f"文件不能超过 {self.settings.max_upload_mb} MB")
                content_hash.update(chunk)
                target.write(chunk)
        return content_hash.hexdigest(), size
