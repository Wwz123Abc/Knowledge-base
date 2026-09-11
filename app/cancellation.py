from __future__ import annotations

from threading import Lock

import redis

from app.config import Settings, get_settings


class CancellationRegistry:
    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self._cancelled: set[str] = set()
        self._lock = Lock()
        self._redis = (
            redis.Redis.from_url(
                settings.cache_url,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )
            if settings.cache_url
            else None
        )

    @staticmethod
    def _key(trace_id: str) -> str:
        return f"rag:cancellation:{trace_id}"

    def cancel(self, trace_id: str) -> None:
        if self._redis:
            try:
                self._redis.setex(self._key(trace_id), 3600, "1")
                return
            except redis.RedisError:
                pass
        with self._lock:
            self._cancelled.add(trace_id)

    def is_cancelled(self, trace_id: str) -> bool:
        if self._redis:
            try:
                return bool(self._redis.exists(self._key(trace_id)))
            except redis.RedisError:
                pass
        with self._lock:
            return trace_id in self._cancelled

    def clear(self, trace_id: str) -> None:
        if self._redis:
            try:
                self._redis.delete(self._key(trace_id))
            except redis.RedisError:
                pass
        with self._lock:
            self._cancelled.discard(trace_id)


cancellations = CancellationRegistry()
