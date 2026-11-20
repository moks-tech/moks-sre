"""The embedding evaluation: measures that mean what they say, a dataset that holds together, gates that decide
on evidence, and a run that unlocks a model replacement only when it passes."""
from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections import Counter

import pytest
from sqlalchemy import delete

from moks.db import session_scope
from moks.evals import embedding_eval, runner
from moks.evals.retrieval_metrics import BM25, average_precision, hybrid, identifier_ranking, mrr_at, ndcg_at, recall_at, separation, top_answer_at
from moks.models import EmbeddingIndex, EvalRun, LLMConfig
from moks.rag import service
from moks.rag.backends import rrf

DATA = embedding_eval.load()


# ---- measures ---------------------------------------------------------------------------------

def test_ndcg_is_graded_and_one_only_for_the_ideal_order():
    rel = {"a": 3, "b": 1}
    assert ndcg_at(["a", "b", "x"], rel) == pytest.approx(1.0)
    assert ndcg_at(["b", "a"], rel) < ndcg_at(["a", "b"], rel)
    assert ndcg_at(["x", "y"], rel) == 0.0
    swapped = (1 / math.log2(2) + 7 / math.log2(3)) / (7 / math.log2(2) + 1 / math.log2(3))
    assert ndcg_at(["b", "a"], rel) == pytest.approx(swapped)


def test_recall_mrr_map_and_answer_position():
    rel = {"a": 3, "b": 2, "c": 1}
    assert recall_at(["x", "a", "b"], rel, 1) == 0 and recall_at(["x", "a", "b"], rel, 3) == pytest.approx(2 / 3)
    assert mrr_at(["x", "y", "b"], rel) == pytest.approx(1 / 3) and mrr_at(["x"] * 20, rel) == 0
    assert average_precision(["a", "x", "b"], rel) == pytest.approx((1 + 2 / 3) / 3)
    assert top_answer_at(["b", "a"], rel, 1) == 0 and top_answer_at(["b", "a"], rel, 3) == 1


def test_separation_rewards_a_model_that_tells_answers_from_lookalikes():
    cases = [{"id": "q", "relevant": {"good": 3}, "hard_negatives": ["near"]}]
    docs = {"good": [1.0, 0.0], "near": [0.6, 0.8], "far": [0.0, 1.0]}
    out = separation(cases, {"q": [1.0, 0.0]}, docs)
    assert out["margin"] == pytest.approx(0.4) and out["auc"] == 1.0


def test_hybrid_counts_the_identifier_ranking_twice_like_search():
    assert hybrid(["k"], ["i"], ["v"]) == [d for d, _ in rrf(["k"], ["i"], ["i"], ["v"])]
    assert hybrid(["k", "i"], ["i"], None)[0] == "i"


def test_identifier_ranking_matches_case_insensitively_and_counts_tokens():
    docs = {"a": "OOMKilled exit code 137", "b": "oomkilled", "c": "nothing"}
    assert identifier_ranking(["OOMKilled", "137"], docs) == ["a", "b"]


def test_bm25_finds_stems_and_ignores_stopwords():
    bm = BM25({"a": "Restarting containers restart", "b": "the and of"})
    assert bm.rank("restarted") == ["a"] and bm.rank("the") == []


# ---- dataset ----------------------------------------------------------------------------------

def test_the_dataset_holds_together():
    ids = [d["id"] for d in DATA["documents"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 80
    cases = [c["id"] for c in DATA["cases"]]
    assert len(cases) == len(set(cases)) and len(cases) >= 80
    for c in DATA["cases"]:
        assert set(c["relevant"]) <= set(ids) and set(c.get("hard_negatives", [])) <= set(ids), c["id"]
        assert 3 in c["relevant"].values(), f"{c['id']} has no document that answers it"
        assert not set(c["relevant"]) & set(c.get("hard_negatives", [])), c["id"]
        assert c["category"] in DATA["categories"] and c["type"] in DATA["query_types"], c["id"]
    per_category = Counter(c["category"] for c in DATA["cases"])
    assert all(per_category[k] >= 5 for k in DATA["categories"]), per_category
    assert {d["category"] for d in DATA["documents"]} == set(DATA["categories"])


def test_the_brief_s_three_questions_are_in_it():
    by_query = {c["query"]: c for c in DATA["cases"]}
    assert {"k8s-crashloop-runbook", "k8s-restart-policy-doc", "inc-oomkilled-payments"} <= set(by_query["Why is my pod restarting?"]["relevant"])
    assert {"prom-latency-runbook", "grafana-api-latency-dashboard", "inc-latency-db-pool"} <= set(by_query["Prometheus shows high API latency"]["relevant"])
    assert {"tf-state-lock-runbook", "tf-vpc-module-doc", "inc-terraform-deploy-failure"} <= set(by_query["Terraform deployment failed"]["relevant"])


def test_the_dataset_says_it_is_synthetic_and_names_no_real_secret():
    raw = embedding_eval.DATASET.read_text()
    assert "synthetic" in raw.lower()
    assert not re.search(r"AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[bp]-", raw)


# ---- the evaluation ---------------------------------------------------------------------------

class Hashing:
    """A deterministic stand-in model: bag of hashed words, so related text really is closer."""
    def __init__(self, size=64, scale=1.0):
        self.size, self.scale, self.calls = size, scale, 0

    async def embed(self, texts):
        self.calls += len(texts)
        out = []
        for t in texts:
            v = [0.0] * self.size
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % self.size] += self.scale
            v[0] += 0.01
            out.append(v)
        return out


class Offline(service.emb.Embedder):
    async def _call(self, inputs, **kwargs):
        return service.emb.normalize(await self.provider.embed(inputs))


def config(name="hash", **options):
    return LLMConfig(name=name, purpose="embedding", provider="openai", model=name, embedding_options=options)


async def test_an_evaluation_measures_every_mode_and_keeps_vectors_out_of_the_result():
    got = await embedding_eval.evaluate(Offline(config(), Hashing()), DATA)
    assert set(got["modes"]) >= {"keyword", "vector", "hybrid", "keyword+expansion", "hybrid+expansion"}
    assert got["dimensions"] == 64 and not got["vector_problems"]
    assert 0 < got["modes"]["hybrid"]["overall"]["ndcg@10"] <= 1 and set(got["modes"]["hybrid"]["by_category"]) == set(DATA["categories"])
    assert got["latency"]["query_p95_ms"] >= 0 and "rankings" in got and "rankings" not in embedding_eval._public(got)


async def test_gates_decide_on_measured_differences():
    good = await embedding_eval.evaluate(Offline(config(), Hashing(256)), DATA)
    alone = embedding_eval.decide(good, None, DATA, 0.02)
    assert {g["name"]: g["state"] for g in alone}["not_worse_than_serving"] == "not_applicable"
    worse = {**good, "modes": {**good["modes"], "hybrid": {**good["modes"]["hybrid"], "overall": {**good["modes"]["hybrid"]["overall"], "ndcg@10": 0.1, "recall@5": 0.1}}}}
    gates = {g["name"]: g for g in embedding_eval.decide(worse, good, DATA, 0.02)}
    assert gates["not_worse_than_serving"]["state"] == "failed" and gates["vector_adds_value"]["state"] == "failed"
    same = {g["name"]: g for g in embedding_eval.decide(good, good, DATA, 0.02)}
    assert same["not_worse_than_serving"]["state"] == "passed"
    slow = {**good, "latency": {**good["latency"], "query_p95_ms": 5000}}
    assert {g["name"]: g["state"] for g in embedding_eval.decide(slow, None, DATA, 0.02)}["query_latency"] == "failed"
    broken = {**good, "vector_problems": ["vector sizes differ: [3, 4]"]}
    assert {g["name"]: g["state"] for g in embedding_eval.decide(broken, None, DATA, 0.02)}["consistent_vectors"] == "failed"


@pytest.fixture
async def clean():
    async def wipe():
        async with session_scope() as db:
            await db.execute(delete(EvalRun).where(EvalRun.kind == "embedding"))
            await db.execute(delete(EmbeddingIndex).where(EmbeddingIndex.backend == "postgres"))
            await db.execute(delete(LLMConfig).where(LLMConfig.purpose == "embedding"))
    await wipe()
    yield
    await wipe()


async def test_a_passing_run_is_what_lets_a_model_replace_the_serving_one(clean, monkeypatch):
    monkeypatch.setattr(service, "provider_for", lambda cfg: Hashing(int(cfg.model.rsplit("-", 1)[-1])))

    async def unmetered(provider, cfg, texts, **kwargs):
        return await provider.embed(texts)
    monkeypatch.setattr(service.emb.budget, "embed", unmetered)
    async with session_scope() as db:
        serving = LLMConfig(name=f"m-{uuid.uuid4().hex[:4]}", purpose="embedding", provider="openai", model="hash-64", is_active=True)
        candidate = LLMConfig(name=f"c-{uuid.uuid4().hex[:4]}", purpose="embedding", provider="openai", model="hash-256", embedding_options={"query_prefix": "q "})
        db.add_all([serving, candidate])
        await db.flush()
        db.add(EmbeddingIndex(backend="postgres", llm_config_id=serving.id, model_key=service.model_key(serving), dimensions=64, index_name="moks-chunks-eval", status="active"))
        cand_id = candidate.id
    monkeypatch.setattr(service, "backend_name", lambda: "postgres")
    async with session_scope() as db:
        assert not (await service.replacement_check(db, await db.get(LLMConfig, cand_id)))["allowed"]
    rid = await runner.start("embedding", "rules", None, cand_id)
    out = await runner.execute(rid)
    async with session_scope() as db:
        run = await db.get(EvalRun, rid)
        assert run.kind == "embedding" and run.status == "completed" and run.versions["candidate"]["config_id"] == str(cand_id)
        assert run.versions["metrics"]["serving"] is not None and len(run.results) == len(DATA["cases"])
        check = await service.replacement_check(db, await db.get(LLMConfig, cand_id))
    names = {g["name"]: g["state"] for g in run.gates}
    assert names.keys() >= {"vector_adds_value", "not_worse_than_serving", "exact_identifiers", "consistent_vectors", "query_latency"}
    assert check["allowed"] == out["gates_passed"] and (check["evaluation"]["id"] == str(rid))


async def test_with_no_model_named_or_active_the_run_says_so(clean):
    rid = await runner.start("embedding", "rules")
    out = await runner.execute(rid)
    assert out["gates_passed"] is False
    async with session_scope() as db:
        assert (await db.get(EvalRun, rid)).gates[0]["name"] == "candidate"


async def test_the_dataset_is_listed_with_its_kind():
    assert {"name": "embedding", "cases": len(DATA["cases"]), "kind": "embedding"} in runner.list_datasets()
