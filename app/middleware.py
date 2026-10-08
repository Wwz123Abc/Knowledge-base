from __future__ import annotations

import ipaddress
import json
import logging
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.config import Settings
from app.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS
from app.request_context import request_id_context

try:
    import redis
except ImportError:  # pragma: no cover - redis is a declared runtime dependency
    redis = None

logger = logging.getLogger("rag.http")


class PlatformMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings):
        super().__init__(app)
        self.settings = settings
        self.requests: dict[str, deque[float]] = defaultdict(deque)
        self.redis = (
            redis.Redis.from_url(
                settings.cache_url,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            if redis and settings.cache_url
            else None
        )

    def _client_key(self, request: Request) -> str:
        direct = request.client.host if request.client else "unknown"
        trusted = set(self.settings.trusted_proxy_ip_list)
        if direct not in trusted:
            return direct
        # Walk X-Forwarded-For from the right, skipping our own proxies: the right-hand entries
        # are the ones our proxies appended, while the leftmost is whatever the client chose to
        # send (nginx's $proxy_add_x_forwarded_for appends to it, it doesn't replace it), so
        # trusting it let anyone pick their own rate-limit bucket.
        entries = [
            item.strip()
            for item in request.headers.get("X-Forwarded-For", "").split(",")
            if item.strip()
        ]
        for entry in reversed(entries):
            try:
                address = str(ipaddress.ip_address(entry))
            except ValueError:
                return direct
            if address not in trusted:
                return address
        return direct

    def _rate_limited(self, key: str, now: float) -> bool:
        if self.redis:
            redis_key = f"rag:rate-limit:{key}"
            try:
                pipe = self.redis.pipeline()
                pipe.zremrangebyscore(redis_key, 0, now - 60)
                # Never use the caller-controlled X-Request-ID as the member: reusing it
                # would overwrite the same sorted-set entry and bypass the rate limit.
                pipe.zadd(redis_key, {f"{now}:{uuid4()}": now})
                pipe.zcard(redis_key)
                pipe.expire(redis_key, 65)
                count = int(pipe.execute()[2])
                return count > self.settings.rate_limit_per_minute
            except redis.RedisError:
                logger.warning("Redis rate limiter unavailable; using process-local fallback")
        if len(self.requests) > 10_000:
            # Only used while Redis is down, but keys were never removed: a stream of
            # distinct client IPs grew this dict without bound.
            for stale in [k for k, w in self.requests.items() if not w or w[-1] <= now - 60]:
                del self.requests[stale]
        window = self.requests[key]
        while window and window[0] <= now - 60:
            window.popleft()
        if len(window) >= self.settings.rate_limit_per_minute:
            return True
        window.append(now)
        return False

    @staticmethod
    def _add_security_headers(response: Response, request_id: str) -> Response:
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            # wwcdn.weixin.qq.com hosts the official wwLogin QR-scan widget script, used so
            # desktop WeCom (which opens links in the system browser, not an in-client
            # webview) can still log in; open.work.weixin.qq.com is the iframe that widget
            # embeds to actually render the QR code.
            "script-src 'self' https://wwcdn.weixin.qq.com; "
            "frame-src https://open.work.weixin.qq.com; "
            "style-src 'self'; img-src 'self' data:; connect-src 'self'"
        )
        return response

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        context_token = request_id_context.set(request_id)
        try:
            if request.url.path.startswith("/api/") and request.url.path not in {
                "/api/health",
                "/api/metrics",
            }:
                key = self._client_key(request)
                now = time.time()
                if self._rate_limited(key, now):
                    return self._add_security_headers(
                        JSONResponse(
                            {"detail": "请求过于频繁，请稍后再试"},
                            status_code=429,
                            headers={"Retry-After": "60"},
                        ),
                        request_id,
                    )

            started = time.perf_counter()
            response = await call_next(request)
            duration = time.perf_counter() - started
            route = request.scope.get("route")
            # Unmatched requests (404s, scanners) must not mint a new Prometheus series per
            # distinct URL, so they all share one label.
            path = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
            HTTP_REQUEST_DURATION.labels(request.method, path).observe(duration)
            logger.info(
                "request method=%s path=%s status=%s duration_ms=%s request_id=%s",
                request.method,
                path,
                response.status_code,
                round(duration * 1000, 2),
                request_id,
            )
            return self._add_security_headers(response, request_id)
        finally:
            request_id_context.reset(context_token)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before the framework buffers them.

    FastAPI parses a multipart body *before* it runs the auth dependency, so without this an
    unauthenticated client could stream gigabytes into the server's temp storage and only
    then get a 401. The limit is checked against Content-Length up front and, for chunked
    bodies that don't declare one, against the bytes actually received.
    """

    _METHODS = {"POST", "PUT", "PATCH"}

    def __init__(self, app, multipart_limit: int, default_limit: int):
        self.app = app
        self.multipart_limit = multipart_limit
        self.default_limit = default_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in self._METHODS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        is_multipart = headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data")
        limit = self.multipart_limit if is_multipart else self.default_limit
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(send)
            return

        received = 0
        exceeded = False
        answered = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message):
            # Once the limit tripped, whatever the framework makes of the aborted read (FastAPI
            # wraps it, inside an ExceptionGroup, into a generic 400) is replaced by one 413.
            nonlocal answered
            if not exceeded:
                await send(message)
            elif not answered:
                answered = True
                await self._reject(send)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        except Exception:
            if not exceeded:
                raise
        if exceeded and not answered:
            await self._reject(send)

    @staticmethod
    async def _reject(send) -> None:
        body = json.dumps({"detail": "请求体过大"}, ensure_ascii=False).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
