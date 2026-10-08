from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import Headers, UploadFile

from app.config import Settings, get_settings
from app.core.loaders import SUPPORTED_EXTENSIONS
from app.models import (
    ConnectorItem,
    ConnectorSyncRun,
    IngestionJob,
    KnowledgeConnector,
    KnowledgeDocument,
)
from app.services import get_document_service

logger = logging.getLogger("rag.connectors")

# Below this many synced files a "most of them vanished" check says nothing useful.
_MASS_DELETE_MIN_FILES = 5


@dataclass(slots=True)
class ConnectorDocument:
    external_id: str
    filename: str
    path: Path
    size: int
    title: str
    department: str | None
    access_groups: tuple[str, ...]
    _hash: str | None = None

    @property
    def content(self) -> bytes:
        return self.path.read_bytes()

    @property
    def content_hash(self) -> str:
        # Hashed from disk in chunks and only when asked for: reading every file of a large
        # directory into memory up front is what a sync of a big share used to do.
        if self._hash is None:
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            self._hash = digest.hexdigest()
        return self._hash


class DirectoryConnector:
    def __init__(self, configuration: dict, settings: Settings):
        configured = Path(str(configuration.get("path", ""))).resolve()
        if not any(_is_within(configured, root) for root in settings.connector_root_list):
            raise ValueError("连接器目录不在允许的根目录中")
        if not configured.is_dir():
            raise ValueError("连接器目录不存在")
        self.root = configured
        self.recursive = bool(configuration.get("recursive", True))
        self.department = str(configuration.get("department", "")).strip() or None
        groups = configuration.get("access_groups", [])
        self.access_groups = tuple(str(item).strip() for item in groups if str(item).strip())

    def documents(self) -> list[ConnectorDocument]:
        pattern = "**/*" if self.recursive else "*"
        connector_documents = []
        for path in sorted(self.root.glob(pattern)):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            # Office lock files ("~$report.docx") and dotfiles are editor droppings, not content.
            if path.name.startswith(("~$", ".")):
                continue
            # A symlink inside the share must not be a way to pull in files from outside it.
            if not _is_within(path.resolve(), self.root):
                continue
            relative = path.relative_to(self.root).as_posix()
            connector_documents.append(
                ConnectorDocument(
                    external_id=relative,
                    filename=path.name,
                    path=path,
                    size=path.stat().st_size,
                    title=path.stem,
                    department=self.department,
                    access_groups=self.access_groups,
                )
            )
        return connector_documents


class ConnectorService:
    def __init__(self, settings: Settings | None = None, document_service=None):
        self.settings = settings or get_settings()
        self.document_service = document_service

    def create(
        self,
        db: Session,
        tenant_id: str,
        user_id: str,
        name: str,
        connector_type: str,
        configuration: dict,
    ) -> KnowledgeConnector:
        self._build(connector_type, configuration)
        connector = KnowledgeConnector(
            tenant_id=tenant_id,
            name=name,
            connector_type=connector_type,
            configuration=configuration,
            created_by=user_id,
        )
        db.add(connector)
        db.commit()
        db.refresh(connector)
        return connector

    def list(
        self, db: Session, tenant_id: str, offset: int = 0, limit: int = 100
    ) -> list[KnowledgeConnector]:
        return list(
            db.scalars(
                select(KnowledgeConnector)
                .where(KnowledgeConnector.tenant_id == tenant_id)
                .order_by(KnowledgeConnector.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )

    def start_sync(
        self, db: Session, connector_id: str, tenant_id: str, user_id: str
    ) -> ConnectorSyncRun | None:
        connector = db.scalar(
            select(KnowledgeConnector).where(
                KnowledgeConnector.id == connector_id,
                KnowledgeConnector.tenant_id == tenant_id,
                KnowledgeConnector.enabled.is_(True),
            )
        )
        if not connector:
            return None
        run = ConnectorSyncRun(
            connector_id=connector.id,
            tenant_id=tenant_id,
            status="queued",
            created_by=user_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def fail_run(self, db: Session, run_id: str, message: str) -> None:
        run = db.scalar(select(ConnectorSyncRun).where(ConnectorSyncRun.id == run_id))
        if run is not None:
            run.status = "failed"
            run.error_message = message[:4000]
            run.finished_at = datetime.now(UTC)
            db.commit()

    def execute_sync(self, db: Session, run_id: str) -> ConnectorSyncRun:
        run = db.scalar(select(ConnectorSyncRun).where(ConnectorSyncRun.id == run_id))
        if not run:
            raise ValueError("同步任务不存在")
        connector = db.scalar(
            select(KnowledgeConnector)
            .options(selectinload(KnowledgeConnector.items))
            .where(KnowledgeConnector.id == run.connector_id)
        )
        run.status = "processing"
        run.started_at = datetime.now(UTC)
        db.commit()
        try:
            source = self._build(connector.connector_type, connector.configuration)
            documents = source.documents()
            run.discovered = len(documents)
            existing = {item.external_id: item for item in connector.items}
            seen: set[str] = set()
            service = self.document_service or get_document_service()
            max_bytes = self.settings.max_upload_mb * 1024 * 1024
            failures: list[str] = []
            for item in documents:
                seen.add(item.external_id)
                mapping = existing.get(item.external_id)
                # One bad file (unreadable, oversized, a transient stage_upload/
                # process_job failure) must not abort the whole run: each item gets its
                # own try/except + commit, so a failure is recorded and skipped instead of
                # taking every later item down with it.
                try:
                    if item.size > max_bytes:
                        raise ValueError(f"文件超过 {self.settings.max_upload_mb} MB 上限")
                    if mapping and mapping.content_hash == item.content_hash:
                        mapping.last_seen_at = datetime.now(UTC)
                        # Unchanged, but if its last ingestion failed, the next sync is the
                        # retry — otherwise the same hash would be skipped forever.
                        job_id = self._failed_job_to_retry(db, connector, mapping.document_id)
                        if job_id:
                            self._process(service, db, job_id)
                            run.updated_count += 1
                        db.commit()
                        continue
                    upload = UploadFile(
                        file=BytesIO(item.content),
                        filename=item.filename,
                        headers=Headers({"content-type": "application/octet-stream"}),
                    )
                    if mapping:
                        _, job = service.stage_new_version(
                            db,
                            mapping.document_id,
                            upload,
                            connector.tenant_id,
                            connector.created_by,
                        )
                        mapping.content_hash = item.content_hash
                        mapping.last_seen_at = datetime.now(UTC)
                        run.updated_count += 1
                    else:
                        document, job = service.stage_upload(
                            db,
                            upload,
                            item.title,
                            item.department,
                            list(item.access_groups),
                            connector.tenant_id,
                            connector.created_by,
                        )
                        db.add(
                            ConnectorItem(
                                connector_id=connector.id,
                                external_id=item.external_id,
                                document_id=document.id,
                                content_hash=item.content_hash,
                            )
                        )
                        run.created_count += 1
                    # Persist the mapping *before* indexing, so a failed indexing leaves a
                    # record that the next sync can find and retry instead of re-staging the
                    # same content (which the duplicate check would then refuse forever).
                    db.commit()
                    self._process(service, db, job.id)
                    db.commit()
                except Exception as item_exc:
                    # A failed statement aborts the whole Postgres transaction until
                    # rolled back — without this, every item after the first failure
                    # would also fail, for an unrelated reason (transaction already
                    # aborted), not the one actually recorded here.
                    db.rollback()
                    run = self._run(db, run_id)
                    run.failed_count += 1
                    failures.append(f"{item.external_id}: {item_exc}")
                    db.commit()

            missing = [
                mapping for external_id, mapping in existing.items() if external_id not in seen
            ]
            if (
                len(existing) >= _MASS_DELETE_MIN_FILES
                and len(missing) / len(existing) > self.settings.connector_max_delete_ratio
            ):
                # A share that is unmounted, emptied or briefly unreachable looks exactly like
                # "everything was deleted"; acting on that would wipe the knowledge base.
                failures.append(
                    f"已同步的 {len(existing)} 个文件中有 {len(missing)} 个本次未发现，"
                    "疑似目录未挂载或被清空，已跳过删除以防误删；确认无误后可手动删除文档"
                )
                missing = []
            run = self._run(db, run_id)
            for mapping in missing:
                try:
                    document_id = mapping.document_id
                    db.delete(mapping)
                    db.flush()
                    service.delete_document(db, document_id, connector.tenant_id)
                    run.deleted_count += 1
                except Exception as delete_exc:
                    db.rollback()
                    run = self._run(db, run_id)
                    run.failed_count += 1
                    failures.append(f"删除 {mapping.external_id}: {delete_exc}")
                    db.commit()
            run = self._run(db, run_id)
            run.status = "partial" if failures else "completed"
            if failures:
                run.error_message = "\n".join(failures)[:4000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            db.refresh(run)
            return run
        except Exception as exc:
            db.rollback()
            run = self._run(db, run_id)
            run.status = "failed"
            run.error_message = str(exc)[:4000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            return run

    @staticmethod
    def _run(db: Session, run_id: str) -> ConnectorSyncRun:
        return db.scalar(select(ConnectorSyncRun).where(ConnectorSyncRun.id == run_id))

    @staticmethod
    def _process(service, db: Session, job_id: str) -> None:
        job = service.process_job(db, job_id, retry_transient=False)
        if job.status != "completed":
            raise RuntimeError(job.error_message or f"入库未完成（{job.status}）")

    @staticmethod
    def _failed_job_to_retry(
        db: Session, connector: KnowledgeConnector, document_id: str
    ) -> str | None:
        latest = db.scalar(
            select(IngestionJob)
            .where(IngestionJob.document_id == document_id)
            .order_by(IngestionJob.created_at.desc())
            .limit(1)
        )
        if latest is None or latest.status != "failed":
            return None
        if db.get(KnowledgeDocument, document_id) is None:
            return None
        retry = IngestionJob(
            tenant_id=connector.tenant_id,
            document_id=document_id,
            status="queued",
            progress=0,
            created_by=connector.created_by,
        )
        db.add(retry)
        db.commit()
        return retry.id

    def get_run(self, db: Session, run_id: str, tenant_id: str) -> ConnectorSyncRun | None:
        return db.scalar(
            select(ConnectorSyncRun).where(
                ConnectorSyncRun.id == run_id,
                ConnectorSyncRun.tenant_id == tenant_id,
            )
        )

    def _build(self, connector_type: str, configuration: dict):
        if connector_type == "directory":
            return DirectoryConnector(configuration, self.settings)
        raise ValueError(f"不支持的连接器类型：{connector_type}")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
