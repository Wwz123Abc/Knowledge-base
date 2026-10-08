from __future__ import annotations

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import get_auth_context
from app.config import Settings, get_settings
from app.db import Base
from app.main import app
from app.wecom import (
    WeComError,
    WeComIdentity,
    build_authorize_url,
    department_groups,
    exchange_code_for_identity,
    mint_session_token,
    mint_state_token,
    resolve_roles,
    verify_state_token,
)


def _wecom_settings(**overrides) -> Settings:
    defaults = dict(
        auth_mode="wecom",
        wecom_corp_id="corp-1",
        wecom_agent_id="1000001",
        wecom_secret="corp-secret",
        wecom_redirect_uri="https://rag.example.com/api/auth/wecom/callback",
        wecom_tenant_id="default",
        wecom_session_secret="session-secret",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_build_authorize_url_contains_required_params():
    settings = _wecom_settings()
    url = build_authorize_url(settings, "state-123")
    assert url.startswith("https://open.weixin.qq.com/connect/oauth2/authorize?")
    assert "appid=corp-1" in url
    assert "agentid=1000001" in url
    assert "state=state-123" in url
    assert "response_type=code" in url
    assert url.endswith("#wechat_redirect")


def test_build_authorize_url_requires_redirect_uri():
    settings = _wecom_settings(wecom_redirect_uri="")
    with pytest.raises(WeComError):
        build_authorize_url(settings, "state-123")


def test_resolve_roles_from_allowlists():
    settings = _wecom_settings(
        wecom_admin_userids="alice, bob",
        wecom_super_admin_userids="root",
    )
    assert resolve_roles(settings, "root") == ["super_admin"]
    assert resolve_roles(settings, "alice") == ["user", "admin"]
    assert resolve_roles(settings, "someone-else") == ["user"]


def test_department_groups_prefixes_ids():
    assert department_groups([3, 12]) == ["dept_3", "dept_12"]


def test_mint_and_verify_state_token_round_trip():
    settings = _wecom_settings()
    token = mint_state_token(settings)
    assert verify_state_token(settings, token) is True
    assert verify_state_token(settings, "garbage") is False
    # A session token minted for a different purpose must not pass as a login state.
    identity = WeComIdentity(userid="u1", display_name="U1", department_ids=[])
    session_token = mint_session_token(settings, identity)
    assert verify_state_token(settings, session_token) is False


def test_mint_session_token_round_trips_through_get_auth_context():
    settings = _wecom_settings(
        wecom_tenant_id="acme",
        wecom_admin_userids="alice",
    )
    identity = WeComIdentity(userid="alice", display_name="Alice", department_ids=[3, 12])
    token = mint_session_token(settings, identity)

    claims = jwt.decode(token, settings.wecom_session_secret, algorithms=["HS256"])
    assert claims["sub"] == "alice"
    assert claims["tenant_id"] == "acme"
    assert claims["groups"] == ["dept_3", "dept_12"]
    assert claims["roles"] == ["user", "admin"]

    credentials = httpx.Headers({"authorization": f"Bearer {token}"})
    from fastapi.security import HTTPAuthorizationCredentials

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        auth = get_auth_context(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), settings, db
        )
    assert auth.user_id == "alice"
    assert auth.tenant_id == "acme"
    assert set(auth.groups) == {"dept_3", "dept_12"}
    assert set(auth.roles) == {"admin", "user"}
    assert credentials  # keep the unused import obviously used


@pytest.mark.anyio
async def test_exchange_code_for_identity_success():
    settings = _wecom_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok", "expires_in": 7200}
            )
        if request.url.path == "/cgi-bin/user/getuserinfo":
            return httpx.Response(200, json={"errcode": 0, "UserId": "alice"})
        if request.url.path == "/cgi-bin/user/get":
            return httpx.Response(200, json={"errcode": 0, "name": "Alice", "department": [3, 12]})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        identity = await exchange_code_for_identity(settings, "code-abc", client=client)
    finally:
        await client.aclose()

    assert identity == WeComIdentity(userid="alice", display_name="Alice", department_ids=[3, 12])


@pytest.mark.anyio
async def test_exchange_code_for_identity_raises_on_wecom_error():
    settings = _wecom_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "tok", "expires_in": 7200}
            )
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WeComError):
            await exchange_code_for_identity(settings, "bad-code", client=client)
    finally:
        await client.aclose()


def test_wecom_login_endpoint_redirects_to_wecom():
    settings = _wecom_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/api/auth/wecom/login")
        assert response.status_code == 307
        location = response.headers["location"]
        assert location.startswith("https://open.weixin.qq.com/connect/oauth2/authorize?")
        assert "appid=corp-1" in location
    finally:
        app.dependency_overrides.clear()


def test_wecom_callback_rejects_invalid_state():
    settings = _wecom_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/api/auth/wecom/callback", params={"code": "x", "state": "bad"})
        assert response.status_code == 307
        assert "auth_error=invalid_state" in response.headers["location"]
    finally:
        app.dependency_overrides.clear()


def test_wecom_callback_success_sets_token_fragment(monkeypatch):
    settings = _wecom_settings()
    app.dependency_overrides[get_settings] = lambda: settings

    async def fake_exchange(_settings, _code, client=None):
        return WeComIdentity(userid="alice", display_name="Alice", department_ids=[3])

    monkeypatch.setattr("app.api.endpoints.auth_wecom.exchange_code_for_identity", fake_exchange)
    try:
        with TestClient(app, follow_redirects=False) as client:
            state = mint_state_token(settings)
            client.cookies.set("wecom_login_state", state, path="/api/auth/wecom")
            response = client.get(
                "/api/auth/wecom/callback", params={"code": "good-code", "state": state}
            )
        assert response.status_code == 307
        location = response.headers["location"]
        assert location.startswith("/#wecom_token=")
        token = location.split("=", 1)[1]
        claims = jwt.decode(token, settings.wecom_session_secret, algorithms=["HS256"])
        assert claims["sub"] == "alice"
    finally:
        app.dependency_overrides.clear()
