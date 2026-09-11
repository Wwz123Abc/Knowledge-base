from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import DbSession
from app.models import AuditLog
from app.rbac import AuditViewer
from app.schemas import AuditOut

router = APIRouter()


@router.get("/audit", response_model=list[AuditOut])
def list_audit_logs(db: DbSession, auth: AuditViewer, limit: int = 100, offset: int = 0):
    safe_limit = min(max(limit, 1), 500)
    return list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == auth.tenant_id)
            .order_by(AuditLog.created_at.desc())
            .offset(max(offset, 0))
            .limit(safe_limit)
        )
    )
