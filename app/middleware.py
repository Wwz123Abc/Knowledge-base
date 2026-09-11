from __future__ import annotations

import ipaddress
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
        if direct not in self.settings.trusted_proxy_ip_list:
            return direct
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded)) if forwarded else direct
        except ValueError:
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
            path = getattr(route, "path", request.url.path)
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
