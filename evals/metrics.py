from __future__ import annotations

import re
from statistics import fmean

from evals.dataset import EvaluationCase


def score_case(case: EvaluationCase, response: dict) -> dict[str, float]:
    answer = str(response.get("answer", ""))
    normalized_answer = _normalize(answer)
    citations = response.get("citations", []) or []
    titles = [str(item.get("title", "")) for item in citations]
    if case.answerable:
        recall = float(bool(case.expected_source and case.expected_source in titles))
        reciprocal_rank = 0.0
        if case.expected_source in titles:
            reciprocal_rank = 1.0 / (titles.index(case.expected_source) + 1)
        referenced_indices = [int(value) for value in re.findall(r"\[(\d+)\]", answer)]
        first_reference_title = None
        if referenced_indices and 1 <= referenced_indices[0] <= len(titles):
            first_reference_title = titles[referenced_indices[0] - 1]
        first_citation_accuracy = float(first_reference_title == case.expected_source)
        correctness = float(
            all(_normalize(phrase) in normalized_answer for phrase in case.expected_phrases)
        )
        no_answer_accuracy = float(not response.get("insufficient_context", False))
    else:
        recall = 1.0
        reciprocal_rank = 1.0
        correctness = float(response.get("insufficient_context", False))
        no_answer_accuracy = correctness
        first_citation_accuracy = float(not citations)

    excerpts = " ".join(str(item.get("excerpt", "")) for item in citations)
    answer_tokens = _meaningful_tokens(answer)
    context_tokens = _meaningful_tokens(excerpts)
    groundedness = (
        len(answer_tokens & context_tokens) / len(answer_tokens) if answer_tokens else 1.0
    )
    citation_accuracy = (
        float(all(item.get("document_id") and item.get("chunk_id") for item in citations))
        if citations
        else float(not case.answerable)
    )
    return {
        "recall_at_k": recall,
        "mrr": reciprocal_rank,
        "first_citation_accuracy": first_citation_accuracy,
        "answer_correctness": correctness,
        "citation_accuracy": citation_accuracy,
        "no_answer_accuracy": no_answer_accuracy,
        "groundedness_heuristic": groundedness,
    }


def aggregate(scores: list[dict[str, float]]) -> dict[str, float]:
    if not scores:
        return {}
    return {key: fmean(item[key] for item in scores) for key in scores[0]}


def _meaningful_tokens(text: str) -> set[str]:
    normalized = text.lower()
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", normalized):
        tokens.update(run[index : index + 2] for index in range(max(len(run) - 1, 0)))
        if len(run) == 1:
            tokens.add(run)
    return tokens


def _normalize(text: str) -> str:
    return re.sub(r"[\s*_`]+", "", text).lower()
