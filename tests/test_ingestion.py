from io import BytesIO
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, UploadFile

from app.config import Settings
from app.db import Base
from app.models import IngestionJob, KnowledgeDocument
from app.services import DocumentService


class RecordingVectorStore:
    def __init__(self):
        self.documents = []
        self.ids = []
        self.deleted = []
        self.metadata_updates = []

    def add_documents(self, documents, ids):
        self.documents.extend(documents)
        self.ids.extend(ids)
        return ids

    def delete(self, ids):
        self.deleted.extend(ids)

    def update_metadata(self, ids, metadata):
        self.metadata_updates.append((list(ids), dict(metadata)))

    def list_ids(self):
        return set(self.ids) - set(self.deleted)


def test_staged_ingestion_tracks_progress_and_multi_group_acl(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = RecordingVectorStore()
    settings = Settings(upload_dir=tmp_path, vector_backend="memory")
    service = DocumentService(settings, store)
    upload = UploadFile(
        file=BytesIO("# 制度\n员工有五天年假。".encode()),
        filename="policy.md",
        headers=Headers({"content-type": "text/markdown"}),
    )

    with Session(engine) as db:
        document, job = service.stage_upload(
            db, upload, "员工制度", "人力资源部", ["hr", "managers"], "tenant-a", "u-1"
        )
        completed = service.process_job(db, job.id)
        saved = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == document.id))
        saved_job = db.scalar(select(IngestionJob).where(IngestionJob.id == job.id))

        assert completed.status == "completed"
        assert saved_job.progress == 100
        assert saved.status == "ready"
        assert saved.access_groups == ["hr", "managers"]
        assert saved.chunk_count == 1
        assert len(store.ids) == 1
        assert set(store.documents[0].metadata["acl_scopes"]) == {"hr", "managers"}

        updated, reindex_job = service.update_document(
            db,
            document.id,
            "tenant-a",
            "员工制度（新版）",
            None,
            None,
            None,
        )

        assert updated.title == "员工制度（新版）"
        assert updated.status == "ready"
        assert reindex_job is None
        assert store.deleted == []
        assert store.metadata_updates == [
            (store.ids, {"title": "员工制度（新版）", "department": "人力资源部"})
        ]


def test_update_document_with_overlapping_access_groups_does_not_crash(tmp_path):
    # Regression test: access_groups=["hr"] -> ["hr", "finance"] (keeping "hr", adding
    # "finance") used to raise a UniqueViolation on (document_id, group_name) — a naive
    # `.clear()` + `.extend()` on the ORM collection can flush the INSERT for the row that's
    # logically unchanged before the DELETE of the row it's replacing.
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = RecordingVectorStore()
    settings = Settings(upload_dir=tmp_path, vector_backend="memory")
    service = DocumentService(settings, store)
    upload = UploadFile(
        file=BytesIO("# 制度\n员工有五天年假。".encode()),
        filename="policy.md",
        headers=Headers({"content-type": "text/markdown"}),
    )

    with Session(engine) as db:
        document, job = service.stage_upload(
            db, upload, "员工制度", "人力资源部", ["hr"], "tenant-a", "u-1"
        )
        service.process_job(db, job.id)

        updated, reindex_job = service.update_document(
            db, document.id, "tenant-a", None, None, ["hr", "finance"], None
        )

        assert set(updated.access_groups) == {"hr", "finance"}
        # Access changes are applied to the stored vectors in place: no re-embedding job, no
        # downtime — the document stays searchable and its vectors carry the new groups.
        assert reindex_job is None
        assert updated.status == "ready"
        last_ids, last_payload = store.metadata_updates[-1]
        assert set(last_payload["acl_scopes"]) == {"hr", "finance"}
        assert last_ids == [vector_id for vector_id in store.ids]

        # Same overlap scenario again, this time shrinking back to just "hr" — the delete
        # side of the same diff logic.
        updated_again, _ = service.update_document(
            db, document.id, "tenant-a", None, None, ["hr"], None
        )
        assert updated_again.access_groups == ["hr"]


def test_stage_upload_cleans_up_orphan_file_when_knowledge_base_id_is_invalid(tmp_path):
    # Regression test: every other failure path in stage_upload (oversized file, failed
    # validate_stored_file, duplicate content) unlinks the file it just wrote before
    # raising. The knowledge_base_ids validation step didn't, so a failed upload with an
    # invalid/disabled knowledge_base_id used to leak a file into uploads/ on every
    # attempt (the DB row itself already rolled back fine — it was only flushed).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    store = RecordingVectorStore()
    settings = Settings(upload_dir=tmp_path, vector_backend="memory")
    service = DocumentService(settings, store)
    upload = UploadFile(
        file=BytesIO("# 制度\n员工有五天年假。".encode()),
        filename="policy.md",
        headers=Headers({"content-type": "text/markdown"}),
    )

    with Session(engine) as db:
        raised = False
        try:
            service.stage_upload(
                db,
                upload,
                "员工制度",
                "人力资源部",
                ["hr"],
                "tenant-a",
                "u-1",
                knowledge_base_ids=["does-not-exist"],
            )
        except ValueError:
            raised = True

        assert raised
        assert list(Path(tmp_path).iterdir()) == []
