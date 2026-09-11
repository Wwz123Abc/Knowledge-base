import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings
from app.core.vector_store import FastEmbedEmbeddings, LocalHashEmbeddings, build_embeddings


def test_local_hash_embeddings_are_deterministic_and_normalized():
    embeddings = LocalHashEmbeddings(dimensions=64)

    first = embeddings.embed_query("员工差旅报销材料")
    second = embeddings.embed_query("员工差旅报销材料")

    assert first == second
    assert len(first) == 64
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0)


def test_local_hash_provider_does_not_require_api_key():
    settings = Settings(
        embedding_provider="local_hash",
        embedding_dimensions=32,
        openai_api_key="",
    )

    embeddings = build_embeddings(settings)

    assert isinstance(embeddings, LocalHashEmbeddings)
    assert settings.embeddings_ready is True


def test_fastembed_provider_builds_without_api_key():
    settings = Settings(
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dimensions=512,
        openai_api_key="",
    )

    embeddings = build_embeddings(settings)

    assert isinstance(embeddings, FastEmbedEmbeddings)
    assert settings.embeddings_ready is True


def test_fastembed_query_initialization_is_serialized():
    class Vector(list):
        def tolist(self):
            return list(self)

    class RaceDetectingModel:
        active = 0
        max_active = 0

        def query_embed(self, _text):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            self.active -= 1
            yield Vector([1.0])

    embeddings = object.__new__(FastEmbedEmbeddings)
    embeddings.model = RaceDetectingModel()
    embeddings._inference_lock = threading.RLock()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(embeddings.embed_query, ["a", "b", "c", "d"]))

    assert results == [[1.0]] * 4
    assert embeddings.model.max_active == 1
