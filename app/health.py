from __future__ import annotations

import socket

import httpx
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings


def probe_dependencies(db: Session, settings: Settings) -> dict[str, object]:
    details: dict[str, str] = {}
    statuses = {
        "database": _probe_database(db, details),
        "redis": _probe_redis(settings, details),
        "clamav": _probe_clamav(settings, details),
        "model": _probe_model(settings, details),
    }
    return {
        "status": "degraded" if "error" in statuses.values() else "ok",
        **statuses,
        "details": details,
    }


def _probe_database(db: Session, details: dict[str, str]) -> str:
    try:
        db.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        details["database"] = _error_detail(exc)
        return "error"


def _probe_redis(settings: Settings, details: dict[str, str]) -> str:
    if not settings.cache_url:
        return "disabled"
    client = redis.Redis.from_url(
        settings.cache_url,
        socket_connect_timeout=settings.health_dependency_timeout_seconds,
        socket_timeout=settings.health_dependency_timeout_seconds,
    )
    try:
        if client.ping():
            return "ok"
        details["redis"] = "PING returned a false response"
    except redis.RedisError as exc:
        details["redis"] = _error_detail(exc)
    finally:
        client.close()
    return "error"


def _probe_clamav(settings: Settings, details: dict[str, str]) -> str:
    if not settings.clamav_host:
        return "disabled"
    try:
        with socket.create_connection(
            (settings.clamav_host, settings.clamav_port),
            timeout=settings.health_dependency_timeout_seconds,
        ) as client:
            client.sendall(b"zPING\0")
            response = client.recv(64)
        if b"PONG" in response:
            return "ok"
        details["clamav"] = "PING did not return PONG"
    except OSError as exc:
        details["clamav"] = _error_detail(exc)
    return "error"


def _probe_model(settings: Settings, details: dict[str, str]) -> str:
    if not settings.model_ready:
        return "not_configured"
    if not settings.health_check_model:
        return "configured"
    base_url = settings.openai_base_url.rstrip("/") or "https://api.openai.com/v1"
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=settings.health_dependency_timeout_seconds,
        )
        response.raise_for_status()
        return "ok"
    except httpx.HTTPError as exc:
        details["model"] = _error_detail(exc)
        return "error"


def _error_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]
