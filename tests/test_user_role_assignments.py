from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.rbac import RoleService, ensure_default_roles, role_names_assigned_to
from app.wecom import resolve_roles


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ensure_default_roles(session)
        yield session


def test_role_names_assigned_to_reflects_assignments(db):
    service = RoleService()
    role = service.create_role(db, "tenant-a", "内容审核员", "审核文档")

    assert role_names_assigned_to(db, "tenant-a", "alice") == []

    assignment = service.assign_user_role(db, "tenant-a", "alice", role.id)
    assert role_names_assigned_to(db, "tenant-a", "alice") == ["内容审核员"]
    # Scoped correctly: a different tenant or user sees nothing.
    assert role_names_assigned_to(db, "tenant-b", "alice") == []
    assert role_names_assigned_to(db, "tenant-a", "bob") == []

    service.remove_user_role_assignment(db, "tenant-a", assignment.id)
    assert role_names_assigned_to(db, "tenant-a", "alice") == []


def test_assign_user_role_rejects_super_admin(db):
    service = RoleService()
    roles = {role.name: role for role in service.list_roles(db, "tenant-a")}
    with pytest.raises(ValueError):
        service.assign_user_role(db, "tenant-a", "alice", roles["super_admin"].id)


def test_assign_user_role_rejects_duplicate(db):
    service = RoleService()
    role = service.create_role(db, "tenant-a", "内容审核员", None)
    service.assign_user_role(db, "tenant-a", "alice", role.id)
    with pytest.raises(ValueError):
        service.assign_user_role(db, "tenant-a", "alice", role.id)


def test_assign_user_role_rejects_unknown_role(db):
    service = RoleService()
    with pytest.raises(LookupError):
        service.assign_user_role(db, "tenant-a", "alice", "not-a-real-id")


def test_remove_user_role_assignment_is_tenant_scoped(db):
    service = RoleService()
    role = service.create_role(db, "tenant-a", "内容审核员", None)
    assignment = service.assign_user_role(db, "tenant-a", "alice", role.id)
    with pytest.raises(LookupError):
        service.remove_user_role_assignment(db, "tenant-b", assignment.id)


def test_resolve_roles_merges_db_assignments_and_protects_super_admin():
    settings = Settings(
        wecom_corp_id="c",
        wecom_agent_id="1",
        wecom_secret="s",
        wecom_session_secret="secret",
        wecom_super_admin_userids="root",
        wecom_admin_userids="alice",
    )
    assert resolve_roles(settings, "alice", db_role_names=["内容审核员"]) == [
        "user",
        "admin",
        "内容审核员",
    ]
    assert resolve_roles(settings, "bob", db_role_names=["内容审核员"]) == ["user", "内容审核员"]
    # A db assignment can never smuggle in super_admin for a non-allowlisted user — this
    # shouldn't be reachable via the API (assign_user_role refuses it), but resolve_roles
    # guards it independently anyway since this is where it would actually matter.
    assert resolve_roles(settings, "root", db_role_names=["super_admin"]) == ["super_admin"]
    assert resolve_roles(settings, "eve", db_role_names=["super_admin"]) == ["user"]
