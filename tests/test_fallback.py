from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth import AuthContext
from app.config import Settings
from app.core.rag import RagService
from app.core.vector_store import EnterpriseVectorStore
from app.db import Base
from app.models import AuditLog
from app.schemas import AskRequest

AUTH = AuthContext("u-1", "测试用户", "tenant-a", ("hr",), ("user",))


class TinyEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[float(text.count("年假")), 1.0] for text in texts]

    def embed_query(self, text):
        return [float(text.count("年假")), 1.0]


class FallbackModel:
    def invoke(self, _messages):
        return AIMessage(content="公司食堂没有统一供餐，通常自行解决。")


class FailingModel:
    def invoke(self, _messages):
        raise RuntimeError("model unavailable")


class FallbackRagService(RagService):
    def _model(self, content=""):
        return FallbackModel()


class FailingFallbackRagService(RagService):
    def _model(self, content=""):
        return FailingModel()


def _make_service(settings: Settings, service_cls):
    return service_cls(settings, EnterpriseVectorStore(settings, TinyEmbeddings()))


def test_unanswerable_falls_back_to_llm_with_disclaimer(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path,
        vector_backend="memory",
        openai_api_key="test-only",
        enable_query_rewrite=False,
        unanswerable_fallback_enabled=True,
    )
    service = _make_service(settings, FallbackRagService)

    with Session(engine) as db:
        response = service.ask(db, AskRequest(question="公司食堂周三供应什么菜？"), AUTH)

    assert response.fallback is True
    assert response.insufficient_context is True
    assert response.citations == []
    assert "不属于企业官方文档" in response.answer
    assert "公司食堂没有统一供餐" in response.answer

    with Session(engine) as db:
        action = db.scalar(select(AuditLog.action).where(AuditLog.action == "chat.fallback"))
        assert action == "chat.fallback"


def test_unanswerable_fallback_disabled_returns_insufficient(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path,
        vector_backend="memory",
        openai_api_key="test-only",
        enable_query_rewrite=False,
        unanswerable_fallback_enabled=False,
    )
    service = _make_service(settings, FallbackRagService)

    with Session(engine) as db:
        response = service.ask(db, AskRequest(question="公司食堂周三供应什么菜？"), AUTH)

    assert response.fallback is False
    assert response.insufficient_context is True
    assert "知识库中没有找到足够依据" in response.answer


def test_unanswerable_fallback_degrades_when_model_fails(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path,
        vector_backend="memory",
        openai_api_key="test-only",
        enable_query_rewrite=False,
        unanswerable_fallback_enabled=True,
    )
    service = _make_service(settings, FailingFallbackRagService)

    with Session(engine) as db:
        response = service.ask(db, AskRequest(question="公司食堂周三供应什么菜？"), AUTH)

    assert response.fallback is False
    assert response.insufficient_context is True
    assert "知识库中没有找到足够依据" in response.answer
