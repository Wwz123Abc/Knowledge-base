"""Targeted retrieval helpers for statute/article-style queries (第N条 / 共几条)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import DocumentAccessGroup, KnowledgeChunk, KnowledgeDocument

_CN_DIGITS = "零一二三四五六七八九"
_ARTICLE_RE = re.compile(r"第([0-9]+|[一二三四五六七八九十百千]+)条")
_COUNT_RE = re.compile(r"(一共|总共|共有|合计|多少条|几条|总条数|多少条)")
_MAX_SCAN_CHUNKS = 800


def cn_to_int(text: str) -> int:
    """Convert Chinese numerals to int: 三百 -> 300, 一千二百六十 -> 1260."""
    total = 0
    section = 0
    num = 0
    for char in text:
        if char in _CN_DIGITS:
            num = _CN_DIGITS.index(char)
        elif char == "十":
            if num == 0:
                section = (section or 1) * 10
            else:
                section += num * 10
                num = 0
        elif char == "百":
            section += num * 100
            num = 0
        elif char == "千":
            section += num * 1000
            num = 0
        else:
            break
    return total + section + num


def int_to_cn(number: int) -> str:
    """Convert int to Chinese numerals: 300 -> 三百, 1260 -> 一千二百六十, 1050 -> 一千零五十."""
    if number == 0:
        return "零"
    if number < 0 or number > 9999:
        return str(number)
    digits = [number // 1000, number // 100 % 10, number // 10 % 10, number % 10]
    units = ("千", "百", "十", "")
    parts: list[str] = []
    started = False
    zero_pending = False
    for index, (digit, unit) in enumerate(zip(digits, units, strict=True)):
        if digit == 0:
            if started and index < 3:
                zero_pending = True
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        if unit == "十" and digit == 1 and not started:
            parts.append("十")
        else:
            parts.append(f"{_CN_DIGITS[digit]}{unit}")
        started = True
    return "".join(parts)


def article_phrases(query: str) -> list[str]:
    """Article numbers named in the query, in both numeral forms.

    "第300条" -> ["第300条", "第三百条"]; "第三百条" -> ["第三百条", "第300条"].
    """
    phrases: list[str] = []
    for match in _ARTICLE_RE.finditer(query):
        raw = match.group(1)
        candidates = {f"第{raw}条"}
        if raw.isdigit():
            candidates.add(f"第{int_to_cn(int(raw))}条")
        else:
            value = cn_to_int(raw)
            if value > 0:
                candidates.add(f"第{value}条")
        phrases.extend(sorted(candidates, key=len))
    return list(dict.fromkeys(phrases))


def is_count_query(query: str) -> bool:
    """Whether the query asks for a total article count (共几条/多少条 ...)."""
    return bool(_COUNT_RE.search(query))


def is_effective_document(document: KnowledgeDocument) -> bool:
    """Whether a document's status and latest version are currently servable."""
    if document.status != "ready":
        return False
    if not document.versions:
        return True
    version = max(document.versions, key=lambda item: item.version_number)
    now = datetime.now(UTC)
    valid_from = version.valid_from
    valid_until = version.valid_until
    if valid_from and valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=UTC)
    if valid_until and valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=UTC)
    return (
        version.status == "published"
        and (not valid_from or valid_from <= now)
        and (not valid_until or valid_until > now)
    )


def find_article_chunks(
    db: Session,
    tenant_id: str,
    phrases: Sequence[str],
    user_groups: Sequence[str],
    limit: int = 6,
) -> list[KnowledgeChunk]:
    """Exact-phrase lookup for 第N条 across authorized, effective documents."""
    if not phrases:
        return []
    conditions = [KnowledgeChunk.content.ilike(f"%{phrase}%", escape="\\") for phrase in phrases]
    acl_exists = exists(
        select(DocumentAccessGroup.id).where(
            DocumentAccessGroup.document_id == KnowledgeDocument.id
        )
    )
    if user_groups:
        acl_allowed = or_(
            ~acl_exists,
            exists(
                select(DocumentAccessGroup.id).where(
                    DocumentAccessGroup.document_id == KnowledgeDocument.id,
                    DocumentAccessGroup.group_name.in_(list(user_groups)),
                )
            ),
        )
    else:
        acl_allowed = ~acl_exists
    rows = list(
        db.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeChunk.document)
            .options(
                selectinload(KnowledgeChunk.document).selectinload(KnowledgeDocument.acl_entries),
                selectinload(KnowledgeChunk.document).selectinload(KnowledgeDocument.versions),
            )
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeDocument.status == "ready",
                or_(*conditions),
                acl_allowed,
            )
            .order_by(KnowledgeChunk.position)
            .limit(limit)
        )
    )
    return [chunk for chunk in rows if is_effective_document(chunk.document)]


def find_max_article_chunk(
    db: Session, tenant_id: str, document_ids: Sequence[str], limit_scan: int = _MAX_SCAN_CHUNKS
) -> KnowledgeChunk | None:
    """Return the chunk with the numerically largest 第N条 among the given documents."""
    if not document_ids:
        return None
    chunks = list(
        db.scalars(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.document_id.in_(list(document_ids)),
            )
            .order_by(KnowledgeChunk.position)
            .limit(limit_scan)
        )
    )
    best_chunk: KnowledgeChunk | None = None
    best_number = 0
    for chunk in chunks:
        for match in _ARTICLE_RE.finditer(chunk.content):
            raw = match.group(1)
            number = int(raw) if raw.isdigit() else cn_to_int(raw)
            if number > best_number:
                best_number = number
                best_chunk = chunk
    return best_chunk
