from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage
from prometheus_client import generate_latest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, UploadFile

from app import main as main_module
from app.auth import AuthContext
from app.config import Settings, get_settings
from app.core.rag import RagService
from app.core.vector_store import EnterpriseVectorStore
from app.db import Base
from app.main import _api_docs_options, app
from app.schemas import AskRequest
from app.services import DocumentService


def test_production_refuses_to_start_with_dev_auth():
    with pytest.raises(ValueError, match="AUTH_MODE=dev"):
        Settings(app_env="production", auth_mode="dev")
    assert Settings(app_env="production", auth_mode="wecom").auth_mode == "wecom"
    assert Settings(app_env="development", auth_mode="dev").auth_mode == "dev"


def test_production_hides_api_docs():
    production = get_settings().model_copy(update={"app_env": "production"})
    assert _api_docs_options(production) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }
    assert _api_docs_options(get_settings()) == {}


def test_metrics_endpoint_is_closed_in_production_unless_a_token_is_configured(monkeypatch):
    production = get_settings().model_copy(update={"app_env": "production"})
    with TestClient(app) as client:
        assert client.get("/api/metrics").status_code == 200  # development stays open

        monkeypatch.setattr(main_module, "settings", production)
        assert client.get("/api/metrics").status_code == 404

        with_token = production.model_copy(update={"metrics_token": "s3cret"})
        monkeypatch.setattr(main_module, "settings", with_token)
        assert client.get("/api/metrics").status_code == 401
        assert (
            client.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
        ok = client.get("/api/metrics", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


def test_unmatched_urls_share_one_metric_label():
    with TestClient(app) as client:
        client.get("/definitely/not/a/route/4242")
    exposition = generate_latest().decode()
    assert "/definitely/not/a/route/4242" not in exposition
    assert 'path="unmatched"' in exposition


class TinyEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [self._embed(text) for text in texts]

    def embed_query(self, text):
        return self._embed(text)

    @staticmethod
    def _embed(text):
        return [float(text.count("年假")), float(text.count("报销")), 1.0]


class ProbeModel:
    """Records whether the DB session still holds a transaction while the model runs."""

    def __init__(self):
        self.db = None
        self.in_transaction: list[bool] = []

    def invoke(self, _messages):
        self.in_transaction.append(self.db.in_transaction())
        return AIMessage(content="员工享有五天年假。[1]")

    def stream(self, _messages):
        self.in_transaction.append(self.db.in_transaction())
        yield AIMessage(content="员工享有五天年假。[1]")


def test_database_connection_is_released_while_the_model_generates(tmp_path):
    probe = ProbeModel()

    class ProbedRag(RagService):
        def _model(self, content=""):
            return probe

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path,
        vector_backend="memory",
        openai_api_key="test-only",
        enable_query_rewrite=False,
    )
    store = EnterpriseVectorStore(settings, TinyEmbeddings())
    documents = DocumentService(settings, store)
    rag = ProbedRag(settings, store)
    upload = UploadFile(
        file=BytesIO("# 年假制度\n员工每年享有五天带薪年假。".encode()),
        filename="handbook.md",
        headers=Headers({"content-type": "text/markdown"}),
    )
    auth = AuthContext("u-1", "测试用户", "tenant-a", ("hr",), ("user",))
    with Session(engine) as db:
        _, job = documents.stage_upload(db, upload, "员工手册", "HR", ["hr"], "tenant-a", "admin")
        documents.process_job(db, job.id)
        probe.db = db
        events = list(rag.stream(db, AskRequest(question="员工有多少天年假？"), auth))
        answered = rag.ask(db, AskRequest(question="员工有多少天年假？"), auth)

    assert events[-1]["event"] == "done"
    assert "五天" in answered.answer
    # A held transaction means a pooled connection stays checked out for the whole
    # generation; with the default pool that capped concurrent answers at 15.
    assert probe.in_transaction == [False, False]
