from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class CorrectiveState(TypedDict, total=False):
    query: str
    result: Any
    attempts: int
    sufficient: bool
    best_result: Any
    best_score: float


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
        result = state.get("result")
        documents = getattr(result, "documents", [])
        top_score = max(
            (float(doc.metadata.get("rerank_score", 0.0)) for doc in documents),
            default=0.0,
        )
        update: CorrectiveState = {"sufficient": bool(documents) and top_score >= min_rerank_score}
        # Remember the best attempt: a rewrite can drift and come back worse (or empty), and
        # the answer must be built from the strongest retrieval seen, not merely the last.
        if "best_result" not in state or top_score > state.get("best_score", 0.0):
            update["best_result"] = result
            update["best_score"] = top_score
        return update

    def route(state: CorrectiveState) -> str:
        if state.get("sufficient") or state.get("attempts", 0) >= max_attempts:
            return "finish"
        return "rewrite"

    def rewrite_node(state: CorrectiveState) -> CorrectiveState:
        return {"query": rewrite(state["query"])}

    def finish_node(state: CorrectiveState) -> CorrectiveState:
        return {"result": state["best_result"]}

    graph = StateGraph(CorrectiveState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("grade", grade_node)
    graph.add_node("rewrite", rewrite_node)
    graph.add_node("finish", finish_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "grade")
    graph.add_conditional_edges("grade", route, {"rewrite": "rewrite", "finish": "finish"})
    graph.add_edge("rewrite", "retrieve")
    graph.add_edge("finish", END)
    return graph.compile()
