from io import BytesIO

from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, UploadFile

from app.auth import AuthContext
from app.config import Settings
from app.core.rag import RagService
from app.core.vector_store import EnterpriseVectorStore
from app.db import Base
from app.schemas import AskRequest
from app.services import DocumentService


class TinyEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [self._embed(text) for text in texts]

    def embed_query(self, text):
        return self._embed(text)

    @staticmethod
    def _embed(text):
        return [float(text.count("年假")), float(text.count("报销")), 1.0]


class FakeChatModel:
    def invoke(self, messages):
        return AIMessage(content="员工每个自然年度享有五天带薪年假。[1]")

    def stream(self, messages):
        yield AIMessage(content="员工享有五天年假。[1]")


class TestRagService(RagService):
    __test__ = False

    def _model(self, content=""):
        return FakeChatModel()


def test_document_to_authorized_answer_flow(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path,
        vector_backend="memory",
        openai_api_key="test-only",
        enable_query_rewrite=False,
    )
    vector_store = EnterpriseVectorStore(settings, TinyEmbeddings())
    document_service = DocumentService(settings, vector_store)
    rag_service = TestRagService(settings, vector_store)
    upload = UploadFile(
        file=BytesIO("# 年假制度\n员工每年享有五天带薪年假。".encode()),
        filename="handbook.md",
        headers=Headers({"content-type": "text/markdown"}),
    )
    auth = AuthContext("u-1", "测试用户", "tenant-a", ("hr",), ("user",))

    with Session(engine) as db:
        _, job = document_service.stage_upload(
            db, upload, "员工手册", "HR", ["hr"], "tenant-a", "admin"
        )
        document_service.process_job(db, job.id)
        response = rag_service.ask(db, AskRequest(question="员工有多少天年假？"), auth)

    assert "五天" in response.answer
    assert response.citations[0].title == "员工手册"
    assert response.insufficient_context is False
    assert response.trace_id


def test_two_departments_and_tenants_never_cross_acl_boundaries(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        upload_dir=tmp_path,
        vector_backend="memory",
        openai_api_key="test-only",
        enable_query_rewrite=False,
    )
    vector_store = EnterpriseVectorStore(settings, TinyEmbeddings())
    document_service = DocumentService(settings, vector_store)
    rag_service = TestRagService(settings, vector_store)
    uploads = [
        (
            UploadFile(
                file=BytesIO("# 年假制度\n员工每年享有五天带薪年假。".encode()),
                filename="hr-policy.md",
                headers=Headers({"content-type": "text/markdown"}),
            ),
            "人力资源制度",
            "HR",
            ["hr"],
        ),
        (
            UploadFile(
                file=BytesIO("# 报销制度\n差旅报销必须提交有效发票。".encode()),
                filename="finance-policy.md",
                headers=Headers({"content-type": "text/markdown"}),
            ),
            "财务报销制度",
            "Finance",
            ["finance"],
        ),
    ]

    with Session(engine) as db:
        for upload, title, department, groups in uploads:
            _, job = document_service.stage_upload(
                db, upload, title, department, groups, "tenant-a", "admin"
            )
            document_service.process_job(db, job.id)

        finance = rag_service.ask(
            db,
            AskRequest(question="差旅报销需要什么材料？"),
            AuthContext("finance-user", "财务用户", "tenant-a", ("finance",), ("user",)),
        )
        hr = rag_service.ask(
            db,
            AskRequest(question="差旅报销需要什么材料？"),
            AuthContext("hr-user", "人事用户", "tenant-a", ("hr",), ("user",)),
        )
        other_tenant = rag_service.ask(
            db,
            AskRequest(question="差旅报销需要什么材料？"),
            AuthContext("other", "其他租户", "tenant-b", ("finance",), ("user",)),
        )

    assert {citation.title for citation in finance.citations} == {"财务报销制度"}
    assert "财务报销制度" not in {citation.title for citation in hr.citations}
    assert other_tenant.citations == []
    assert other_tenant.insufficient_context is True
