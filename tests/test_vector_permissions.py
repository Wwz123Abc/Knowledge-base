from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.config import Settings
from app.core.vector_store import EnterpriseVectorStore


class TinyEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    @staticmethod
    def _embed(text: str) -> list[float]:
        return [
            float(text.count("年假")),
            float(text.count("报销")),
            float(text.count("采购")),
            1.0,
        ]


def test_search_never_returns_another_access_group():
    store = EnterpriseVectorStore(Settings(vector_backend="memory"), TinyEmbeddings())
    docs = [
        Document(
            page_content="全员年假制度",
            metadata={
                "chunk_id": "public",
                "tenant_id": "default",
                "access_group": None,
            },
        ),
        Document(
            page_content="人力资源部年假细则",
            metadata={
                "chunk_id": "hr",
                "tenant_id": "default",
                "access_group": "hr",
            },
        ),
        Document(
            page_content="财务部年假细则",
            metadata={
                "chunk_id": "finance",
                "tenant_id": "default",
                "access_group": "finance",
            },
        ),
        Document(
            page_content="另一个租户的人力资源年假细则",
            metadata={
                "chunk_id": "other-tenant",
                "tenant_id": "tenant-b",
                "access_group": "hr",
            },
        ),
    ]
    store.add_documents(docs, ids=["public", "hr", "finance", "other-tenant"])

    results = store.search("年假", "default", ["hr"], k=10, fetch_k=10)
    ids = {item.metadata["chunk_id"] for item in results}

    assert "public" in ids
    assert "hr" in ids
    assert "finance" not in ids
    assert "other-tenant" not in ids


def test_global_score_merge_keeps_later_acl_and_knowledge_base_candidates():
    store = EnterpriseVectorStore(Settings(vector_backend="memory"), TinyEmbeddings())
    documents = [
        Document(
            page_content=f"无关机械片段 {index}",
            metadata={
                "chunk_id": f"default-{index}",
                "tenant_id": "default",
                "acl_scope": "__public__",
                "knowledge_base_scope": "__default__",
            },
        )
        for index in range(18)
    ]
    documents.extend(
        [
            Document(
                page_content="员工每年享有五天带薪年假",
                metadata={
                    "chunk_id": "hr-answer",
                    "tenant_id": "default",
                    "acl_scope": "admins",
                    "knowledge_base_scope": "__default__",
                },
            ),
            Document(
                page_content="机械知识库中的年假示例",
                metadata={
                    "chunk_id": "mechanical-answer",
                    "tenant_id": "default",
                    "acl_scope": "__public__",
                    "knowledge_base_scope": "mechanical",
                },
            ),
        ]
    )
    store.add_documents(documents, ids=[str(index) for index in range(len(documents))])

    results = store.search(
        "员工年假",
        "default",
        ["admins"],
        k=6,
        fetch_k=18,
        knowledge_base_ids=["mechanical"],
    )
    ids = {document.metadata["chunk_id"] for document in results}

    assert "hr-answer" in ids
    assert "mechanical-answer" in ids
    assert all("vector_score" in document.metadata for document in results)


def test_pgvector_distance_scores_are_sorted_ascending():
    # pgvector search now runs a raw SQL query (see EnterpriseVectorStore._search_pgvector)
    # instead of going through PGVector's `$in` filter DSL, because each chunk is stored as
    # a single row with acl_scopes/knowledge_base_scopes carried as JSON arrays, and `$in`
    # can't test array overlap. So this fakes the SQLAlchemy engine/connection instead of
    # PGVector's own client.
    store = EnterpriseVectorStore(Settings(vector_backend="memory"), TinyEmbeddings())
    store.settings.vector_backend = "pgvector"

    class FakeRow:
        def __init__(self, document, cmetadata_json, distance):
            self.document = document
            self.cmetadata_json = cmetadata_json
            self.distance = distance

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    captured_params: dict = {}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params):
            captured_params.update(params)
            return FakeResult(
                [
                    FakeRow("较远", '{"chunk_id": "far"}', 0.8),
                    FakeRow("较近", '{"chunk_id": "near"}', 0.1),
                ]
            )

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    # EnterpriseVectorStore now opens its raw-SQL engine once in __init__ and reuses
    # it (see _raw_engine), instead of calling create_engine() fresh on every search —
    # so faking the engine means swapping that stored instance, not patching the
    # create_engine() call site, which _search_pgvector no longer makes.
    store._raw_engine = FakeEngine()
    results = store.search("问题", "default", ["admins"], k=2, fetch_k=2)

    assert set(captured_params["scopes"]) == {"__public__", "admins"}
    assert [document.metadata["chunk_id"] for document in results] == ["near", "far"]


def test_finance_acl_answer_is_not_truncated_after_public_candidates():
    store = EnterpriseVectorStore(Settings(vector_backend="memory"), TinyEmbeddings())
    documents = [
        Document(
            page_content=f"公共机械说明 {index}",
            metadata={
                "chunk_id": f"public-{index}",
                "tenant_id": "default",
                "acl_scope": "__public__",
                "knowledge_base_scope": "__default__",
            },
        )
        for index in range(18)
    ]
    documents.append(
        Document(
            page_content="采购金额超过五万元需要总经理审批",
            metadata={
                "chunk_id": "finance-approval",
                "tenant_id": "default",
                "acl_scope": "finance",
                "knowledge_base_scope": "__default__",
            },
        )
    )
    store.add_documents(documents, ids=[str(index) for index in range(len(documents))])

    results = store.search("采购金额超过五万元由谁审批", "default", ["finance"], 6, 18)

    assert results[0].metadata["chunk_id"] == "finance-approval"
