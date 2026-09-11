from __future__ import annotations

import time
from functools import lru_cache
from threading import Lock


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_seconds: float = 30):
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.opened_at: float | None = None
        self.lock = Lock()

    def call(self, function, *args, **kwargs):
        self._check()
        try:
            result = function(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def _check(self) -> None:
        with self.lock:
            if self.opened_at is None:
                return
            if time.monotonic() - self.opened_at >= self.reset_seconds:
                self.failures = 0
                self.opened_at = None
                return
            raise CircuitOpenError("模型服务熔断中，请稍后重试")

    def record_failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()

    def record_success(self) -> None:
        with self.lock:
            self.failures = 0
            self.opened_at = None


@lru_cache
def get_model_circuit_breaker() -> CircuitBreaker:
    return CircuitBreaker()
