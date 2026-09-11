from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import Headers, UploadFile

from app.config import Settings, get_settings
from app.core.loaders import SUPPORTED_EXTENSIONS
from app.models import ConnectorItem, ConnectorSyncRun, KnowledgeConnector
from app.services import get_document_service


@dataclass(frozen=True, slots=True)
class ConnectorDocument:
    external_id: str
    filename: str
    content: bytes
    title: str
    department: str | None
    access_groups: tuple[str, ...]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


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
            relative = path.relative_to(self.root).as_posix()
            connector_documents.append(
                ConnectorDocument(
                    external_id=relative,
                    filename=path.name,
                    content=path.read_bytes(),
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
            failures: list[str] = []
            for item in documents:
                seen.add(item.external_id)
                mapping = existing.get(item.external_id)
                if mapping and mapping.content_hash == item.content_hash:
                    mapping.last_seen_at = datetime.now(UTC)
                    continue
                # One bad file (unreadable, oversized, a transient stage_upload/
                # process_job failure) used to abort the whole run: everything after it
                # in `documents` was silently never even attempted. Each item now gets
                # its own try/except + commit, so a failure is recorded and skipped
                # instead of taking every later item down with it.
                try:
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
                    service.process_job(db, job.id)
                    db.commit()
                except Exception as item_exc:
                    # A failed statement aborts the whole Postgres transaction until
                    # rolled back — without this, every item after the first failure
                    # would also fail, for an unrelated reason (transaction already
                    # aborted), not the one actually recorded here.
                    db.rollback()
                    run = db.scalar(select(ConnectorSyncRun).where(ConnectorSyncRun.id == run_id))
                    run.failed_count += 1
                    failures.append(f"{item.external_id}: {item_exc}")
                    db.commit()

            for external_id, mapping in existing.items():
                if external_id in seen:
                    continue
                db.delete(mapping)
                db.flush()
                service.delete_document(db, mapping.document_id, connector.tenant_id)
                run.deleted_count += 1
            run.status = "partial" if failures else "completed"
            if failures:
                run.error_message = "\n".join(failures)[:4000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            db.refresh(run)
            return run
        except Exception as exc:
            db.rollback()
            run = db.scalar(select(ConnectorSyncRun).where(ConnectorSyncRun.id == run_id))
            run.status = "failed"
            run.error_message = str(exc)[:4000]
            run.finished_at = datetime.now(UTC)
            db.commit()
            return run

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
