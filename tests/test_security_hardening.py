from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app import wecom
from app.auth import get_auth_context
from app.config import Settings, get_settings
from app.core.vector_store import EnterpriseVectorStore, LocalHashEmbeddings
from app.db import Base
from app.logging_setup import JsonFormatter
from app.main import app
from app.middleware import PlatformMiddleware
from app.rbac import RoleService, ensure_default_roles
from app.security import validate_stored_file
from app.tools import ToolService
from app.wecom import (
    WeComIdentity,
    exchange_code_for_identity,
    mint_session_token,
    mint_state_token,
)


def _wecom_settings(**overrides) -> Settings:
    defaults = dict(
        auth_mode="wecom",
        wecom_corp_id="corp-1",
        wecom_agent_id="1000001",
        wecom_secret="corp-secret",
        wecom_redirect_uri="https://rag.example.com/api/auth/wecom/callback",
        wecom_tenant_id="default",
        wecom_session_secret="session-secret-0123456789abcdef-0123",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------- login state is bound to the browser ----------


def _callback(settings, cookie_state, query_state, monkeypatch):
    async def fake_exchange(_settings, _code, client=None):
        return WeComIdentity(userid="alice", display_name="Alice", department_ids=[3])

    monkeypatch.setattr("app.api.endpoints.auth_wecom.exchange_code_for_identity", fake_exchange)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, follow_redirects=False) as client:
            if cookie_state:
                client.cookies.set("wecom_login_state", cookie_state, path="/api/auth/wecom")
            return client.get(
                "/api/auth/wecom/callback", params={"code": "c", "state": query_state}
            )
    finally:
        app.dependency_overrides.clear()


def test_login_sets_an_httponly_state_cookie_matching_the_state_it_sends():
    settings = _wecom_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/api/auth/wecom/login")
    finally:
        app.dependency_overrides.clear()
    cookie = response.headers["set-cookie"]
    assert "wecom_login_state=" in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "SameSite=lax" in cookie and "Path=/api/auth/wecom" in cookie
    state = cookie.split("wecom_login_state=")[1].split(";")[0]
    assert f"state={state}" in response.headers["location"]


def test_callback_requires_the_cookie_that_started_the_login(monkeypatch):
    settings = _wecom_settings()
    state = mint_state_token(settings)

    ok = _callback(settings, state, state, monkeypatch)
    assert ok.headers["location"].startswith("/#wecom_token=")

    # a valid, server-signed state carried by someone who never started this login
    missing = _callback(settings, None, state, monkeypatch)
    assert "auth_error=invalid_state" in missing.headers["location"]

    other = _callback(settings, mint_state_token(settings) + "x", state, monkeypatch)
    assert "auth_error=invalid_state" in other.headers["location"]


def test_state_cookie_requirement_can_be_switched_off(monkeypatch):
    settings = _wecom_settings(wecom_require_state_cookie=False)
    state = mint_state_token(settings)
    response = _callback(settings, None, state, monkeypatch)
    assert response.headers["location"].startswith("/#wecom_token=")


# ---------- roles granted in the UI are live, not baked into the token ----------


def test_role_assignments_and_revocations_apply_without_logging_in_again():
    settings = _wecom_settings()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    token = mint_session_token(
        settings, WeComIdentity(userid="alice", display_name="Alice", department_ids=[])
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with Session(engine) as db:
        ensure_default_roles(db)
        service = RoleService()
        role = service.create_role(db, "default", "内容审核员", None)
        assignment = service.assign_user_role(db, "default", "alice", role.id)

        assert "内容审核员" in get_auth_context(credentials, settings, db).roles

        service.remove_user_role_assignment(db, "default", assignment.id)
        assert "内容审核员" not in get_auth_context(credentials, settings, db).roles


# ---------- WeCom access_token invalidation ----------


@pytest.mark.anyio
async def test_a_token_wecom_rejected_is_dropped_and_the_login_retried():
    settings = _wecom_settings()
    wecom._token_cache.clear()
    issued = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            token = f"tok{len(issued) + 1}"
            issued.append(token)
            return httpx.Response(
                200, json={"errcode": 0, "access_token": token, "expires_in": 7200}
            )
        if request.url.path == "/cgi-bin/user/getuserinfo":
            if request.url.params["access_token"] == "tok1":
                return httpx.Response(
                    200, json={"errcode": 42001, "errmsg": "access_token expired"}
                )
            return httpx.Response(200, json={"errcode": 0, "UserId": "alice"})
        return httpx.Response(200, json={"errcode": 0, "name": "Alice", "department": [3]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        identity = await exchange_code_for_identity(settings, "code", client=client)
    finally:
        await client.aclose()
        wecom._token_cache.clear()
    assert identity.userid == "alice" and issued == ["tok1", "tok2"]


# ---------- client IP behind a proxy that appends to X-Forwarded-For ----------


def _client_key(forwarded: str, direct: str = "10.0.0.1", trusted: str = "10.0.0.1") -> str:
    middleware = PlatformMiddleware(app=None, settings=Settings(trusted_proxy_ips=trusted))
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    request = Request({"type": "http", "headers": headers, "client": (direct, 1234)})
    return middleware._client_key(request)


def test_a_client_cannot_choose_its_own_rate_limit_bucket():
    # nginx ($proxy_add_x_forwarded_for) appends the real peer after whatever the client sent
    assert _client_key("1.2.3.4, 203.0.113.9") == "203.0.113.9"
    assert _client_key("203.0.113.9") == "203.0.113.9"
    assert _client_key("203.0.113.9, 10.0.0.1") == "203.0.113.9"  # our own proxies are skipped
    assert _client_key("not-an-ip") == "10.0.0.1"
    assert _client_key("", direct="10.0.0.1") == "10.0.0.1"
    assert _client_key("1.2.3.4", direct="198.51.100.7") == "198.51.100.7"  # untrusted peer


# ---------- pgvector ----------


def test_postgres_search_asks_for_iterative_scans_when_supported():
    store = EnterpriseVectorStore(Settings(vector_backend="memory"), LocalHashEmbeddings(8))
    statements: list[str] = []

    class FakeResult:
        def all(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params):
            statements.append(str(statement))
            return FakeResult()

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    store._raw_engine = FakeEngine()
    store._iterative_scan = True
    store._search_pgvector([0.1] * 8, "default", ["__public__"], ["__default__"], 5)
    assert statements[0].startswith("SET LOCAL hnsw.iterative_scan")
    assert statements[1].startswith("SET LOCAL hnsw.ef_search")
    assert "ORDER BY distance" in statements[2]

    statements.clear()
    store._iterative_scan = False
    store._search_pgvector([0.1] * 8, "default", ["__public__"], ["__default__"], 5)
    assert len(statements) == 1


# ---------- tool sandbox ----------


def test_sql_tool_rejects_unknown_functions_and_foreign_schemas():
    service = ToolService(Settings(tool_allowed_tables="employees,hr.salaries"))
    service.validate("sql.read", {"query": "SELECT count(*), max(id) FROM employees"})
    service.validate("sql.read", {"query": "SELECT * FROM hr.salaries"})

    for query in (
        "SELECT pg_read_file('/etc/passwd') FROM employees",
        "SELECT nextval('employee_id_seq') FROM employees",
        "SELECT * FROM other.employees",
        "SELECT * FROM salaries",
    ):
        with pytest.raises(ValueError):
            service.validate("sql.read", {"query": query})


def test_http_tool_connects_to_the_address_it_validated(monkeypatch):
    import socket

    service = ToolService(Settings(tool_http_allowed_domains="api.example.com"))
    resolved = [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 443))]
    monkeypatch.setattr("app.tools.socket.getaddrinfo", lambda *args, **kwargs: resolved)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "app.tools.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = service._execute_http("https://api.example.com/v1/policies?x=1")

    assert seen["url"].startswith("https://93.184.216.34/v1/policies")
    assert seen["host"] == "api.example.com" and seen["sni"] == "api.example.com"
    assert result["body"] == {"ok": True} and result["status_code"] == 200

    private = [(socket.AF_INET, 0, 0, "", ("10.1.2.3", 443))]
    monkeypatch.setattr("app.tools.socket.getaddrinfo", lambda *args, **kwargs: private)
    with pytest.raises(ValueError, match="公网"):
        service._execute_http("https://api.example.com/v1/policies")


def test_http_tool_stops_reading_at_the_size_cap(monkeypatch):
    import socket

    service = ToolService(Settings(tool_http_allowed_domains="api.example.com"))
    monkeypatch.setattr(
        "app.tools.socket.getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 443))],
    )
    chunks = iter([b"a" * 600_000, b"b" * 600_000, b"never read"])

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield from chunks

    def handler(request):
        return httpx.Response(200, stream=Body(), headers={"content-type": "text/plain"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "app.tools.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = service._execute_http("https://api.example.com/big")
    assert len(result["body"]) == 1_000_000
    assert next(chunks) == b"never read"  # the rest of the body was left unread


# ---------- upload validation ----------


def test_corrupt_and_bomb_images_are_rejected_not_crashed_on(tmp_path):
    settings = Settings(max_image_megapixels=1)
    corrupt = tmp_path / "bad.png"
    corrupt.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really a png")
    with pytest.raises(ValueError, match="图片"):
        validate_stored_file(corrupt, settings)

    large = tmp_path / "large.png"
    Image.new("RGB", (1400, 1000)).save(large)
    with pytest.raises(ValueError, match="像素"):
        validate_stored_file(large, settings)


def test_extra_log_fields_reach_the_json_output():
    record = logging.LogRecord("rag.x", logging.INFO, __file__, 1, "indexed", (), None)
    record.document_id = "doc-1"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "indexed" and payload["document_id"] == "doc-1"
