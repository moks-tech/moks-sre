"""Retrieval backends. PostgreSQL is always the source of truth for documents and chunks; a
backend only indexes and searches them.

postgres   - full-text (websearch_to_tsquery, ts_rank_cd) and, when the pgvector extension is
             installed and an embedding index is active, cosine k-NN; fused with RRF.
pinecone   - vectors only, one index per embedding version. Keyword search stays in PostgreSQL, which
             holds the text. Pinecone receives vectors, document ids and scope names, never chunk text.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..connectors.base import ConnectorError
from . import access

RRF_K = 60
MAPPING_VERSION = 2


def rrf(*rankings: list[str], weights: list[float] | None = None, k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: each ranker adds weight / (k + rank). Equal weights and k = 60 are the textbook form;
    a weight of 2 is the same as listing a ranking twice, which is how an exact identifier counted before weights."""
    scores: dict[str, float] = {}
    for i, ranking in enumerate(rankings):
        w = 1.0 if weights is None else weights[i]
        if w <= 0:
            continue
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + w / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


@dataclass(frozen=True)
class Narrow:
    """Filters a request may add. Each can only remove candidates: none reaches past the grants."""
    doc_types: tuple[str, ...] = ()
    changed_since: datetime | None = None

    def __bool__(self) -> bool:
        return bool(self.doc_types or self.changed_since)


def _filters_sql(found: list[access.Grant], params: dict, only: list[str] | None = None, narrow: Narrow | None = None) -> str:
    params["realm"] = access.realm()
    narrowed = ""
    if only:
        params["only"] = [uuid.UUID(i) for i in only]
        narrowed = " AND s.id = ANY(CAST(:only AS uuid[]))"
    if narrow and narrow.doc_types:
        params["doc_types"] = list(narrow.doc_types)
        narrowed += " AND d.doc_type = ANY(CAST(:doc_types AS varchar[]))"
    if narrow and narrow.changed_since:
        params["changed_since"] = narrow.changed_since
        narrowed += " AND d.content_changed_at >= :changed_since"
    return f"d.status = 'active' AND d.lifecycle = 'active' AND s.lifecycle = 'active' AND d.realm = :realm AND s.realm = :realm AND {access.sql(found, params)}{narrowed}"


class PostgresBackend:
    name = "postgres"

    async def catalog(self, found: list[access.Grant], limit: int = 2000) -> list[tuple]:
        """(environment, namespace, document service, source service, applies_to) for what these grants can read: the
        names a question may be resolved against. Built from readable documents only, so resolving a name can never
        reveal that something unreadable exists."""
        params: dict[str, Any] = {"lim": limit}
        where = _filters_sql(found, params)
        return [tuple(r) for r in (await self.db.execute(text(f"""
            SELECT DISTINCT d.environment, d.namespace, d.service, s.service, s.applies_to
            FROM documents d JOIN knowledge_sources s ON s.id = d.source_id WHERE {where} LIMIT :lim"""), params)).all()]

    def __init__(self, db: AsyncSession):
        self.db = db

    async def has_vector(self) -> bool:
        row = (await self.db.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'chunk_embeddings' AND column_name = 'embedding'"))).first()
        return row is not None

    async def enable_vector(self) -> bool:
        """Whether vectors can be stored, adding the column first when pgvector has been installed since.
        Migration 0002 adds it only if this role could create the extension then. A tenant of a shared
        CloudNativePG cluster cannot (pgvector is not a trusted extension), so the cluster installs it
        (the Database's extensions) and the next index build adds the column here."""
        if await self.has_vector():
            return True
        if (await self.db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))).first() is None:
            return False
        await self.db.execute(text("ALTER TABLE chunk_embeddings ADD COLUMN IF NOT EXISTS embedding vector"))
        return True

    async def keyword(self, query: str, found: list[access.Grant], k: int, only: list[str] | None = None, narrow: Narrow | None = None) -> list[str]:
        params: dict[str, Any] = {"q": query, "k": k}
        where = _filters_sql(found, params, only, narrow)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
            WHERE {where} AND c.tsv @@ websearch_to_tsquery('english', :q)
            ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) DESC, d.path, c.ord LIMIT :k"""), params)).all()
        if not rows:  # fall back to OR-semantics so partial matches still rank
            terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if len(t) > 2][:12]
            if terms:
                params["q2"] = " | ".join(terms)
                rows = (await self.db.execute(text(f"""
                    SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
                    WHERE {where} AND c.tsv @@ to_tsquery('english', :q2)
                    ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', :q2)) DESC, d.path, c.ord LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def identifiers(self, tokens: list[str], found: list[access.Grant], k: int, only: list[str] | None = None, narrow: Narrow | None = None) -> list[str]:
        """Exact matches for error codes, resource names, paths and configuration keys, which a text
        search stems and splits. PostgreSQL holds the text whichever backend ranks the rest."""
        if not tokens:
            return []
        params: dict[str, Any] = {"k": k}
        where = _filters_sql(found, params, only, narrow)
        hits = []
        for i, t in enumerate(tokens[:6]):
            params[f"t{i}"] = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            hits.append(f"(CASE WHEN c.text ILIKE :t{i} OR d.path ILIKE :t{i} OR c.heading ILIKE :t{i} THEN 1 ELSE 0 END)")
        score = " + ".join(hits)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
            WHERE {where} AND ({score}) > 0 ORDER BY ({score}) DESC, d.path, c.ord LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def vector(self, embedding: list[float], index_id: uuid.UUID, found: list[access.Grant], k: int, only: list[str] | None = None, narrow: Narrow | None = None) -> list[str]:
        params: dict[str, Any] = {"v": json.dumps(embedding), "k": k, "idx": index_id}
        where = _filters_sql(found, params, only, narrow)
        rows = (await self.db.execute(text(f"""
            SELECT c.id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id JOIN documents d ON d.id = c.document_id JOIN knowledge_sources s ON s.id = d.source_id
            WHERE {where} AND e.index_id = :idx
            ORDER BY e.embedding <=> CAST(:v AS vector) LIMIT :k"""), params)).all()
        return [str(r[0]) for r in rows]

    async def upsert_embeddings(self, index_id: uuid.UUID, items: list[tuple[uuid.UUID, list[float]]], dims: int | None = None) -> None:
        """One statement for the batch. With `dims`, the cast is to vector(dims), so PostgreSQL itself refuses a
        vector of another size: one generation's vectors can never mix sizes, whatever the caller checked."""
        if not items:
            return
        cast = f"vector({int(dims)})" if dims else "vector"
        await self.db.execute(text(f"""
            INSERT INTO chunk_embeddings (chunk_id, index_id, embedding) VALUES (:c, :i, CAST(:v AS {cast}))
            ON CONFLICT (chunk_id, index_id) DO UPDATE SET embedding = EXCLUDED.embedding"""),
            [{"c": chunk_id, "i": index_id, "v": json.dumps(emb)} for chunk_id, emb in items])

    async def delete_document(self, document_id: uuid.UUID) -> None:
        pass  # chunk rows (and their embeddings) are deleted by FK cascade

    async def index_chunks(self, index, rows: list[dict], embeddings: list[list[float]] | None) -> None:
        if embeddings and index is not None:
            await self.upsert_embeddings(index.id, [(uuid.UUID(r["chunk_id"]), e) for r, e in zip(rows, embeddings)], index.dimensions)


class PineconeBackend:
    """Pinecone REST API, pinned to one version. A derived copy: PostgreSQL can rebuild all of it."""

    name = "pinecone"
    API_VERSION = "2026-07"
    CONTROL = "https://api.pinecone.io"
    NAMESPACE = "moks"
    UPSERT_BATCH = 25  # high-dimension vectors serialize to tens of KB each
    READY_SECONDS = 120
    POLL_SECONDS = 2.0
    TRIES = 4

    def __init__(self, api_key: str | None, cloud: str = "aws", region: str = "us-east-1", transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ConnectorError("config", "the pinecone backend needs its API key: paste it in Settings > Credentials (MOKS_CRED_PINECONE_API_KEY)")
        self.cloud, self.region = cloud, region
        self.client = httpx.AsyncClient(timeout=30, transport=transport, headers={"Api-Key": api_key, "X-Pinecone-Api-Version": self.API_VERSION})
        self._hosts: dict[str, str] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, url: str, body: Any = None, ok404: bool = False) -> dict:
        for attempt in range(self.TRIES):
            try:
                r = await self.client.request(method, url, json=body)
            except httpx.HTTPError as exc:
                raise ConnectorError("unavailable", f"cannot reach Pinecone ({type(exc).__name__})", retryable=True) from None
            if r.status_code != 429 or attempt == self.TRIES - 1:
                break
            await asyncio.sleep(2 ** attempt)
        if r.status_code == 404 and ok404:
            return {}
        if r.status_code in (401, 403):
            raise ConnectorError("auth", f"Pinecone rejected the credentials (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise ConnectorError("unavailable" if r.status_code >= 500 or r.status_code == 429 else "bad_request", f"Pinecone {method}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    async def _describe(self, name: str) -> dict:
        return await self._req("GET", f"{self.CONTROL}/indexes/{name}", ok404=True)

    async def _host(self, name: str) -> str | None:
        if name not in self._hosts:
            info = await self._describe(name)
            if not info.get("host"):
                return None
            self._hosts[name] = info["host"]
        return self._hosts[name]

    async def health(self) -> dict:
        return await self._req("GET", f"{self.CONTROL}/indexes")

    async def ensure_index(self, name: str, dims: int | None, model_key: str = "") -> None:
        if not dims:
            return  # keyword-only: nothing to store here
        info = await self._describe(name)
        if not info:
            info = await self._req("POST", f"{self.CONTROL}/indexes", {
                "name": name, "deployment": {"deployment_type": "managed", "cloud": self.cloud, "region": self.region},
                # The reserved field _values makes a vector index. Any other name makes a document index,
                # which refuses the /vectors endpoints used here.
                "schema": {"fields": {"_values": {"type": "dense_vector", "dimension": dims, "metric": "cosine"}}}})
        found = (((info.get("schema") or {}).get("fields") or {}).get("_values") or {}).get("dimension", dims)
        if found != dims:
            raise ConnectorError("config", f"Pinecone index {name} has vector dimension {found}, expected {dims}; run a reindex")
        for _ in range(int(self.READY_SECONDS / self.POLL_SECONDS)):
            if (info.get("status") or {}).get("ready"):
                self._hosts[name] = info["host"]
                return
            await asyncio.sleep(self.POLL_SECONDS)
            info = await self._describe(name)
        raise ConnectorError("unavailable", f"Pinecone index {name} was not ready within {self.READY_SECONDS}s")

    async def index_chunks(self, index_name: str, rows: list[dict], embeddings: list[list[float]] | None) -> None:
        host = await self._host(index_name) if embeddings else None
        if not host:
            return
        vectors = [{"id": r["chunk_id"], "values": v, "metadata": {"document_id": r["document_id"], "environment": r["environment"], "namespace": r["namespace"]}}
                   for r, v in zip(rows, embeddings or [])]
        for i in range(0, len(vectors), self.UPSERT_BATCH):
            await self._req("POST", f"https://{host}/vectors/upsert", {"vectors": vectors[i:i + self.UPSERT_BATCH], "namespace": self.NAMESPACE})

    async def delete_document(self, index_name: str, document_id: uuid.UUID) -> None:
        host = await self._host(index_name)
        if host:
            await self._req("POST", f"https://{host}/vectors/delete", {"filter": {"document_id": {"$eq": str(document_id)}}, "namespace": self.NAMESPACE}, ok404=True)

    async def update_scope(self, index_name: str, document_id: uuid.UUID, environment: str, namespace: str) -> None:
        host = await self._host(index_name)
        if host:
            await self._req("POST", f"https://{host}/vectors/update", {"filter": {"document_id": {"$eq": str(document_id)}}, "namespace": self.NAMESPACE,
                                                                       "setMetadata": {"environment": environment, "namespace": namespace}}, ok404=True)

    async def count_document(self, index_name: str, document_id: uuid.UUID) -> int | None:
        return None  # Pinecone cannot count by metadata; revision tracking is what is available

    async def vector(self, index_name: str, embedding: list[float], found: list[access.Grant], k: int, exclude: dict | None = None) -> tuple[list[str], bool]:
        host = await self._host(index_name)
        if not host:
            return [], True
        flt, exact = access.pinecone(found)
        exact = exact and not any((exclude or {}).values())  # metadata has no source id, so the final check removes these
        body: dict[str, Any] = {"vector": embedding, "topK": k, "namespace": self.NAMESPACE, "includeMetadata": False, "includeValues": False}
        if flt:
            body["filter"] = flt
        return [m["id"] for m in (await self._req("POST", f"https://{host}/query", body)).get("matches", [])], exact

    async def drop_index(self, index_name: str) -> None:
        await self._req("DELETE", f"{self.CONTROL}/indexes/{index_name}", ok404=True)
        self._hosts.pop(index_name, None)
