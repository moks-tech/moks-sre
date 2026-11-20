"""Embedding versions on PostgreSQL + pgvector: vectors reused instead of re-embedded, one size per generation
enforced by the database, a new model built beside the serving one and switched in only after its evaluation,
and a rollback that brings the old model back with its index."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from moks.connectors.base import ConnectorError
from moks.db import session_scope
from moks.models import Document, EmbeddingCache, EmbeddingIndex, EvalRun, KnowledgeSource, LLMConfig
from moks.rag import service

from .conftest import login, make_user


class Counting:
    """A model whose vectors depend on the text and on which model it is, and that counts what it was sent."""
    sent: dict[str, list[str]] = {}

    def __init__(self, cfg):
        self.model, self.size = cfg.model, int(cfg.model.rsplit("-", 1)[-1])

    async def embed(self, texts):
        Counting.sent.setdefault(self.model, []).extend(t for t in texts if t != "dimension probe")  # chunks and queries only
        return [[float((hash((self.model, t, i)) % 97) + 1) for i in range(self.size)] for t in texts]


@pytest.fixture
async def pg(monkeypatch):
    async with session_scope() as db:
        if not await service.PostgresBackend(db).enable_vector():
            pytest.skip("pgvector is not installed on this server")
    monkeypatch.setattr(service, "backend_name", lambda: "postgres")
    monkeypatch.setattr(service, "provider_for", lambda cfg: Counting(cfg))

    async def unmetered(provider, cfg, texts, **kwargs):
        return await provider.embed(texts)
    monkeypatch.setattr(service.emb.budget, "embed", unmetered)
    Counting.sent = {}

    async def clean():
        async with session_scope() as db:
            await db.execute(delete(EmbeddingIndex).where(EmbeddingIndex.backend == "postgres"))
            await db.execute(delete(EmbeddingCache))  # each test counts what its model is sent, so none starts with cached vectors
            await db.execute(delete(LLMConfig).where(LLMConfig.purpose == "embedding"))
            await db.execute(delete(EvalRun).where(EvalRun.kind == "embedding"))
            await db.execute(delete(KnowledgeSource).where(KnowledgeSource.name.like("life-%")))
    await clean()
    yield
    await clean()


async def model(name: str, size: int, active: bool = True, **options) -> uuid.UUID:
    async with session_scope() as db:
        if active:
            await db.execute(update(LLMConfig).where(LLMConfig.purpose == "embedding").values(is_active=False))
        c = LLMConfig(name=f"{name}-{uuid.uuid4().hex[:4]}", purpose="embedding", provider="openai", model=f"{name}-{size}", is_active=active, embedding_options=options)
        db.add(c)
        await db.flush()
        return c.id


async def source() -> uuid.UUID:
    async with session_scope() as db:
        src = KnowledgeSource(name=f"life-{uuid.uuid4().hex[:6]}", type="upload", config={})
        db.add(src)
        await db.flush()
        return src.id


async def write(sid: uuid.UUID, path: str, body: str) -> None:
    async with session_scope() as db:
        await service.index_document(db, await db.get(KnowledgeSource, sid), path, body, revision=None, url=None)


async def active() -> EmbeddingIndex:
    async with session_scope() as db:
        return await service.active_index(db)


async def vectors(index_id: uuid.UUID) -> int:
    async with session_scope() as db:
        return (await db.execute(text("SELECT count(*) FROM chunk_embeddings WHERE index_id = :i AND embedding IS NOT NULL"), {"i": index_id})).scalar_one()


async def passed_evaluation(cfg_id: uuid.UUID, passed: bool = True) -> None:
    async with session_scope() as db:
        cfg = await db.get(LLMConfig, cfg_id)
        state = "passed" if passed else "failed"
        db.add(EvalRun(dataset="embedding", mode="rules", kind="embedding", status="completed", score=Decimal("0.9"),
                       gates=[{"name": "not_worse_than_serving", "state": state, "passed": passed}],
                       versions={"candidate": {"key": service.model_key(cfg), "config_id": str(cfg.id)}}))


DOC = "# Runbook\n\n## Restart\nRestart the api with a rolling restart.\n\n## Drain\nDrain the node before maintenance.\n"


async def test_a_rebuild_with_the_same_model_reuses_every_vector(pg):
    await model("alpha", 8)
    sid = await source()
    await write(sid, "a.md", DOC)
    await write(sid, "b.md", "# Other\nScale the deployment with the HPA.")
    mine = list(Counting.sent["alpha-8"])
    assert len(mine) >= 2
    async with session_scope() as db:
        result = await service.reindex(db)
    again = Counting.sent["alpha-8"][len(mine):]  # other tests' documents share this database and realm
    assert not set(again) & set(mine) and result["reused_vectors"] >= len(mine)  # none of this text was sent again
    assert await vectors((await active()).id) >= len(mine)


async def test_editing_one_section_re_embeds_only_that_section(pg):
    await model("alpha", 8)
    sid = await source()
    await write(sid, "a.md", DOC)
    before = list(Counting.sent["alpha-8"])
    await write(sid, "a.md", DOC.replace("rolling restart", "blue green restart"))
    again = Counting.sent["alpha-8"][len(before):]
    assert len(again) == 1 and "blue green" in again[0]
    async with session_scope() as db:
        doc = (await db.execute(select(Document).where(Document.source_id == sid))).scalar_one()
        assert doc.vector_state == "complete"
    assert await vectors((await active()).id) == len(before)


async def test_the_database_refuses_a_vector_of_another_size_in_a_generation(pg):
    await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    idx = await active()
    async with session_scope() as db:
        cid = (await db.execute(text("SELECT chunk_id FROM chunk_embeddings WHERE index_id = :i LIMIT 1"), {"i": idx.id})).scalar_one()
        with pytest.raises(DBAPIError, match="expected 8 dimensions"):
            await service.PostgresBackend(db).upsert_embeddings(idx.id, [(cid, [0.1] * 5)], idx.dimensions)


async def test_a_changed_configuration_writes_no_vectors_into_the_index_it_no_longer_matches(pg):
    cfg = await model("alpha", 8)
    sid = await source()
    await write(sid, "a.md", DOC)
    async with session_scope() as db:
        (await db.get(LLMConfig, cfg)).embedding_options = {"query_prefix": "q: "}  # edited behind the API's back
    await write(sid, "b.md", "# New\nnew text")
    async with session_scope() as db:
        doc = (await db.execute(select(Document).where(Document.path == "b.md", Document.source_id == sid))).scalar_one()
    assert doc.vector_state == "missing" and "rebuild the index" in doc.vector_error


async def test_a_new_model_needs_its_evaluation_before_it_can_replace_one_with_vectors(pg):
    await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    beta = await model("beta", 4, active=False)
    async with session_scope() as db:
        check = await service.replacement_check(db, await db.get(LLMConfig, beta))
        assert check["replacement"] and not check["allowed"] and "embedding evaluation" in check["reason"]
        with pytest.raises(ConnectorError, match="cannot replace") as raised:
            await service.reindex(db, llm_config_id=beta)
        assert raised.value.kind == "conflict"
    await passed_evaluation(beta, passed=False)
    async with session_scope() as db:
        assert "failed" in (await service.replacement_check(db, await db.get(LLMConfig, beta)))["reason"]


async def test_the_rule_can_be_turned_off_and_a_first_model_is_never_gated(pg, monkeypatch):
    beta = await model("beta", 4, active=False)
    async with session_scope() as db:
        assert (await service.replacement_check(db, await db.get(LLMConfig, beta)))["allowed"]  # nothing serves vectors yet
    await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    monkeypatch.setattr(service.get_settings(), "rag_require_embedding_evaluation", False)
    async with session_scope() as db:
        check = await service.replacement_check(db, await db.get(LLMConfig, beta))
    assert check["allowed"] and check["reason"] == "evaluation not required by settings"


async def test_a_model_migration_has_no_keyword_only_window_and_switches_the_model_with_the_index(pg):
    alpha = await model("alpha", 8)
    sid = await source()
    await write(sid, "a.md", DOC)
    old = await active()
    beta = await model("beta", 4, active=False, query_prefix="query: ")
    await passed_evaluation(beta)
    principal = service.Principal(str(uuid.uuid4()), "x@example.com", [])

    async with session_scope() as db:  # someone activates the new model before rebuilding: search keeps its vectors
        await db.execute(update(LLMConfig).where(LLMConfig.purpose == "embedding").values(is_active=False))
        await db.execute(update(LLMConfig).where(LLMConfig.id == beta).values(is_active=True))
    async with session_scope() as db:
        qv, reason = await service._query_vector(db, principal, "restart the api", old)
    assert reason is None and len(qv) == 8 and Counting.sent["alpha-8"][-1] == "restart the api"
    async with session_scope() as db:
        assert (await service.stats(db))["index"]["reindex_needed"]

    async with session_scope() as db:
        result = await service.reindex(db, llm_config_id=beta)
    new = await active()
    assert result["replaced"] == old.index_name and new.model_key == "openai/beta-4/default/" + new.model_key.rsplit("/", 1)[-1]
    assert new.spec["query_prefix"] == "query: " and new.spec["evaluation"]["passed"] is True
    assert all(t.startswith("query: ") is False for t in Counting.sent["beta-4"])  # documents get no query prefix
    async with session_scope() as db:
        assert (await db.get(LLMConfig, beta)).is_active and not (await db.get(LLMConfig, alpha)).is_active
        qv, reason = await service._query_vector(db, principal, "drain", new)
    assert reason is None and len(qv) == 4 and Counting.sent["beta-4"][-1] == "query: drain"


async def test_rolling_back_the_index_rolls_back_the_model(pg):
    alpha = await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    old = await active()
    beta = await model("beta", 4, active=False)
    await passed_evaluation(beta)
    async with session_scope() as db:
        await service.reindex(db, llm_config_id=beta)
    async with session_scope() as db:
        result = await service.activate_generation(db, old.index_name)
    assert result["model_restored"] and result["model"] == old.model_key
    async with session_scope() as db:
        assert (await db.get(LLMConfig, alpha)).is_active and not (await db.get(LLMConfig, beta)).is_active
        assert (await service.active_index(db)).id == old.id


async def test_a_rollback_is_refused_when_the_old_model_no_longer_makes_the_same_vectors(pg):
    alpha = await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    old = await active()
    beta = await model("beta", 4, active=False)
    await passed_evaluation(beta)
    async with session_scope() as db:
        await service.reindex(db, llm_config_id=beta)
        (await db.get(LLMConfig, alpha)).embedding_options = {"revision": "other"}
    async with session_scope() as db:
        with pytest.raises(ConnectorError, match="deleted or changed"):
            await service.activate_generation(db, old.index_name)


async def test_the_api_starts_a_rebuild_with_a_model_only_when_it_may_replace_the_current_one(pg, client):
    await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    beta = await model("beta", 4, active=False)
    admin = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(admin, ["admin:*:*"])
    h = await login(client, admin)
    check = (await client.get(f"/moks/api/knowledge/reindex/check?llm_config_id={beta}", headers=h)).json()
    assert check["replacement"] and not check["allowed"]
    refused = await client.post("/moks/api/knowledge/reindex", json={"llm_config_id": str(beta)}, headers=h)
    assert refused.status_code == 409 and "embedding evaluation" in refused.json()["detail"]
    await passed_evaluation(beta)
    ok = await client.post("/moks/api/knowledge/reindex", json={"llm_config_id": str(beta)}, headers=h)
    assert ok.status_code == 200 and ok.json()["replacement"] is True


async def test_the_api_refuses_an_option_change_that_would_strand_an_index(pg, client):
    alpha = await model("alpha", 8)
    await write(await source(), "a.md", DOC)
    admin = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(admin, ["admin:*:*"])
    h = await login(client, admin)
    row = next(r for r in (await client.get("/moks/api/admin/llm", headers=h)).json() if r["id"] == str(alpha))
    body = {k: row[k] for k in ("name", "purpose", "provider", "model", "timeout_seconds", "max_output_tokens", "daily_budget_usd", "input_price_per_mtok", "output_price_per_mtok")}
    changed = await client.put(f"/moks/api/admin/llm/{alpha}", json={**body, "embedding_options": {"query_prefix": "query: "}}, headers=h)
    assert changed.status_code == 409 and "Add a new configuration" in changed.json()["detail"]
    same = await client.put(f"/moks/api/admin/llm/{alpha}", json={**body, "timeout_seconds": 90}, headers=h)
    assert same.status_code == 200 and same.json()["model_key"] == row["model_key"]
    new = await client.post("/moks/api/admin/llm", json={**body, "name": f"tei-{uuid.uuid4().hex[:4]}", "provider": "huggingface", "model": "BAAI/bge-base-en-v1.5",
                                                         "endpoint": "http://moks-embedder:8090", "embedding_options": {"query_prefix": "Represent: ", "revision": "a5beb1e3"}}, headers=h)
    assert new.status_code == 201 and new.json()["model_key"].startswith("huggingface/BAAI/bge-base-en-v1.5/default/")
    chat = await client.post("/moks/api/admin/llm", json={**body, "name": f"tei-chat-{uuid.uuid4().hex[:4]}", "purpose": "chat", "provider": "huggingface"}, headers=h)
    assert chat.status_code == 422
    profiles = (await client.get("/moks/api/admin/llm/embedding-profiles", headers=h)).json()
    assert any(p["model"] == "BAAI/bge-base-en-v1.5" for p in profiles)
