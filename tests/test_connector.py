from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.connectors import ConnectorService, DirectoryConnector
from app.core.vector_store import EnterpriseVectorStore, LocalHashEmbeddings
from app.db import Base
from app.models import KnowledgeDocument
from app.services import DocumentService


def test_directory_connector_is_scoped_and_filters_extensions(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "policy.md").write_text("# Policy", encoding="utf-8")
    (allowed / "ignored.exe").write_bytes(b"binary")
    settings = Settings(connector_allowed_roots=str(allowed))

    connector = DirectoryConnector(
        {"path": str(allowed), "access_groups": ["hr"], "department": "HR"}, settings
    )
    documents = connector.documents()

    assert len(documents) == 1
    assert documents[0].external_id == "policy.md"
    assert documents[0].access_groups == ("hr",)


def test_directory_connector_rejects_path_outside_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    settings = Settings(connector_allowed_roots=str(allowed))

    try:
        DirectoryConnector({"path": str(outside)}, settings)
    except ValueError as exc:
        assert "允许" in str(exc)
    else:
        raise AssertionError("outside path should be rejected")


def test_connector_sync_create_update_and_delete_end_to_end(tmp_path):
    source = tmp_path / "source"
    uploads = tmp_path / "uploads"
    source.mkdir()
    source_file = source / "policy.md"
    source_file.write_text("# 制度\n年假为五天。", encoding="utf-8")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        connector_allowed_roots=str(source),
        upload_dir=uploads,
        vector_backend="memory",
    )
    settings.prepare_directories()
    vector_store = EnterpriseVectorStore(settings, LocalHashEmbeddings(32))
    document_service = DocumentService(settings, vector_store)
    connector_service = ConnectorService(settings, document_service)

    with Session(engine) as db:
        connector = connector_service.create(
            db,
            "tenant-a",
            "admin",
            "制度目录",
            "directory",
            {"path": str(source), "access_groups": ["hr"]},
        )
        run = connector_service.start_sync(db, connector.id, "tenant-a", "admin")
        first = connector_service.execute_sync(db, run.id)
        assert first.status == "completed"
        assert first.created_count == 1

        source_file.write_text("# 制度\n年假为十天。", encoding="utf-8")
        run = connector_service.start_sync(db, connector.id, "tenant-a", "admin")
        second = connector_service.execute_sync(db, run.id)
        assert second.updated_count == 1

        source_file.unlink()
        run = connector_service.start_sync(db, connector.id, "tenant-a", "admin")
        third = connector_service.execute_sync(db, run.id)
        assert third.deleted_count == 1
        assert db.scalar(select(KnowledgeDocument)) is None


def test_connector_sync_skips_a_failing_item_instead_of_aborting_the_whole_run(tmp_path):
    # Regression test: one bad file used to abort the entire sync — every item after it
    # in directory-listing order was never even attempted, and the run was marked
    # "failed" wholesale even though most files were fine.
    source = tmp_path / "source"
    uploads = tmp_path / "uploads"
    source.mkdir()
    (source / "good.md").write_text("# 制度\n年假为五天。", encoding="utf-8")
    # A .pdf extension with non-PDF content passes the connector's own extension
    # filter, then fails validate_stored_file's PDF header check during stage_upload —
    # exercising the actual per-item failure path, not a contrived exception.
    (source / "bad.pdf").write_bytes(b"not a real pdf")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        connector_allowed_roots=str(source),
        upload_dir=uploads,
        vector_backend="memory",
    )
    settings.prepare_directories()
    vector_store = EnterpriseVectorStore(settings, LocalHashEmbeddings(32))
    document_service = DocumentService(settings, vector_store)
    connector_service = ConnectorService(settings, document_service)

    with Session(engine) as db:
        connector = connector_service.create(
            db,
            "tenant-a",
            "admin",
            "制度目录",
            "directory",
            {"path": str(source), "access_groups": ["hr"]},
        )
        run = connector_service.start_sync(db, connector.id, "tenant-a", "admin")
        result = connector_service.execute_sync(db, run.id)

        assert result.status == "partial"
        assert result.created_count == 1  # good.md still made it through
        assert result.failed_count == 1
        assert "bad.pdf" in (result.error_message or "")
        titles = {doc.title for doc in db.scalars(select(KnowledgeDocument))}
        assert titles == {"good"}
