from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app import services


def test_vector_store_is_constructed_once_under_concurrency(monkeypatch):
    barrier = Barrier(8)
    instances = []

    class FakeVectorStore:
        def __init__(self, settings, embeddings):
            instances.append(self)

    monkeypatch.setattr(services, "_vector_store_instance", None)
    monkeypatch.setattr(services, "EnterpriseVectorStore", FakeVectorStore)
    monkeypatch.setattr(services, "build_embeddings", lambda settings: object())

    def resolve_store():
        barrier.wait()
        return services.get_vector_store()

    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: resolve_store(), range(8)))

    assert len(instances) == 1
    assert all(store is stores[0] for store in stores)
