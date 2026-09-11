import httpx

from app.config import Settings
from app.health import probe_dependencies


class HealthyDatabase:
    def execute(self, _statement):
        return None


class HealthyRedis:
    def ping(self):
        return True

    def close(self):
        return None


class HealthyClamav:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def sendall(self, _payload):
        return None

    def recv(self, _size):
        return b"PONG\0"


class HealthyModelResponse:
    def raise_for_status(self):
        return None


def test_dependency_health_reports_configured_services(monkeypatch):
    monkeypatch.setattr("app.health.redis.Redis.from_url", lambda *_args, **_kwargs: HealthyRedis())
    monkeypatch.setattr(
        "app.health.socket.create_connection", lambda *_args, **_kwargs: HealthyClamav()
    )
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: HealthyModelResponse())
    settings = Settings(
        cache_url="redis://cache/2",
        clamav_host="clamav",
        openai_api_key="configured",
        openai_base_url="https://model.example/v1",
        health_check_model=True,
    )

    result = probe_dependencies(HealthyDatabase(), settings)

    assert result == {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
        "clamav": "ok",
        "model": "ok",
        "details": {},
    }


def test_dependency_health_degrades_and_includes_error_detail():
    class BrokenDatabase:
        def execute(self, _statement):
            raise RuntimeError("database unavailable")

    result = probe_dependencies(BrokenDatabase(), Settings(openai_api_key=""))

    assert result["status"] == "degraded"
    assert result["database"] == "error"
    assert result["redis"] == "disabled"
    assert result["clamav"] == "disabled"
    assert result["model"] == "not_configured"
    assert "database unavailable" in result["details"]["database"]
