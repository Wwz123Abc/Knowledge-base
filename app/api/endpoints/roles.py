from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import DbSession
from app.audit import write_audit
from app.auth import CurrentUser
from app.rbac import RoleManager, RoleService
from app.schemas import (
    PermissionOut,
    RoleCreate,
    RoleOut,
    RolePermissionsUpdate,
    RoleUpdate,
    UserRoleAssignmentCreate,
    UserRoleAssignmentOut,
)

router = APIRouter()


@router.get("/permissions", response_model=list[PermissionOut])
def list_permissions(db: DbSession, auth: CurrentUser):
    return RoleService().list_permissions(db)


@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: DbSession, auth: CurrentUser):
    return RoleService().list_roles(db, auth.tenant_id)


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(payload: RoleCreate, db: DbSession, auth: RoleManager):
    try:
        role = RoleService().create_role(db, auth.tenant_id, payload.name, payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(db, auth, "role.create", "role", role.id, {"name": role.name})
    db.commit()
    return role


@router.patch("/roles/{role_id}", response_model=RoleOut)
def update_role(role_id: str, payload: RoleUpdate, db: DbSession, auth: RoleManager):
    try:
        role = RoleService().update_role(db, role_id, auth.tenant_id, payload.description)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(db, auth, "role.update", "role", role.id)
    db.commit()
    return role


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(role_id: str, db: DbSession, auth: RoleManager):
    try:
        RoleService().delete_role(db, role_id, auth.tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(db, auth, "role.delete", "role", role_id)
    db.commit()


@router.put("/roles/{role_id}/permissions", response_model=RoleOut)
def set_role_permissions(
    role_id: str, payload: RolePermissionsUpdate, db: DbSession, auth: RoleManager
):
    try:
        role = RoleService().set_role_permissions(
            db, role_id, auth.tenant_id, payload.permission_codes
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(
        db, auth, "role.permissions.update", "role", role.id, {"codes": role.permission_codes}
    )
    db.commit()
    return role


@router.get("/user-roles", response_model=list[UserRoleAssignmentOut])
def list_user_role_assignments(db: DbSession, auth: RoleManager):
    return RoleService().list_user_role_assignments(db, auth.tenant_id)


@router.post(
    "/user-roles", response_model=UserRoleAssignmentOut, status_code=status.HTTP_201_CREATED
)
def assign_user_role(payload: UserRoleAssignmentCreate, db: DbSession, auth: RoleManager):
    try:
        assignment = RoleService().assign_user_role(
            db, auth.tenant_id, payload.user_id, payload.role_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit(
        db,
        auth,
        "user_role.assign",
        "user_role_assignment",
        assignment.id,
        {"user_id": assignment.user_id, "role_id": assignment.role_id},
    )
    db.commit()
    return assignment


@router.delete("/user-roles/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user_role_assignment(assignment_id: str, db: DbSession, auth: RoleManager):
    try:
        RoleService().remove_user_role_assignment(db, auth.tenant_id, assignment_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    write_audit(db, auth, "user_role.remove", "user_role_assignment", assignment_id)
    db.commit()
