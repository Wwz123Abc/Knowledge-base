from app.cache import QueryRewriteCache, RetrievalCache
from app.config import Settings
from app.core.retrieval import RetrievalResult


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        self.values[key] = value


def test_retrieval_cache_round_trip_and_acl_key_isolation():
    cache = RetrievalCache(Settings(cache_url="", retrieval_cache_ttl_seconds=60))
    cache.client = FakeRedis()
    key = cache.key("年假", "tenant-a", ["hr"], ["kb-1"])
    other_key = cache.key("年假", "tenant-a", ["finance"], ["kb-1"])
    result = RetrievalResult(documents=[], scores={"chunk-1": {"fused": 0.5}})

    cache.set(key, result)

    assert cache.get(key).scores == result.scores
    assert cache.get(other_key) is None


def test_query_rewrite_cache_round_trip_and_history_isolation():
    cache = QueryRewriteCache(Settings(cache_url="", retrieval_cache_ttl_seconds=60))
    cache.client = FakeRedis()
    key = cache.key("那需要多久？", "用户：如何申请年假？")
    other_history_key = cache.key("那需要多久？", "用户：如何报销差旅？")

    assert cache.get(key) is None  # miss before anything is cached

    cache.set(key, "申请年假需要多久？")

    assert cache.get(key) == "申请年假需要多久？"
    assert cache.get(other_history_key) is None  # different preceding turn, different key
