from __future__ import annotations

import re

from langchain_core.documents import Document

from app.config import Settings
from app.core.retrieval import tokenize
from app.costs import estimate_cost, normalize_usage
from app.schemas import Citation


def _escape_attribute(value: str) -> str:
    # Document titles are uploader-controlled; escape before they land inside a
    # quoted XML-style attribute so a title can't break out of the tag and
    # inject fake instructions into the prompt context.
    return (
        value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


_CONTEXT_TAG = re.compile(r"<(/?\s*knowledge_document)", re.IGNORECASE)


def _neutralize_context_tags(content: str) -> str:
    # Uploaded text is untrusted: a literal </knowledge_document> in it would close the
    # wrapper early and let the rest read as instructions outside the quoted material.
    return _CONTEXT_TAG.sub(r"&lt;\1", content)


def format_context(documents: list[Document]) -> str:
    parts = []
    for index, document in enumerate(documents, start=1):
        title = document.metadata.get("title", "未命名文档")
        page = document.metadata.get("page_number")
        locator = f"，第 {page} 页" if page else ""
        safe_title = _escape_attribute(f"{title}{locator}")
        parts.append(
            f'<knowledge_document index="{index}" title="{safe_title}">\n'
            f"{_neutralize_context_tags(document.page_content)}\n"
            "</knowledge_document>"
        )
    return "\n\n".join(parts)


def format_history(history: list[dict[str, str]]) -> str:
    lines = []
    for item in history[-6:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = item.get("content", "").strip()[:1000]
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


_EXCERPT_BOUNDARY = re.compile(r"[。！？；\n]")
# Chunks are cut at sentence marks and keep the mark at the start of the next chunk, so the
# stored text often opens with a stray "。" or "；" that makes an excerpt look broken.
_EXCERPT_LEADING_JUNK = "。！？；，、：:）) \t\r\n"


def _query_terms(query: str) -> set[str]:
    terms = {word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]+", query)}
    for run in re.findall(r"[一-鿿]+", query):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _excerpt(content: str, limit: int = 220, query: str = "") -> str:
    """The part of a retrieved chunk to show as evidence.

    The head of the chunk is rarely the relevant part: a 700-character chunk about leave rules
    answered "how many days off for Spring Festival" with its first 220 characters — which
    ended just before the sentence that says so. Pick the stretch (starting at a sentence
    boundary) that contains the most terms from the question instead.
    """
    text = content.strip().lstrip(_EXCERPT_LEADING_JUNK)
    if len(text) <= limit:
        return text
    starts = [0] + [m.end() for m in _EXCERPT_BOUNDARY.finditer(text) if m.end() < len(text)]
    terms = _query_terms(query)
    best = 0
    if terms:

        def hits(start: int) -> int:
            window = text[start : start + limit].lower()
            return sum(1 for term in terms if term in window)

        top = max(hits(start) for start in starts)
        if top:
            # Among the windows that contain the most terms, take the latest start, so the
            # matching sentence sits near the top of the excerpt rather than at its far edge.
            best = max(start for start in starts if hits(start) == top)
    window = text[best : best + limit]
    prefix = "…" if best else ""
    if best + limit >= len(text):
        return prefix + window.lstrip(_EXCERPT_LEADING_JUNK)
    boundary = max((window.rfind(mark) for mark in "。！？；\n"), default=-1)
    if boundary >= limit - 60:
        return prefix + window[: boundary + 1].lstrip(_EXCERPT_LEADING_JUNK)
    return prefix + window.rstrip() + "…"


def citation(document: Document, query: str = "") -> Citation:
    return Citation(
        document_id=str(document.metadata.get("document_id", "")),
        title=str(document.metadata.get("title", "未命名文档")),
        chunk_id=str(document.metadata.get("chunk_id", "")),
        page_number=document.metadata.get("page_number"),
        excerpt=_excerpt(document.page_content, query=query),
    )


def groundedness(answer: str, documents: list[Document]) -> float:
    answer_tokens = set(tokenize(answer))
    context_tokens = set(tokenize(" ".join(document.page_content for document in documents)))
    return len(answer_tokens & context_tokens) / max(len(answer_tokens), 1)


_CITATION_MARK = re.compile(r"\[\d{1,2}\]")


def is_insufficient_answer(answer: str) -> bool:
    # An answer that cites its sources is grounded, even when it adds that part of the
    # question isn't covered — system-prompt rule 6 tells the model to write exactly that
    # ("资料未提及"). Treating it as a refusal threw the grounded part away and replaced it
    # with an ungrounded fallback answer.
    if _CITATION_MARK.search(answer):
        return False
    normalized = "".join(answer.split())
    markers = (
        "知识库中没有找到足够依据",
        "没有找到足够依据",
        "资料中未提及",
        "知识库中未提及",
        "知识库中没有找到关于",
        "知识库未提供",
        "无法回答",
    )
    return any(marker in normalized for marker in markers)


def validate_citation_indices(answer: str, document_count: int) -> tuple[str, list[int]]:
    invalid = sorted(
        {
            int(match.group(1))
            for match in re.finditer(r"\[(\d{1,2})\]", answer)
            if not 1 <= int(match.group(1)) <= document_count
        }
    )
    if not invalid:
        return answer, []
    invalid_set = set(invalid)
    cleaned = re.sub(
        r"\[(\d{1,2})\]",
        lambda match: "" if int(match.group(1)) in invalid_set else match.group(0),
        answer,
    )
    return cleaned, invalid


def stream_usage(chunk, current: dict[str, int], current_model: str) -> tuple[dict[str, int], str]:
    response_metadata = getattr(chunk, "response_metadata", {}) or {}
    raw_usage = getattr(chunk, "usage_metadata", None) or response_metadata.get("token_usage", {})
    normalized = normalize_usage(raw_usage)
    merged = {key: max(int(current.get(key, 0)), int(value)) for key, value in normalized.items()}
    model = str(response_metadata.get("model_name") or current_model)
    return merged, model


def usage_record(settings: Settings, model: str, usage: dict) -> dict:
    normalized = normalize_usage(usage)
    return {
        **normalized,
        "model": model,
        "estimated_cost_usd": estimate_cost(settings, model, normalized),
    }
