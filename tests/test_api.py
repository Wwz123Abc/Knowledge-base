from fastapi.testclient import TestClient

from app.main import app


def test_health_and_document_list_work_without_model_key():
    with TestClient(app) as client:
        home = client.get("/")
        health = client.get("/api/health")
        identity = client.get("/api/me")
        documents = client.get("/api/documents")

    assert home.status_code == 200
    assert "企业知识库" in home.text
    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert health.json()["redis"] in {"ok", "disabled"}
    assert health.json()["clamav"] in {"ok", "disabled"}
    assert health.json()["model"] in {"configured", "not_configured"}
    assert isinstance(health.json()["details"], dict)
    assert identity.status_code == 200
    assert identity.json()["user_id"] == "local-admin"
    assert "admin" in identity.json()["roles"]
    assert documents.status_code == 200
    assert isinstance(documents.json(), list)
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["referrer-policy"] == "no-referrer"
    assert health.headers["content-security-policy"].startswith("default-src 'self'")
    assert health.headers["x-request-id"]
