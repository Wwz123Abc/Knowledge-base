from langchain_core.documents import Document

from app.core.corrective_retrieval import build_corrective_retrieval_graph
from app.core.retrieval import RetrievalResult


def test_corrective_graph_rewrites_and_retries_low_quality_retrieval():
    queries = []

    def retrieve(query):
        queries.append(query)
        if len(queries) == 1:
            return RetrievalResult(documents=[], scores={})
        document = Document(
            page_content="年假为五天",
            metadata={"chunk_id": "c1", "rerank_score": 0.9},
        )
        return RetrievalResult(documents=[document], scores={})

    graph = build_corrective_retrieval_graph(retrieve, lambda query: query + " 带薪年假")
    state = graph.invoke({"query": "假期", "attempts": 0})

    assert queries == ["假期", "假期 带薪年假"]
    assert state["sufficient"] is True


def test_corrective_graph_skips_rewrite_when_first_retrieval_is_already_sufficient():
    document = Document(page_content="年假为五天", metadata={"chunk_id": "c1", "rerank_score": 0.9})
    queries = []

    def retrieve(query):
        queries.append(query)
        return RetrievalResult(documents=[document], scores={})

    graph = build_corrective_retrieval_graph(retrieve, lambda query: query + " 改写")
    state = graph.invoke({"query": "假期", "attempts": 0})

    assert queries == ["假期"]  # no rewrite/retry happened
    assert state["sufficient"] is True


def test_corrective_graph_gives_up_after_max_attempts_without_becoming_sufficient():
    document = Document(
        page_content="不相关内容", metadata={"chunk_id": "c1", "rerank_score": 0.01}
    )
    queries = []

    def retrieve(query):
        queries.append(query)
        return RetrievalResult(documents=[document], scores={})

    graph = build_corrective_retrieval_graph(
        retrieve, lambda query: query + " 改写", max_attempts=2
    )
    state = graph.invoke({"query": "假期", "attempts": 0})

    # max_attempts=2: one retrieval, one rewrite+retry, then the graph must stop
    # even though the score never crossed the threshold — otherwise a persistently
    # low-quality retrieval would rewrite forever instead of falling through to the
    # fallback/insufficient-context path in RagService.
    assert queries == ["假期", "假期 改写"]
    assert state["attempts"] == 2
    assert state["sufficient"] is False


def test_corrective_graph_treats_documents_below_score_threshold_as_insufficient():
    low_score_document = Document(
        page_content="低相关内容", metadata={"chunk_id": "c1", "rerank_score": 0.03}
    )
    high_score_document = Document(
        page_content="年假为五天", metadata={"chunk_id": "c2", "rerank_score": 0.9}
    )
    queries = []

    def retrieve(query):
        queries.append(query)
        if len(queries) == 1:
            return RetrievalResult(documents=[low_score_document], scores={})
        return RetrievalResult(documents=[high_score_document], scores={})

    graph = build_corrective_retrieval_graph(
        retrieve, lambda query: query + " 改写", min_rerank_score=0.05
    )
    state = graph.invoke({"query": "假期", "attempts": 0})

    # Documents present but below min_rerank_score must still trigger a rewrite —
    # "sufficient" isn't just "non-empty", it's "non-empty AND good enough".
    assert queries == ["假期", "假期 改写"]
    assert state["sufficient"] is True
