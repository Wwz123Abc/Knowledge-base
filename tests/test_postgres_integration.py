"""Runs only when POSTGRES_TEST_URL points at a Postgres with the pgvector extension available
(CI starts one; locally: POSTGRES_TEST_URL=postgresql+psycopg://rag:rag_password@host:5432/rag).

The rest of the suite uses SQLite and an in-memory vector store, so the SQL that production
actually runs — jsonb `?|` ACL filtering, the HNSW index, in-place metadata updates — would
otherwise never be executed by any test.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from langchain_core.documents import Document
from sqlalchemy import create_engine, text

from app.config import Settings
from app.core.vector_store import EnterpriseVectorStore, LocalHashEmbeddings, vector_id_for_chunk

POSTGRES_URL = os.environ.get("POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="POSTGRES_TEST_URL not set")

DIMENSIONS = 512  # same as the production embedding size, so the shared table type matches


def _chunk(chunk_id: str, content: str, tenant: str, scopes: list[str]) -> Document:
    return Document(
        page_content=content,
        metadata={
            "chunk_id": chunk_id,
            "tenant_id": tenant,
            "acl_scopes": scopes,
            "knowledge_base_scopes": ["__default__"],
        },
    )


@pytest.fixture
def store():
    settings = Settings(
        vector_backend="pgvector",
        postgres_vector_url=POSTGRES_URL,
        vector_collection=f"pgtest_{uuid4().hex[:8]}",
        embedding_dimensions=DIMENSIONS,
    )
    store = EnterpriseVectorStore(settings, LocalHashEmbeddings(DIMENSIONS))
    yield store
    store.store.delete_collection()


def test_acl_filtering_hnsw_index_and_in_place_metadata_update(store):
    chunks = {
        "pub": _chunk("pub", "年假制度 公开说明", "t1", ["__public__"]),
        "hr": _chunk("hr", "年假制度 人事内部细则", "t1", ["hr"]),
        "other": _chunk("other", "年假制度 其他租户", "t2", ["__public__"]),
    }
    ids = {name: vector_id_for_chunk(name) for name in chunks}
    store.add_documents(list(chunks.values()), ids=list(ids.values()))

    def visible(tenant, groups):
        found = store.search("年假制度", tenant, groups, k=10, fetch_k=10)
        return {doc.metadata["chunk_id"] for doc in found}

    assert visible("t1", []) == {"pub"}
    assert visible("t1", ["hr"]) == {"pub", "hr"}
    assert visible("t2", ["hr"]) == {"other"}

    # access change applied to the stored vector without re-embedding
    store.update_metadata([ids["hr"]], {"acl_scopes": ["finance"]})
    assert visible("t1", ["hr"]) == {"pub"}
    assert visible("t1", ["finance"]) == {"pub", "hr"}

    engine = create_engine(POSTGRES_URL)
    with engine.connect() as connection:
        index = connection.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = 'ix_langchain_pg_embedding_hnsw'")
        ).scalar()
        column_type = connection.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = to_regclass('langchain_pg_embedding') AND attname = 'embedding'"
            )
        ).scalar()
    assert index == 1
    assert column_type == f"vector({DIMENSIONS})"
    assert isinstance(store._iterative_scan, bool)  # pgvector >= 0.8 enables it


def test_filtered_search_still_finds_the_visible_hit_among_many_hidden_neighbours(store):
    """Smoke test of the iterative-scan path on a real pgvector: many near neighbours the
    caller may not see, one distant hit they may. (On a table this small the planner may not
    even use the HNSW index, so this proves the query runs and filters correctly — not that the
    recall problem iterative scans address is reproduced; that only shows up with real data.)"""
    if not store._iterative_scan:
        pytest.skip("pgvector < 0.8 has no iterative scan")
    hidden = [
        _chunk(f"hidden-{index}", "年假制度 财务保密材料", "t1", ["finance"])
        for index in range(150)
    ]
    mine = _chunk("mine", "完全无关的话题 采购流程", "t1", ["__public__"])
    everything = [*hidden, mine]
    store.add_documents(
        everything, ids=[vector_id_for_chunk(doc.metadata["chunk_id"]) for doc in everything]
    )
    found = store.search("年假制度 财务保密材料", "t1", [], k=5, fetch_k=5)
    assert [doc.metadata["chunk_id"] for doc in found] == ["mine"]
