from fastapi import APIRouter

from app.api.dependencies import DbSession, SettingsDep
from app.auth import CurrentUser
from app.health import probe_dependencies
from app.rbac import PERMISSIONS, SUPER_ADMIN_ROLE, permission_codes_for
from app.schemas import AuthOut, HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(db: DbSession, settings: SettingsDep):
    dependencies = probe_dependencies(db, settings)
    return HealthResponse(
        **dependencies,
        vector_backend=settings.vector_backend,
        model_ready=settings.model_ready,
    )


@router.get("/me", response_model=AuthOut)
def current_identity(auth: CurrentUser, db: DbSession):
    if SUPER_ADMIN_ROLE in auth.roles:
        permissions = [code for code, _, _ in PERMISSIONS]
    else:
        permissions = sorted(permission_codes_for(db, auth.tenant_id, auth.roles))
    return AuthOut(
        user_id=auth.user_id,
        display_name=auth.display_name,
        tenant_id=auth.tenant_id,
        groups=list(auth.groups),
        roles=list(auth.roles),
        permissions=permissions,
    )
