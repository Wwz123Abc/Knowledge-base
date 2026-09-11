from __future__ import annotations

import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import _jwks_client
from app.config import Settings, get_settings
from app.main import app


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_oidc_jwks_signature_claims_and_rejection_paths():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "local-integration-key",
                "use": "sig",
                "alg": "RS256",
                "n": _base64url_uint(public_numbers.n),
                "e": _base64url_uint(public_numbers.e),
            }
        ]
    }

    class JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(jwks).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), JwksHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    issuer = "https://identity.local.example/"
    audience = "enterprise-rag"
    settings = Settings(
        auth_mode="oidc",
        oidc_issuer=issuer,
        oidc_audience=audience,
        oidc_jwks_url=f"http://127.0.0.1:{server.server_port}/jwks.json",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    now = datetime.now(UTC)

    def token(**overrides):
        claims = {
            "sub": "employee-1001",
            "name": "本地 OIDC 测试用户",
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "tenant_id": "tenant-a",
            "groups": ["hr", "employees"],
            "roles": ["user"],
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "local-integration-key"},
        )

    try:
        client = TestClient(app)
        valid = client.get("/api/me", headers={"Authorization": f"Bearer {token()}"})
        assert valid.status_code == 200
        assert valid.json() == {
            "user_id": "employee-1001",
            "display_name": "本地 OIDC 测试用户",
            "tenant_id": "tenant-a",
            "groups": ["hr", "employees"],
            "roles": ["user"],
            "permissions": [],
        }

        assert client.get("/api/me").status_code == 401
        assert (
            client.get(
                "/api/me",
                headers={"Authorization": f"Bearer {token(aud='wrong-audience')}"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/me",
                headers={"Authorization": f"Bearer {token(tenant_id='')}"},
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides.clear()
        _jwks_client.cache_clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
