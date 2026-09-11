from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import AuthContext, get_auth_context
from app.main import app


def _override_auth(roles: tuple[str, ...], tenant_id: str = "default"):
    def _dependency() -> AuthContext:
        return AuthContext(
            user_id="rbac-test-user",
            display_name="RBAC 测试用户",
            tenant_id=tenant_id,
            groups=(),
            roles=roles,
        )

    return _dependency


def test_default_admin_role_can_manage_knowledge_bases_but_not_roles():
    # Pin the identity explicitly instead of relying on the default dev auth context —
    # that reads DEV_ROLES from the environment, and a local .env with
    # DEV_ROLES=admin,user,super_admin (a real setup used elsewhere in this project for
    # local admin bootstrapping) silently gives the default identity super_admin, which
    # bypasses the 403 this test exists to check. Every other test in this file already
    # overrides the identity for exactly this reason.
    app.dependency_overrides[get_auth_context] = _override_auth(roles=("admin",))
    kb_name = f"RBAC 测试知识库 {uuid4().hex[:8]}"
    try:
        with TestClient(app) as client:
            created = client.post("/api/knowledge-bases", json={"name": kb_name})
            assert created.status_code == 201
            knowledge_base_id = created.json()["id"]
            try:
                denied = client.post("/api/roles", json={"name": "rbac-test-role"})
                assert denied.status_code == 403
            finally:
                deleted = client.delete(f"/api/knowledge-bases/{knowledge_base_id}")
                assert deleted.status_code == 204
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


def test_permission_denied_without_matching_role():
    app.dependency_overrides[get_auth_context] = _override_auth(roles=("member",))
    try:
        with TestClient(app) as client:
            response = client.get("/api/audit")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_super_admin_bypasses_permission_checks_even_with_no_granted_role_rows():
    app.dependency_overrides[get_auth_context] = _override_auth(roles=("super_admin",))
    try:
        role_name = f"rbac-test-{uuid4().hex[:8]}"
        with TestClient(app) as client:
            created = client.post("/api/roles", json={"name": role_name})
            assert created.status_code == 201
            role_id = created.json()["id"]

            permissions = client.get("/api/permissions")
            assert permissions.status_code == 200
            codes = {item["code"] for item in permissions.json()}
            assert "document.manage" in codes
            assert "role.manage" in codes

            granted = client.put(
                f"/api/roles/{role_id}/permissions",
                json={"permission_codes": ["audit.view"]},
            )
            assert granted.status_code == 200
            assert granted.json()["permission_codes"] == ["audit.view"]

            deleted = client.delete(f"/api/roles/{role_id}")
            assert deleted.status_code == 204
    finally:
        app.dependency_overrides.clear()


def test_system_roles_cannot_be_renamed_deleted_or_impersonated():
    app.dependency_overrides[get_auth_context] = _override_auth(roles=("super_admin",))
    try:
        with TestClient(app) as client:
            roles = client.get("/api/roles").json()
            admin_role = next(role for role in roles if role["name"] == "admin")
            assert admin_role["is_system"] is True
            assert set(admin_role["permission_codes"]) == {
                "document.manage",
                "knowledge_base.manage",
                "connector.manage",
                "tool.approve",
                "audit.view",
                "system.admin",
            }

            blocked_update = client.patch(
                f"/api/roles/{admin_role['id']}", json={"description": "改一下"}
            )
            assert blocked_update.status_code == 409

            blocked_delete = client.delete(f"/api/roles/{admin_role['id']}")
            assert blocked_delete.status_code == 409

            blocked_create = client.post("/api/roles", json={"name": "super_admin"})
            assert blocked_create.status_code == 409
    finally:
        app.dependency_overrides.clear()
