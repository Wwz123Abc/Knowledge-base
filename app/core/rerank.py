from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx
from langchain_core.documents import Document

from app.config import Settings

logger = logging.getLogger("rag.reranker")

# build_reranker() makes a fresh instance per retrieval, so the failure memory has to live at
# module level. While the cross-encoder service is down or hanging, skip it for a while
# instead of making every question (and each corrective retry) wait out the timeout again.
_COOLDOWN_SECONDS = 30.0
_unavailable_until = 0.0


class Reranker(Protocol):
    def rerank(self, query: str, documents: list[Document]) -> list[Document]: ...


class TokenOverlapReranker:
    """Fast local baseline that can be replaced by a cross-encoder in production."""

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def rerank(self, query: str, documents: list[Document]) -> list[Document]:
        query_tokens = set(self.tokenizer(query))
        for doc in documents:
            doc_tokens = set(self.tokenizer(doc.page_content))
            overlap = len(query_tokens & doc_tokens) / max(len(query_tokens), 1)
            fused = float(doc.metadata.get("retrieval_score", 0.0))
            doc.metadata["rerank_score"] = overlap * 0.7 + fused * 0.3
        return sorted(
            documents,
            key=lambda doc: float(doc.metadata.get("rerank_score", 0.0)),
            reverse=True,
        )


class CrossEncoderApiReranker:
    """Cohere-compatible rerank API with a deterministic offline fallback."""

    def __init__(self, settings: Settings, fallback: Reranker):
        self.settings = settings
        self.fallback = fallback

    def rerank(self, query: str, documents: list[Document]) -> list[Document]:
        if not documents:
            return []
        if not self.settings.reranker_base_url:
            logger.warning("Cross-encoder endpoint is not configured; using token-overlap fallback")
            return self.fallback.rerank(query, documents)
        global _unavailable_until
        if time.monotonic() < _unavailable_until:
            return self.fallback.rerank(query, documents)
        try:
            scores = self._request_scores(query, documents)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            _unavailable_until = time.monotonic() + _COOLDOWN_SECONDS
            logger.warning("Cross-encoder rerank failed; using token-overlap fallback: %s", exc)
            return self.fallback.rerank(query, documents)
        for index, document in enumerate(documents):
            document.metadata["rerank_score"] = scores[index]
            document.metadata["reranker_provider"] = "cross_encoder_api"
        return sorted(
            documents,
            key=lambda document: float(document.metadata["rerank_score"]),
            reverse=True,
        )

    def _request_scores(self, query: str, documents: list[Document]) -> dict[int, float]:
        endpoint = self.settings.reranker_base_url.rstrip("/")
        if not endpoint.endswith("/rerank"):
            endpoint += "/rerank"
        headers = {"Accept": "application/json"}
        if self.settings.reranker_api_key:
            headers["Authorization"] = f"Bearer {self.settings.reranker_api_key}"
        response = httpx.post(
            endpoint,
            headers=headers,
            json={
                "model": self.settings.reranker_model,
                "query": query,
                "documents": [
                    _query_aware_excerpt(
                        query,
                        document.page_content,
                        self.settings.reranker_max_chars,
                        self.fallback.tokenizer,
                    )
                    for document in documents
                ],
                "top_n": len(documents),
            },
            timeout=self.settings.reranker_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload["results"]
        scores = {int(item["index"]): float(item["relevance_score"]) for item in results}
        if set(scores) != set(range(len(documents))):
            raise ValueError("reranker response does not contain every document")
        return scores


def build_reranker(provider: str, tokenizer, settings: Settings | None = None) -> Reranker:
    fallback = TokenOverlapReranker(tokenizer)
    if provider == "token_overlap":
        return fallback
    if provider == "cross_encoder_api":
        if settings is None:
            raise ValueError("cross_encoder_api requires application settings")
        return CrossEncoderApiReranker(settings, fallback)
    raise ValueError(f"未知重排器：{provider}")


def _query_aware_excerpt(query: str, text: str, max_chars: int, tokenizer) -> str:
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    query_tokens = set(tokenizer(query))
    step = max(max_chars // 2, 1)
    starts = list(range(0, max(len(text) - max_chars + 1, 1), step))
    starts.append(max(len(text) - max_chars, 0))

    def score(start: int) -> tuple[int, int]:
        window_tokens = set(tokenizer(text[start : start + max_chars]))
        return len(query_tokens & window_tokens), -start

    best_start = max(dict.fromkeys(starts), key=score)
    if best_start <= 0:
        return head
    if best_start < max_chars:
        # Best window overlaps the head: extend the head contiguously instead of duplicating.
        return text[: best_start + max_chars]
    # Keep the head plus the query-relevant window so a long chunk cannot hide
    # its answer outside the single best window.
    return head + text[best_start : best_start + max_chars]
