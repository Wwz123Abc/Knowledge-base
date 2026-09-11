from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cancellation import CancellationRegistry
from app.config import Settings
from app.middleware import PlatformMiddleware


def test_rate_limit_applies_security_headers_and_honors_trusted_proxy():
    app = FastAPI()
    app.add_middleware(
        PlatformMiddleware,
        settings=Settings(
            cache_url="",
            rate_limit_per_minute=2,
            trusted_proxy_ips="testclient",
        ),
    )

    @app.get("/api/value")
    def value():
        return {"ok": True}

    with TestClient(app) as client:
        assert (
            client.get("/api/value", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
        )
        assert (
            client.get("/api/value", headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
        )
        limited = client.get("/api/value", headers={"X-Forwarded-For": "203.0.113.1"})
        different_client = client.get("/api/value", headers={"X-Forwarded-For": "203.0.113.2"})

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.headers["x-content-type-options"] == "nosniff"
    assert different_client.status_code == 200


class SharedCancellationRedis:
    def __init__(self):
        self.values = set()

    def setex(self, key, _ttl, _value):
        self.values.add(key)

    def exists(self, key):
        return key in self.values

    def delete(self, key):
        self.values.discard(key)


def test_cancellation_state_is_shared_through_redis():
    shared = SharedCancellationRedis()
    first = CancellationRegistry(Settings(cache_url=""))
    second = CancellationRegistry(Settings(cache_url=""))
    first._redis = shared
    second._redis = shared

    first.cancel("trace-1")
    assert second.is_cancelled("trace-1") is True
    second.clear("trace-1")
    assert first.is_cancelled("trace-1") is False


class SharedRatePipeline:
    def __init__(self, values):
        self.values = values
        self.operations = []

    def zremrangebyscore(self, key, minimum, maximum):
        self.operations.append(("remove", key, minimum, maximum))
        return self

    def zadd(self, key, members):
        self.operations.append(("add", key, members))
        return self

    def zcard(self, key):
        self.operations.append(("count", key))
        return self

    def expire(self, key, ttl):
        self.operations.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for operation in self.operations:
            name, key, *arguments = operation
            bucket = self.values.setdefault(key, {})
            if name == "remove":
                maximum = arguments[1]
                expired = [member for member, score in bucket.items() if score <= maximum]
                for member in expired:
                    del bucket[member]
                results.append(len(expired))
            elif name == "add":
                bucket.update(arguments[0])
                results.append(len(arguments[0]))
            elif name == "count":
                results.append(len(bucket))
            else:
                results.append(True)
        return results


class SharedRateRedis:
    def __init__(self):
        self.values = {}

    def pipeline(self):
        return SharedRatePipeline(self.values)


def test_rate_limit_is_shared_and_cannot_be_bypassed_by_reusing_request_id():
    app = FastAPI()
    settings = Settings(cache_url="", rate_limit_per_minute=2)
    first = PlatformMiddleware(app, settings=settings)
    second = PlatformMiddleware(app, settings=settings)
    shared = SharedRateRedis()
    first.redis = shared
    second.redis = shared

    assert first._rate_limited("client", 10.0) is False
    assert second._rate_limited("client", 11.0) is False
    assert first._rate_limited("client", 12.0) is True
