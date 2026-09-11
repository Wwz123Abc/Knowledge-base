from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.auth import CurrentUser
from app.knowledge_bases import KnowledgeBaseService
from app.rbac import SUPER_ADMIN_ROLE, KnowledgeBaseManager, permission_codes_for
from app.schemas import KnowledgeBaseCreate, KnowledgeBaseOut, KnowledgeBaseUpdate

router = APIRouter()


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(
    db: DbSession,
    auth: CurrentUser,
    offset: int = 0,
    limit: int = 100,
    include_disabled: bool = False,
):
    # Document managers see every knowledge base regardless of their own group membership —
    # they need the full list to assign documents to any of them. Everyone else only sees
    # knowledge bases that actually contain something their groups can read, so a
    # department-restricted knowledge base doesn't show up as a selectable scope for people
    # outside that department.
    can_manage = SUPER_ADMIN_ROLE in auth.roles or "document.manage" in permission_codes_for(
        db, auth.tenant_id, auth.roles
    )
    return KnowledgeBaseService().list(
        db,
        auth.tenant_id,
        max(offset, 0),
        min(max(limit, 1), 500),
        include_disabled and auth.is_admin,
        visible_to_groups=None if can_manage else list(auth.groups),
    )


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseOut,
    status_code=status.HTTP_201_CREATED,
)
def create_knowledge_base(payload: KnowledgeBaseCreate, db: DbSession, auth: KnowledgeBaseManager):
    try:
        knowledge_base = KnowledgeBaseService().create(
            db,
            auth.tenant_id,
            auth.user_id,
            payload.name,
            payload.description,
            payload.routing_keywords,
        )
        write_audit(
            db,
            auth,
            "knowledge_base.create",
            "knowledge_base",
            knowledge_base.id,
        )
        db.commit()
        return knowledge_base
    except Exception as exc:
        raise HTTPException(status_code=409, detail="知识库名称已存在") from exc


@router.patch("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseOut)
def update_knowledge_base(
    knowledge_base_id: str,
    payload: KnowledgeBaseUpdate,
    db: DbSession,
    auth: KnowledgeBaseManager,
):
    try:
        knowledge_base = KnowledgeBaseService().update(
            db,
            knowledge_base_id,
            auth.tenant_id,
            payload.name,
            payload.description,
            payload.routing_keywords,
            payload.enabled,
            payload.model_fields_set,
        )
        if not knowledge_base:
            raise HTTPException(status_code=404, detail="知识库不存在")
        write_audit(db, auth, "knowledge_base.update", "knowledge_base", knowledge_base.id)
        db.commit()
        return knowledge_base
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail="知识库名称已存在") from exc


@router.delete("/knowledge-bases/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_base(knowledge_base_id: str, db: DbSession, auth: KnowledgeBaseManager):
    try:
        if not KnowledgeBaseService().delete(db, knowledge_base_id, auth.tenant_id):
            raise HTTPException(status_code=404, detail="知识库不存在")
        write_audit(db, auth, "knowledge_base.delete", "knowledge_base", knowledge_base_id)
        db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
