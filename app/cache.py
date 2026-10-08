from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache

import redis
from langchain_core.documents import Document

from app.config import Settings, get_settings
from app.core.retrieval import RetrievalResult

# How long an in-process epoch value is trusted before re-checking Redis. Every
# retrieval — cache hit or miss — needs the epoch just to build the cache key, so
# without this every single request pays a Redis round-trip before it even knows
# whether it hit the cache. A short TTL bounds the worst case (an invalidation
# taking up to this long to be seen by a given process) in exchange for cutting
# that round-trip out of the hot path almost all the time.
_EPOCH_CACHE_TTL_SECONDS = 1.5


def _redis_client(settings: Settings):
    # Without timeouts a hung (not refused) Redis would block every request on a cache read.
    if not settings.cache_url:
        return None
    return redis.Redis.from_url(settings.cache_url, socket_connect_timeout=0.5, socket_timeout=0.5)


class RetrievalCache:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = _redis_client(settings)
        self._epoch_cache: dict[str, tuple[int, float]] = {}

    def key(
        self, query: str, tenant_id: str, groups: list[str], knowledge_base_ids: list[str]
    ) -> str:
        raw = json.dumps(
            {
                # Version the key so pipeline changes (chunking, blending, statute
                # injection) invalidate stale cached retrievals automatically.
                "version": self.settings.retrieval_cache_version,
                # Per-tenant epoch so a document delete/ACL/version change on that
                # tenant invalidates its cached retrievals without a global flush.
                "epoch": self._get_epoch(tenant_id),
                "query": query,
                "tenant": tenant_id,
                "groups": sorted(groups),
                "knowledge_bases": sorted(knowledge_base_ids),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return "rag:retrieval:" + hashlib.sha256(raw.encode()).hexdigest()

    def _epoch_key(self, tenant_id: str) -> str:
        return f"rag:retrieval:epoch:{tenant_id}"

    def _get_epoch(self, tenant_id: str) -> int:
        if not self.client:
            return 0
        cached = self._epoch_cache.get(tenant_id)
        now = time.monotonic()
        if cached is not None and now - cached[1] < _EPOCH_CACHE_TTL_SECONDS:
            return cached[0]
        try:
            value = self.client.get(self._epoch_key(tenant_id))
            epoch = int(value) if value else 0
        except (redis.RedisError, ValueError, TypeError):
            epoch = 0
        self._epoch_cache[tenant_id] = (epoch, now)
        return epoch

    def invalidate_tenant(self, tenant_id: str) -> None:
        """Invalidate every cached retrieval for a tenant, e.g. after a document
        delete or an ACL/knowledge-base/version change that could change what a
        cached retrieval is allowed to return."""
        if not self.client:
            return
        try:
            self.client.incr(self._epoch_key(tenant_id))
        except redis.RedisError:
            pass

    def get(self, key: str) -> RetrievalResult | None:
        if not self.client:
            return None
        try:
            payload = self.client.get(key)
            if not payload:
                return None
            cached_result = json.loads(payload)
            return RetrievalResult(
                documents=[
                    Document(page_content=item["content"], metadata=item["metadata"])
                    for item in cached_result["documents"]
                ],
                scores=cached_result["scores"],
            )
        except (redis.RedisError, ValueError, KeyError, TypeError):
            return None

    def set(self, key: str, retrieval_result: RetrievalResult) -> None:
        if not self.client:
            return
        payload = {
            "documents": [
                {"content": doc.page_content, "metadata": doc.metadata}
                for doc in retrieval_result.documents
            ],
            "scores": retrieval_result.scores,
        }
        try:
            self.client.setex(
                key,
                self.settings.retrieval_cache_ttl_seconds,
                json.dumps(payload, ensure_ascii=False),
            )
        except redis.RedisError:
            pass


class QueryRewriteCache:
    """Caches the LLM query-rewrite output for a given (question, recent history) pair
    — a repeated follow-up ("那需要多久？" after the same preceding turn) re-invokes the
    rewrite model for an answer that's already known, when it's cheap to remember
    instead. Not versioned/epoch-scoped like retrieval results: a rewrite only depends
    on the conversation text itself, not on any document/ACL state, so there's nothing
    for it to go stale against within its TTL."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = _redis_client(settings)

    def key(self, question: str, history_text: str) -> str:
        raw = json.dumps({"question": question, "history": history_text}, ensure_ascii=False)
        return "rag:rewrite:" + hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        if not self.client:
            return None
        try:
            payload = self.client.get(key)
        except redis.RedisError:
            return None
        if payload is None:
            return None
        return payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload

    def set(self, key: str, rewritten_query: str) -> None:
        if not self.client:
            return
        try:
            self.client.setex(key, self.settings.retrieval_cache_ttl_seconds, rewritten_query)
        except redis.RedisError:
            pass


@lru_cache
def get_retrieval_cache() -> RetrievalCache:
    return RetrievalCache(get_settings())


@lru_cache
def get_query_rewrite_cache() -> QueryRewriteCache:
    return QueryRewriteCache(get_settings())
