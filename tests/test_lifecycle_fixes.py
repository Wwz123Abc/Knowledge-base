from __future__ import annotations

from io import BytesIO

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, UploadFile

from app.config import Settings
from app.connectors import ConnectorService
from app.core.vector_store import EnterpriseVectorStore, LocalHashEmbeddings
from app.db import Base
from app.domain.documents.ingestion import is_transient_error
from app.models import (
    ConnectorSyncRun,
    DocumentVersion,
    IngestionJob,
    KnowledgeConnector,
    KnowledgeDocument,
)
from app.services import DocumentService


def _upload(text: str, name: str = "policy.md") -> UploadFile:
    return UploadFile(
        file=BytesIO(text.encode()),
        filename=name,
        headers=Headers({"content-type": "text/markdown"}),
    )


def _env(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        vector_backend="memory",
        connector_allowed_roots=str(tmp_path),
    )
    settings.prepare_directories()
    store = EnterpriseVectorStore(settings, LocalHashEmbeddings(32))
    return engine, settings, store, DocumentService(settings, store)


# ---------- access changes are applied to vectors in place ----------


def test_access_change_updates_vectors_in_place_and_keeps_the_document_online(tmp_path):
    engine, _, store, service = _env(tmp_path)
    with Session(engine) as db:
        document, job = service.stage_upload(
            db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u"
        )
        service.process_job(db, job.id)
        stored = list(store.store.store.values())
        assert stored and all(item["metadata"]["acl_scopes"] == ["__public__"] for item in stored)

        updated, reindex_job = service.update_document(
            db, document.id, "t", None, None, ["hr"], None
        )

        assert reindex_job is None and updated.status == "ready"
        assert all(item["metadata"]["acl_scopes"] == ["hr"] for item in store.store.store.values())
        assert (
            db.scalar(
                select(IngestionJob).where(IngestionJob.created_by == "system-routing-update")
            )
            is None
        )


def test_failed_vector_update_rolls_the_edit_back(tmp_path):
    engine, _, store, service = _env(tmp_path)
    with Session(engine) as db:
        document, job = service.stage_upload(
            db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u"
        )
        service.process_job(db, job.id)

        def broken(*args, **kwargs):
            raise ConnectionError("vector store down")

        store.update_metadata = broken
        with pytest.raises(RuntimeError, match="修改未生效"):
            service.update_document(db, document.id, "t", None, None, ["hr"], None)
        db.expire_all()
        assert db.get(KnowledgeDocument, document.id).access_groups == []


def test_access_change_made_while_indexing_is_not_lost(tmp_path, monkeypatch):
    engine, _, store, service = _env(tmp_path)
    with Session(engine) as db:
        document, job = service.stage_upload(
            db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u"
        )
        original_add = store.add_documents

        def add_then_change_acl(documents, ids):
            # the admin restricts the document while the job is between embedding and commit
            with Session(engine) as other:
                service.update_document(other, document.id, "t", None, None, ["hr"], None)
            return original_add(documents, ids)

        monkeypatch.setattr(store, "add_documents", add_then_change_acl)
        service.process_job(db, job.id)

        scopes = {tuple(item["metadata"]["acl_scopes"]) for item in store.store.store.values()}
        assert scopes == {("hr",)}


# ---------- version bookkeeping ----------


def test_expiring_an_old_version_does_not_take_the_document_offline(tmp_path):
    engine, _, _, service = _env(tmp_path)
    with Session(engine) as db:
        document, job = service.stage_upload(
            db, _upload("# v1\n内容一"), "制度", None, [], "t", "u"
        )
        service.process_job(db, job.id)
        _, job2 = service.stage_new_version(db, document.id, _upload("# v2\n内容二"), "t", "u")
        service.process_job(db, job2.id)
        versions = {v.version_number: v for v in db.scalars(select(DocumentVersion))}

        service.update_version(db, versions[1].id, "t", "expired", None, None, None)
        assert db.get(KnowledgeDocument, document.id).status == "ready"

        service.update_version(db, versions[2].id, "t", "expired", None, None, None)
        assert db.get(KnowledgeDocument, document.id).status == "expired"


def test_new_version_keeps_serving_the_old_content_until_it_is_indexed(tmp_path):
    engine, _, _, service = _env(tmp_path)
    with Session(engine) as db:
        document, job = service.stage_upload(
            db, _upload("# v1\n内容一"), "制度", None, [], "t", "u"
        )
        service.process_job(db, job.id)
        _, job2 = service.stage_new_version(db, document.id, _upload("# v2\n内容二"), "t", "u")
        assert db.get(KnowledgeDocument, document.id).status == "ready"

        db.get(KnowledgeDocument, document.id).stored_path = str(tmp_path / "missing.md")
        db.commit()
        failed = service.process_job(db, job2.id)
        assert failed.status == "failed"
        assert db.get(KnowledgeDocument, document.id).status == "ready"  # old chunks still live


# ---------- job claiming and retries ----------


def test_a_claimed_job_is_not_processed_twice(tmp_path):
    engine, _, store, service = _env(tmp_path)
    with Session(engine) as db:
        _, job = service.stage_upload(db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u")
        job.status = "processing"  # another worker already took it
        db.commit()
        result = service.process_job(db, job.id)
        assert result.status == "processing"
        assert not store.store.store  # nothing was embedded


def test_transient_failures_are_requeued_but_bad_files_are_not(tmp_path):
    engine, _, store, service = _env(tmp_path)
    with Session(engine) as db:
        _, job = service.stage_upload(db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u")

        def offline(*args, **kwargs):
            raise ConnectionError("network is unreachable")

        store.add_documents = offline
        requeued = service.process_job(db, job.id)
        assert requeued.status == "queued" and "稍后自动重试" in requeued.error_message

        for _ in range(5):
            result = service.process_job(db, job.id)
        assert result.status == "failed"  # gives up after the automatic attempts

    assert is_transient_error(ConnectionError("x"))
    assert is_transient_error(OSError("[Errno 101] Network is unreachable"))
    assert not is_transient_error(ValueError("文件中没有提取到可用文字"))


def test_enqueue_failure_marks_the_job_failed_and_returns_503(tmp_path, monkeypatch):
    engine, _, _, service = _env(tmp_path)
    from app.api.endpoints import documents as endpoint

    monkeypatch.setattr(endpoint, "get_document_service", lambda: service)

    def queue_down(job_id):
        raise ConnectionError("redis down")

    monkeypatch.setattr(endpoint, "enqueue_ingestion", queue_down)
    with Session(engine) as db:
        _, job = service.stage_upload(db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u")
        with pytest.raises(HTTPException) as caught:
            endpoint._enqueue_or_fail(db, job.id)
        assert caught.value.status_code == 503
        db.expire_all()
        assert db.get(IngestionJob, job.id).status == "failed"


# ---------- reconcile ----------


def test_reconcile_refuses_to_run_while_ingestion_is_active(tmp_path):
    engine, _, _, service = _env(tmp_path)
    with Session(engine) as db:
        service.stage_upload(db, _upload("# 制度\n年假五天。"), "制度", None, [], "t", "u")
        with pytest.raises(ValueError, match="入库任务"):
            service.reconcile_vector_index(db)


# ---------- connector sync ----------


def _connector(db, source, created_by="u"):
    connector = KnowledgeConnector(
        tenant_id="t",
        name="share",
        connector_type="directory",
        configuration={"path": str(source)},
        created_by=created_by,
    )
    db.add(connector)
    db.commit()
    run = ConnectorSyncRun(
        connector_id=connector.id, tenant_id="t", status="queued", created_by="u"
    )
    db.add(run)
    db.commit()
    return connector, run


def _sync(db, settings, service, source):
    _, run = _connector(db, source)
    return ConnectorService(settings, service).execute_sync(db, run.id)


def test_an_emptied_share_does_not_wipe_the_synced_documents(tmp_path):
    engine, settings, _, service = _env(tmp_path)
    source = tmp_path / "share"
    source.mkdir()
    for index in range(6):
        (source / f"doc{index}.md").write_text(f"# 文档{index}\n内容{index}", encoding="utf-8")
    with Session(engine) as db:
        connector, run = _connector(db, source)
        connectors = ConnectorService(settings, service)
        first = connectors.execute_sync(db, run.id)
        assert first.status == "completed" and first.created_count == 6

        for path in source.glob("*.md"):  # the share is unmounted / emptied
            path.unlink()
        second_run = ConnectorSyncRun(
            connector_id=connector.id, tenant_id="t", status="queued", created_by="u"
        )
        db.add(second_run)
        db.commit()
        second = connectors.execute_sync(db, second_run.id)

        assert second.deleted_count == 0
        assert second.status == "partial" and "已跳过删除" in second.error_message
        assert len(list(db.scalars(select(KnowledgeDocument)))) == 6


def test_a_document_whose_indexing_failed_is_retried_on_the_next_sync(tmp_path, monkeypatch):
    engine, settings, store, service = _env(tmp_path)
    source = tmp_path / "share"
    source.mkdir()
    (source / "a.md").write_text("# 制度\n年假五天。", encoding="utf-8")
    with Session(engine) as db:
        connector, run = _connector(db, source)
        connectors = ConnectorService(settings, service)

        original = store.add_documents
        monkeypatch.setattr(
            store, "add_documents", lambda *a, **k: (_ for _ in ()).throw(ValueError("坏了"))
        )
        first = connectors.execute_sync(db, run.id)
        assert first.failed_count == 1 and first.status == "partial"

        monkeypatch.setattr(store, "add_documents", original)
        again = ConnectorSyncRun(
            connector_id=connector.id, tenant_id="t", status="queued", created_by="u"
        )
        db.add(again)
        db.commit()
        second = connectors.execute_sync(db, again.id)

        assert second.status == "completed" and second.failed_count == 0
        document = db.scalar(select(KnowledgeDocument))
        assert document.status == "ready" and document.chunk_count > 0
        assert len(list(db.scalars(select(KnowledgeDocument)))) == 1  # no duplicate staged


def test_connector_skips_symlinks_that_point_outside_and_lock_files(tmp_path):
    from app.connectors import DirectoryConnector

    source = tmp_path / "share"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("# 机密", encoding="utf-8")
    (source / "ok.md").write_text("# ok", encoding="utf-8")
    (source / "~$ok.md").write_text("lock", encoding="utf-8")
    try:
        (source / "link.md").symlink_to(outside / "secret.md")
    except OSError:
        pytest.skip("symlinks need extra privileges on this OS")
    settings = Settings(connector_allowed_roots=str(source))
    names = [
        doc.external_id for doc in DirectoryConnector({"path": str(source)}, settings).documents()
    ]
    assert names == ["ok.md"]
