from __future__ import annotations

import re
from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.applications import Starlette
from starlette.datastructures import Headers, UploadFile
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import AuthContext, get_auth_context
from app.config import get_settings
from app.db import Base, get_db
from app.main import app
from app.middleware import BodySizeLimitMiddleware
from app.rbac import ensure_default_roles
from app.services import DocumentService


def test_index_points_at_the_real_assets_with_a_derived_version():
    with TestClient(app) as client:
        home = client.get("/")
        assert "__ASSET_VERSION__" not in home.text
        match = re.search(r"/static/app\.js\?v=([0-9a-f]{10})", home.text)
        assert match, "index.html must load /static/app.js with a content-derived version"
        assert "/static/styles.css?v=" in home.text
        assert home.headers["cache-control"] == "no-cache"
        script = client.get(f"/static/app.js?v={match.group(1)}")
        assert script.status_code == 200
        # The streamed-answer render throttle shipped in app.js but not in the stale
        # versioned copy index.html used to load; keep the page pointed at the real file.
        assert "scheduleRender" in script.text


async def _echo_length(request):
    return JSONResponse({"bytes": len(await request.body())})


def _limited_app(multipart_limit: int = 2000, default_limit: int = 100):
    return TestClient(
        Starlette(
            routes=[Route("/echo", _echo_length, methods=["POST"])],
            middleware=[
                Middleware(
                    BodySizeLimitMiddleware,
                    multipart_limit=multipart_limit,
                    default_limit=default_limit,
                )
            ],
        )
    )


def test_body_limit_rejects_declared_oversize_and_passes_small_bodies():
    client = _limited_app()
    assert client.post("/echo", content=b"x" * 50).json() == {"bytes": 50}
    too_big = client.post("/echo", content=b"x" * 500)
    assert too_big.status_code == 413
    assert client.get("/echo").status_code == 405  # GET bodies are never inspected


def test_body_limit_applies_to_chunked_bodies_without_content_length():
    client = _limited_app()

    def chunks():
        for _ in range(10):
            yield b"x" * 50

    response = client.post("/echo", content=chunks())
    assert response.status_code == 413


def test_multipart_gets_the_larger_upload_limit():
    client = _limited_app()
    ok = client.post("/echo", files={"file": ("a.txt", b"x" * 500, "text/plain")})
    assert ok.status_code == 200
    too_big = client.post("/echo", files={"file": ("a.txt", b"x" * 5000, "text/plain")})
    assert too_big.status_code == 413


def test_oversized_upload_is_refused_before_authentication():
    limit = (get_settings().max_upload_mb + 1) * 1024 * 1024
    with TestClient(app) as client:
        response = client.post(
            "/api/documents",
            headers={"Content-Length": str(limit + 1), "Content-Type": "multipart/form-data"},
            content=b"",
        )
    assert response.status_code == 413


def test_streamed_upload_without_content_length_is_cut_off_at_the_limit():
    limit = (get_settings().max_upload_mb + 1) * 1024 * 1024
    chunk = b"x" * (1024 * 1024)
    preamble = (
        b"--probe\r\n"
        b'Content-Disposition: form-data; name="file"; filename="a.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )

    def body():
        yield preamble
        for _ in range(limit // len(chunk) + 2):
            yield chunk

    with TestClient(app) as client:
        response = client.post(
            "/api/documents",
            headers={"Content-Type": "multipart/form-data; boundary=probe"},
            content=body(),
        )
    assert response.status_code == 413


def _shared_memory_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def _stage(service, db, name, groups, status="ready"):
    upload = UploadFile(
        file=BytesIO(f"# {name}\n内容".encode()),
        filename=f"{name}.md",
        headers=Headers({"content-type": "text/markdown"}),
    )
    document, job = service.stage_upload(db, upload, name, None, groups, "default", "admin")
    document.status = status
    db.commit()
    return document.id, job.id


def test_document_list_hides_documents_the_caller_cannot_read(tmp_path):
    sessions = _shared_memory_db()
    settings = get_settings().model_copy(update={"upload_dir": tmp_path})
    service = DocumentService(settings)
    with sessions() as db:
        ensure_default_roles(db)
        _stage(service, db, "公开制度", [])
        _stage(service, db, "人事制度", ["hr"])
        _stage(service, db, "财务制度", ["finance"])
        _stage(service, db, "未完成上传", [], status="queued")
        _, job_id = _stage(service, db, "人事草稿", ["hr"], status="failed")

    def override_db():
        with sessions() as db:
            yield db

    def as_user(roles, groups):
        return lambda: AuthContext("u-1", "测试", "default", tuple(groups), tuple(roles))

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            app.dependency_overrides[get_auth_context] = as_user(("user",), ("hr",))
            titles = {item["title"] for item in client.get("/api/documents").json()}
            assert titles == {"公开制度", "人事制度"}
            # job/version metadata is for the people who manage documents, not every employee
            assert client.get(f"/api/jobs/{job_id}").status_code == 403

            app.dependency_overrides[get_auth_context] = as_user(("user",), ())
            titles = {item["title"] for item in client.get("/api/documents").json()}
            assert titles == {"公开制度"}

            app.dependency_overrides[get_auth_context] = as_user(("admin",), ())
            titles = {item["title"] for item in client.get("/api/documents").json()}
            assert titles == {"公开制度", "人事制度", "财务制度", "未完成上传", "人事草稿"}
            assert client.get(f"/api/jobs/{job_id}").status_code == 200
    finally:
        app.dependency_overrides.clear()
