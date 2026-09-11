from typing import Any

from sqlalchemy.orm import Session

from app.auth import AuthContext
from app.models import AuditLog
from app.request_context import current_request_id


def write_audit(
    db: Session,
    auth: AuthContext,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id or current_request_id(),
        details=details or {},
    )
    db.add(entry)
    return entry
