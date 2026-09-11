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


def format_context(documents: list[Document]) -> str:
    parts = []
    for index, document in enumerate(documents, start=1):
        title = document.metadata.get("title", "未命名文档")
        page = document.metadata.get("page_number")
        locator = f"，第 {page} 页" if page else ""
        safe_title = _escape_attribute(f"{title}{locator}")
        parts.append(
            f'<knowledge_document index="{index}" title="{safe_title}">\n'
            f"{document.page_content}\n"
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


_EXCERPT_BOUNDARY_MARKS = "。！？；\n"


def _excerpt(content: str, limit: int = 220) -> str:
    if len(content) <= limit:
        return content
    window = content[:limit]
    # Prefer cutting at the last sentence-ending punctuation near the limit so
    # excerpts don't end mid-word; only fall back to a hard cut (with an
    # ellipsis to signal it's truncated) when no boundary is close enough.
    boundary = max((window.rfind(mark) for mark in _EXCERPT_BOUNDARY_MARKS), default=-1)
    if boundary >= limit - 60:
        return window[: boundary + 1]
    return window.rstrip() + "…"


def citation(document: Document) -> Citation:
    return Citation(
        document_id=str(document.metadata.get("document_id", "")),
        title=str(document.metadata.get("title", "未命名文档")),
        chunk_id=str(document.metadata.get("chunk_id", "")),
        page_number=document.metadata.get("page_number"),
        excerpt=_excerpt(document.page_content),
    )


def groundedness(answer: str, documents: list[Document]) -> float:
    answer_tokens = set(tokenize(answer))
    context_tokens = set(tokenize(" ".join(document.page_content for document in documents)))
    return len(answer_tokens & context_tokens) / max(len(answer_tokens), 1)


def is_insufficient_answer(answer: str) -> bool:
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
            for match in re.finditer(r"\[(\d+)\]", answer)
            if not 1 <= int(match.group(1)) <= document_count
        }
    )
    if not invalid:
        return answer, []
    invalid_set = set(invalid)
    cleaned = re.sub(
        r"\[(\d+)\]",
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
