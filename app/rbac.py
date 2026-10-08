from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.dependencies import DbSession
from app.auth import AuthContext, CurrentUser
from app.models import Permission, Role, RolePermission, UserRoleAssignment

# The super-admin role bypasses every permission check outright, rather than being granted
# every permission as data. That's a deliberate hard protection (business requirement 5.2):
# even if someone deletes or edits the role_permissions rows for "super_admin", the bypass
# still holds, because it never depended on those rows in the first place.
SUPER_ADMIN_ROLE = "super_admin"

PERMISSIONS: list[tuple[str, str, str]] = [
    # (code, description, category)
    ("document.manage", "创建、更新、删除文档及其版本，管理入库任务", "文档"),
    ("knowledge_base.manage", "创建、更新、删除逻辑知识库", "知识库"),
    ("connector.manage", "管理外部连接器并触发同步", "连接器"),
    ("tool.approve", "审批高风险工具调用", "工具"),
    ("audit.view", "查看审计日志", "审计"),
    ("system.admin", "执行系统维护操作（向量索引重建、评测失败清单等）", "系统"),
    ("role.manage", "创建、修改、删除角色及其权限分配", "权限"),
]

# System roles are seeded on every startup/migration and cannot be edited or deleted through
# the API (see RoleService below) — they're the floor every tenant gets for free. The "admin"
# role's permission set intentionally matches what the old flat is_admin check used to grant,
# so existing deployments whose IdP issues an "admin" role claim keep working unchanged.
SYSTEM_ROLES: dict[str, tuple[str, list[str]]] = {
    SUPER_ADMIN_ROLE: (
        "拥有全部功能权限，权限校验对其直接放行",
        [code for code, _, _ in PERMISSIONS],
    ),
    "admin": (
        "租户管理员，覆盖文档、知识库、连接器、工具审批、审计与系统维护",
        [
            "document.manage",
            "knowledge_base.manage",
            "connector.manage",
            "tool.approve",
            "audit.view",
            "system.admin",
        ],
    ),
    "auditor": ("只读查看审计日志", ["audit.view"]),
}


def ensure_default_roles(db: Session) -> None:
    """Idempotently seed permissions and system roles. Called from app.db.init_db() for the
    dev/sqlite bootstrap path and from the RBAC Alembic migration for production Postgres."""
    existing_permissions = set(db.scalars(select(Permission.code)))
    for code, description, category in PERMISSIONS:
        if code not in existing_permissions:
            db.add(Permission(code=code, description=description, category=category))
    db.flush()

    for name, (description, codes) in SYSTEM_ROLES.items():
        role = db.scalar(select(Role).where(Role.tenant_id.is_(None), Role.name == name))
        if role is None:
            role = Role(tenant_id=None, name=name, description=description, is_system=True)
            db.add(role)
            db.flush()
        else:
            role.description = description
        current_codes = set(
            db.scalars(
                select(RolePermission.permission_code).where(RolePermission.role_id == role.id)
            )
        )
        desired_codes = set(codes)
        for code in desired_codes - current_codes:
            db.add(RolePermission(role_id=role.id, permission_code=code))
        stale_codes = current_codes - desired_codes
        if stale_codes:
            db.execute(
                delete(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_code.in_(stale_codes),
                )
            )
        db.flush()
    db.commit()


def role_names_assigned_to(db: Session, tenant_id: str, user_id: str) -> list[str]:
    """Roles granted to an external identity (e.g. a WeCom userid) via UserRoleAssignment —
    the database-backed alternative to the WECOM_ADMIN_USERIDS/WECOM_SUPER_ADMIN_USERIDS env
    var allowlists, so a super_admin can grant custom roles through the UI instead of asking
    for a server config change every time. Only consulted at WeCom login (see app/wecom.py)."""
    return list(
        db.scalars(
            select(Role.name)
            .join(UserRoleAssignment, UserRoleAssignment.role_id == Role.id)
            .where(UserRoleAssignment.tenant_id == tenant_id, UserRoleAssignment.user_id == user_id)
        )
    )


def permission_codes_for(db: Session, tenant_id: str, roles: tuple[str, ...]) -> set[str]:
    if not roles:
        return set()
    rows = db.scalars(
        select(RolePermission.permission_code)
        .join(Role, Role.id == RolePermission.role_id)
        .where(
            Role.name.in_(roles),
            or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id),
        )
    )
    return set(rows)


def can_manage_documents(db: Session, auth: AuthContext) -> bool:
    return SUPER_ADMIN_ROLE in auth.roles or "document.manage" in permission_codes_for(
        db, auth.tenant_id, auth.roles
    )


def require_permission(code: str):
    def _dependency(db: DbSession, auth: CurrentUser) -> AuthContext:
        if SUPER_ADMIN_ROLE in auth.roles:
            return auth
        if code not in permission_codes_for(db, auth.tenant_id, auth.roles):
            raise HTTPException(status_code=403, detail=f"缺少权限：{code}")
        return auth

    return _dependency


DocumentManager = Annotated[AuthContext, Depends(require_permission("document.manage"))]
KnowledgeBaseManager = Annotated[AuthContext, Depends(require_permission("knowledge_base.manage"))]
ConnectorManager = Annotated[AuthContext, Depends(require_permission("connector.manage"))]
ToolApprover = Annotated[AuthContext, Depends(require_permission("tool.approve"))]
AuditViewer = Annotated[AuthContext, Depends(require_permission("audit.view"))]
SystemAdmin = Annotated[AuthContext, Depends(require_permission("system.admin"))]
RoleManager = Annotated[AuthContext, Depends(require_permission("role.manage"))]


class RoleService:
    def list_permissions(self, db: Session) -> list[Permission]:
        return list(db.scalars(select(Permission).order_by(Permission.category, Permission.code)))

    def list_roles(self, db: Session, tenant_id: str) -> list[Role]:
        return list(
            db.scalars(
                select(Role)
                .where(or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id))
                .order_by(Role.is_system.desc(), Role.name)
            )
        )

    def create_role(self, db: Session, tenant_id: str, name: str, description: str | None) -> Role:
        name = name.strip()
        if not name:
            raise ValueError("角色名称不能为空")
        if name == SUPER_ADMIN_ROLE or name in SYSTEM_ROLES:
            raise ValueError("该名称为系统角色保留名称，不能用于自定义角色")
        role = Role(tenant_id=tenant_id, name=name, description=description, is_system=False)
        db.add(role)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("角色名称已存在") from exc
        db.refresh(role)
        return role

    def _get_editable_role(self, db: Session, role_id: str, tenant_id: str) -> Role:
        # System roles (tenant_id IS NULL) must still resolve here, even though they're
        # never actually editable — otherwise "edit a system role" 404s ("not found")
        # instead of 409 ("not allowed"), which would leak nothing but is the wrong signal.
        role = db.scalar(
            select(Role).where(
                Role.id == role_id,
                or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id),
            )
        )
        if role is None:
            raise LookupError("角色不存在")
        if role.is_system:
            raise ValueError("系统角色不可修改或删除")
        return role

    def update_role(
        self, db: Session, role_id: str, tenant_id: str, description: str | None
    ) -> Role:
        role = self._get_editable_role(db, role_id, tenant_id)
        role.description = description
        db.commit()
        db.refresh(role)
        return role

    def delete_role(self, db: Session, role_id: str, tenant_id: str) -> None:
        role = self._get_editable_role(db, role_id, tenant_id)
        db.delete(role)
        db.commit()

    def set_role_permissions(
        self, db: Session, role_id: str, tenant_id: str, codes: list[str]
    ) -> Role:
        role = self._get_editable_role(db, role_id, tenant_id)
        valid_codes = set(db.scalars(select(Permission.code)))
        requested = set(codes)
        if not requested.issubset(valid_codes):
            raise ValueError(f"包含未知权限：{sorted(requested - valid_codes)}")
        role.permissions.clear()
        db.flush()
        for code in requested:
            db.add(RolePermission(role_id=role.id, permission_code=code))
        db.commit()
        db.refresh(role)
        return role

    def list_user_role_assignments(self, db: Session, tenant_id: str) -> list[UserRoleAssignment]:
        return list(
            db.scalars(
                select(UserRoleAssignment)
                .options(selectinload(UserRoleAssignment.role))
                .where(UserRoleAssignment.tenant_id == tenant_id)
                .order_by(UserRoleAssignment.user_id)
            )
        )

    def assign_user_role(
        self, db: Session, tenant_id: str, user_id: str, role_id: str
    ) -> UserRoleAssignment:
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("账号不能为空")
        role = db.scalar(
            select(Role).where(
                Role.id == role_id, or_(Role.tenant_id.is_(None), Role.tenant_id == tenant_id)
            )
        )
        if role is None:
            raise LookupError("角色不存在")
        if role.name == SUPER_ADMIN_ROLE:
            raise ValueError("super_admin 只能通过服务器配置授予，不能在页面里分配")
        assignment = UserRoleAssignment(tenant_id=tenant_id, user_id=user_id, role_id=role_id)
        db.add(assignment)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("该账号已经拥有这个角色") from exc
        db.refresh(assignment)
        return assignment

    def remove_user_role_assignment(self, db: Session, tenant_id: str, assignment_id: str) -> None:
        assignment = db.scalar(
            select(UserRoleAssignment).where(
                UserRoleAssignment.id == assignment_id, UserRoleAssignment.tenant_id == tenant_id
            )
        )
        if assignment is None:
            raise LookupError("分配记录不存在")
        db.delete(assignment)
        db.commit()
