from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.retrieval import tokenize
from app.models import DocumentAccessGroup, DocumentKnowledgeBase, KnowledgeBase, KnowledgeDocument


class KnowledgeBaseService:
    def create(
        self,
        db: Session,
        tenant_id: str,
        user_id: str,
        name: str,
        description: str | None,
        routing_keywords: list[str],
    ) -> KnowledgeBase:
        knowledge_base = KnowledgeBase(
            tenant_id=tenant_id,
            name=name.strip(),
            description=(description or "").strip() or None,
            routing_keywords=list(
                dict.fromkeys(item.strip() for item in routing_keywords if item.strip())
            ),
            created_by=user_id,
        )
        db.add(knowledge_base)
        db.commit()
        db.refresh(knowledge_base)
        return knowledge_base

    def list(
        self,
        db: Session,
        tenant_id: str,
        offset: int = 0,
        limit: int = 100,
        include_disabled: bool = False,
        visible_to_groups: list[str] | None = None,
    ) -> list[KnowledgeBase]:
        statement = select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)
        if not include_disabled:
            statement = statement.where(KnowledgeBase.enabled.is_(True))
        if visible_to_groups is not None:
            # Regular employees (no document.manage permission) should only see knowledge
            # bases that actually contain something they're allowed to read — otherwise the
            # scope filter on the chat homepage would still list a knowledge base whose every
            # document is access-group-restricted away from them, defeating the point of the
            # restriction. A document counts as visible if it has no ACL entries at all (open
            # to the whole tenant) or at least one ACL entry matching the caller's groups —
            # same "any overlap" rule the retrieval layer already uses.
            visible_kb_ids = (
                select(DocumentKnowledgeBase.knowledge_base_id)
                .join(KnowledgeDocument, KnowledgeDocument.id == DocumentKnowledgeBase.document_id)
                .outerjoin(
                    DocumentAccessGroup, DocumentAccessGroup.document_id == KnowledgeDocument.id
                )
                .where(
                    KnowledgeDocument.tenant_id == tenant_id,
                    KnowledgeDocument.status == "ready",
                    or_(
                        DocumentAccessGroup.id.is_(None),
                        DocumentAccessGroup.group_name.in_(visible_to_groups),
                    ),
                )
            )
            statement = statement.where(KnowledgeBase.id.in_(visible_kb_ids))
        return list(db.scalars(statement.order_by(KnowledgeBase.name).offset(offset).limit(limit)))

    def update(
        self,
        db: Session,
        knowledge_base_id: str,
        tenant_id: str,
        name: str | None,
        description: str | None,
        routing_keywords: list[str] | None,
        enabled: bool | None,
        changed_fields: set[str] | None = None,
    ) -> KnowledgeBase | None:
        knowledge_base = db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.tenant_id == tenant_id,
            )
        )
        if not knowledge_base:
            return None
        if name is not None:
            knowledge_base.name = name.strip()
        if description is not None or (changed_fields and "description" in changed_fields):
            knowledge_base.description = (description or "").strip() or None
        if routing_keywords is not None:
            knowledge_base.routing_keywords = list(
                dict.fromkeys(item.strip() for item in routing_keywords if item.strip())
            )
        if enabled is not None:
            knowledge_base.enabled = enabled
        db.commit()
        db.refresh(knowledge_base)
        return knowledge_base

    def delete(self, db: Session, knowledge_base_id: str, tenant_id: str) -> bool:
        knowledge_base = db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.tenant_id == tenant_id,
            )
        )
        if not knowledge_base:
            return False
        assigned = db.scalar(
            select(DocumentKnowledgeBase.id).where(
                DocumentKnowledgeBase.knowledge_base_id == knowledge_base.id
            )
        )
        if assigned:
            raise ValueError("知识库仍有关联文档，请先重新分配文档")
        db.delete(knowledge_base)
        db.commit()
        return True

    def validate_ids(self, db: Session, tenant_id: str, ids: list[str]) -> list[str]:
        if not ids:
            return []
        valid = set(
            db.scalars(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.id.in_(ids),
                    KnowledgeBase.enabled.is_(True),
                )
            )
        )
        if valid != set(ids):
            raise ValueError("包含不存在或无权访问的知识库")
        return list(dict.fromkeys(ids))

    def route(self, db: Session, tenant_id: str, query: str) -> list[str]:
        bases = self.list(db, tenant_id, limit=1000)
        if not bases:
            return []
        query_tokens = set(tokenize(query))
        ranked = []
        for base in bases:
            routing_text = " ".join([base.name, base.description or "", *base.routing_keywords])
            score = len(query_tokens & set(tokenize(routing_text)))
            ranked.append((base.id, score))
        matched = [
            base_id
            for base_id, score in sorted(ranked, key=lambda item: item[1], reverse=True)
            if score > 0
        ]
        return matched[:3] if matched else [base.id for base in bases]
