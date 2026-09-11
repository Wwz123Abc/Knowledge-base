from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class CorrectiveState(TypedDict, total=False):
    query: str
    result: Any
    attempts: int
    sufficient: bool


def build_corrective_retrieval_graph(
    retrieve: Callable[[str], Any],
    rewrite: Callable[[str], str],
    max_attempts: int = 2,
    min_rerank_score: float = 0.05,
):
    def retrieve_node(state: CorrectiveState) -> CorrectiveState:
        return {
            "result": retrieve(state["query"]),
            "attempts": state.get("attempts", 0) + 1,
        }

    def grade_node(state: CorrectiveState) -> CorrectiveState:
        documents = getattr(state.get("result"), "documents", [])
        top_score = max(
            (float(doc.metadata.get("rerank_score", 0.0)) for doc in documents),
            default=0.0,
        )
        return {"sufficient": bool(documents) and top_score >= min_rerank_score}

    def route(state: CorrectiveState) -> str:
        if state.get("sufficient") or state.get("attempts", 0) >= max_attempts:
            return END
        return "rewrite"

    def rewrite_node(state: CorrectiveState) -> CorrectiveState:
        return {"query": rewrite(state["query"])}

    graph = StateGraph(CorrectiveState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("grade", grade_node)
    graph.add_node("rewrite", rewrite_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "grade")
    graph.add_conditional_edges("grade", route, {"rewrite": "rewrite", END: END})
    graph.add_edge("rewrite", "retrieve")
    return graph.compile()
