from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from uuid import NAMESPACE_URL, uuid5

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector
from sqlalchemy import ARRAY, String, bindparam, create_engine, text

from app.config import Settings

logger = logging.getLogger("rag.vector_store")


class EnterpriseVectorStore:
    def __init__(self, settings: Settings, embeddings: Embeddings):
        self.settings = settings
        self.embeddings = embeddings
        # One engine (and connection pool) reused for every raw-SQL call this class
        # makes, instead of each method opening its own via create_engine() — that
        # used to rebuild the pool from scratch on every _ensure_vector_index /
        # list_ids / update_metadata / search call.
        self._raw_engine = (
            create_engine(settings.postgres_vector_url, pool_pre_ping=True)
            if settings.vector_backend == "pgvector"
            else None
        )
        if settings.vector_backend == "pgvector":
            self.store = PGVector(
                embeddings=embeddings,
                collection_name=settings.vector_collection,
                connection=settings.postgres_vector_url,
                embedding_length=settings.embedding_dimensions,
                use_jsonb=True,
            )
            self._ensure_vector_index()
        else:
            self.store = InMemoryVectorStore(embedding=embeddings)

    def _ensure_vector_index(self) -> None:
        # Without an ANN index, PGVector falls back to a brute-force sequential
        # scan over every row on each search, which stops scaling once the
        # corpus grows past a few thousand chunks. HNSW requires a fixed
        # vector dimension, so this also fixes the column type for
        # collections created before `embedding_length` was passed above.
        # Best-effort: retrieval still works (just slower) if this fails, so
        # a failure here must not block application startup.
        engine = self._raw_engine
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE langchain_pg_embedding ALTER COLUMN embedding "
                        f"TYPE vector({self.settings.embedding_dimensions})"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_langchain_pg_embedding_hnsw "
                        "ON langchain_pg_embedding USING hnsw (embedding vector_cosine_ops)"
                    )
                )
        except Exception:
            logger.warning("Could not create pgvector HNSW index", exc_info=True)

    def add_documents(self, documents: list[Document], ids: list[str]) -> list[str]:
        return self.store.add_documents(documents, ids=ids)

    def delete(self, ids: list[str]) -> None:
        self.store.delete(ids=ids)

    def list_ids(self) -> set[str]:
        if self.settings.vector_backend == "memory":
            return set(self.store.store)
        engine = self._raw_engine
        query = text(
            """
            SELECT embedding.id::text
            FROM langchain_pg_embedding AS embedding
            JOIN langchain_pg_collection AS collection
              ON collection.uuid = embedding.collection_id
            WHERE collection.name = :collection
            """
        )
        with engine.connect() as connection:
            return set(connection.scalars(query, {"collection": self.settings.vector_collection}))

    def update_metadata(self, ids: list[str], metadata: dict) -> None:
        if not ids:
            return
        if self.settings.vector_backend == "memory":
            for vector_id in ids:
                stored = self.store.store.get(vector_id)
                if stored:
                    stored["metadata"].update(metadata)
            return
        engine = self._raw_engine
        statement = text(
            """
            UPDATE langchain_pg_embedding
            SET cmetadata = cmetadata || CAST(:metadata AS jsonb)
            WHERE id::text IN :ids
              AND collection_id = (
                SELECT uuid FROM langchain_pg_collection WHERE name = :collection
              )
            """
        ).bindparams(bindparam("ids", expanding=True))
        with engine.begin() as connection:
            connection.execute(
                statement,
                {
                    "ids": ids,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                    "collection": self.settings.vector_collection,
                },
            )

    def search(
        self,
        query: str,
        tenant_id: str,
        user_groups: list[str],
        k: int,
        fetch_k: int,
        knowledge_base_ids: list[str] | None = None,
    ) -> list[Document]:
        scopes = ["__public__", *dict.fromkeys(user_groups)]
        knowledge_bases = ["__default__", *dict.fromkeys(knowledge_base_ids or [])]
        lower_score_is_better = self.settings.vector_backend == "pgvector"
        # Embed the query once: similarity_search_with_score() re-embeds on every
        # call, which would otherwise fire one embedding API request per
        # scope x knowledge_base combination for a single retrieval.
        query_vector = self.embeddings.embed_query(query)

        # Each chunk is stored as exactly one vector row, with the ACL groups
        # and knowledge bases it belongs to carried as JSON arrays in
        # cmetadata (see add_documents callers) rather than one duplicate
        # vector row per (scope, knowledge_base) combination. That dedup is
        # why this can't use PGVector's built-in `$in` filter DSL: `$in`
        # only does scalar equality against a field, it can't test whether a
        # user's allowed scopes overlap an array-valued metadata field. So
        # pgvector search runs one raw SQL query using jsonb `?|` (array
        # overlap) instead, matching the same table PGVector itself manages.
        if self.settings.vector_backend == "pgvector":
            scored_documents = self._search_pgvector(
                query_vector, tenant_id, scopes, knowledge_bases, fetch_k
            )
        else:
            scored_documents = self._search_memory(
                query_vector, tenant_id, scopes, knowledge_bases, fetch_k
            )

        # A chunk should only ever produce one row now, but keep the
        # dedup-by-chunk-id merge as a safety net (e.g. mixed old/new rows
        # mid-migration) — it's a no-op once the index is fully reconciled.
        merged: dict[str, tuple[Document, float]] = {}
        for document, score in scored_documents:
            chunk_id = str(document.metadata.get("chunk_id"))
            current = merged.get(chunk_id)
            if current is None or _is_better_score(float(score), current[1], lower_score_is_better):
                merged[chunk_id] = (document, float(score))
        ranked = sorted(
            merged.values(),
            key=lambda item: item[1],
            reverse=not lower_score_is_better,
        )[:k]
        for document, score in ranked:
            document.metadata["vector_score"] = score
        return [document for document, _score in ranked]

    def _search_pgvector(
        self,
        query_vector: list[float],
        tenant_id: str,
        scopes: list[str],
        knowledge_bases: list[str],
        fetch_k: int,
    ) -> list[tuple[Document, float]]:
        vector_literal = "[" + ",".join(repr(float(value)) for value in query_vector) + "]"
        engine = self._raw_engine
        statement = text(
            """
            SELECT
                embedding.document,
                embedding.cmetadata::text AS cmetadata_json,
                embedding.embedding <=> CAST(:query_vector AS vector) AS distance
            FROM langchain_pg_embedding AS embedding
            JOIN langchain_pg_collection AS collection
              ON collection.uuid = embedding.collection_id
            WHERE collection.name = :collection
              AND embedding.cmetadata ->> 'tenant_id' = :tenant_id
              AND embedding.cmetadata -> 'acl_scopes' ?| :scopes
              AND embedding.cmetadata -> 'knowledge_base_scopes' ?| :knowledge_bases
            ORDER BY distance
            LIMIT :fetch_k
            """
        ).bindparams(
            bindparam("scopes", type_=ARRAY(String)),
            bindparam("knowledge_bases", type_=ARRAY(String)),
        )
        with engine.connect() as connection:
            rows = connection.execute(
                statement,
                {
                    "query_vector": vector_literal,
                    "collection": self.settings.vector_collection,
                    "tenant_id": tenant_id,
                    "scopes": scopes,
                    "knowledge_bases": knowledge_bases,
                    "fetch_k": fetch_k,
                },
            ).all()
        results: list[tuple[Document, float]] = []
        for row in rows:
            metadata = json.loads(row.cmetadata_json) if row.cmetadata_json else {}
            document = Document(page_content=row.document or "", metadata=metadata)
            results.append((document, float(row.distance)))
        return results

    def _search_memory(
        self,
        query_vector: list[float],
        tenant_id: str,
        scopes: list[str],
        knowledge_bases: list[str],
        fetch_k: int,
    ) -> list[tuple[Document, float]]:
        scope_set = set(scopes)
        base_set = set(knowledge_bases)

        def filter_fn(doc: Document) -> bool:
            doc_scopes = doc.metadata.get("acl_scopes")
            if doc_scopes is None:
                legacy_scope = doc.metadata.get("acl_scope") or doc.metadata.get("access_group")
                doc_scopes = [legacy_scope or "__public__"]
            doc_bases = doc.metadata.get("knowledge_base_scopes")
            if doc_bases is None:
                doc_bases = [doc.metadata.get("knowledge_base_scope") or "__default__"]
            return (
                doc.metadata.get("tenant_id") == tenant_id
                and bool(scope_set.intersection(doc_scopes))
                and bool(base_set.intersection(doc_bases))
            )

        return self.store.similarity_search_with_score_by_vector(
            embedding=query_vector, k=fetch_k, filter=filter_fn
        )


def _is_better_score(candidate: float, current: float, lower_is_better: bool) -> bool:
    return candidate < current if lower_is_better else candidate > current


def acl_scopes(groups: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(item.strip() for item in groups if item.strip()))
    return cleaned or ["__public__"]


def vector_id_for_chunk(chunk_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"enterprise-rag:{chunk_id}"))


class LocalHashEmbeddings(Embeddings):
    """Deterministic offline embeddings for local development and lexical fallback."""

    def __init__(self, dimensions: int = 1536):
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in self._tokens(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = text.lower()
        tokens = re.findall(r"[a-z0-9_]+", normalized)
        cjk_runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", normalized)
        for run in cjk_runs:
            tokens.extend(run[index : index + 2] for index in range(max(len(run) - 1, 0)))
            tokens.extend(run[index : index + 3] for index in range(max(len(run) - 2, 0)))
            if len(run) == 1:
                tokens.append(run)
        return tokens


class FastEmbedEmbeddings(Embeddings):
    """LangChain adapter for FastEmbed's lightweight local ONNX models."""

    def __init__(self, model_name: str, cache_dir: str):
        from fastembed import TextEmbedding

        self.model_name = model_name
        self.model = TextEmbedding(model_name=model_name, cache_dir=cache_dir, lazy_load=True)
        self._inference_lock = threading.RLock()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        with self._inference_lock:
            return [vector.tolist() for vector in self.model.passage_embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        with self._inference_lock:
            return next(iter(self.model.query_embed(text))).tolist()


def build_embeddings(settings: Settings) -> Embeddings:
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbeddings(
            settings.embedding_model,
            str(settings.embedding_cache_dir),
        )
    if settings.embedding_provider == "local_hash":
        return LocalHashEmbeddings(settings.embedding_dimensions)
    api_key = settings.embedding_api_key or settings.openai_api_key
    if not api_key:
        raise RuntimeError("尚未配置 OPENAI_API_KEY，无法执行向量化或问答")
    kwargs: dict = {
        "model": settings.embedding_model,
        "api_key": api_key,
    }
    base_url = settings.embedding_base_url or settings.openai_base_url
    if base_url:
        kwargs["base_url"] = base_url
    if settings.embedding_model.startswith("text-embedding-3"):
        kwargs["dimensions"] = settings.embedding_dimensions
    return OpenAIEmbeddings(**kwargs)
