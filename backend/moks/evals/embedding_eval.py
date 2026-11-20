"""Does an embedding model earn its place? Measured on MOKS's SRE questions (datasets/embedding.yaml).

A candidate embedding configuration and the model serving search today embed the same synthetic documents and
questions through the production path (rag/embeddings.py, metered by the budget), and both are ranked the way search
ranks: keyword only, vector only, and hybrid. Nothing is indexed and no operational document is read; the vectors
live in memory for the length of the run.

A pass is what lets the candidate replace the serving model (service.replacement_check). Gates:
- vector_adds_value: hybrid ranking beats keyword-only ranking. A model that does not is not worth its cost.
- not_worse_than_serving: nDCG@10 and Recall@5 (hybrid) within the tolerance of the serving model's.
- exact_identifiers: on identifier questions the candidate never pushes the exact match out of the top three.
- consistent_vectors: one size, unit length, finite.
- query_latency: p95 for one query within the time search waits for an embedding.
"""
from __future__ import annotations

import math
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from ..rag.query import entities, expansion_terms, normalize_query
from . import gates
from .retrieval_metrics import BM25, case_measures, grouped, hybrid, identifier_ranking, separation, summarize, vector_ranking

DATASET = Path(__file__).parent / "datasets" / "embedding.yaml"
POOL = 32  # service._search's candidate pool for the default k of 8: max(k * 4, 20)
BATCH = 32
SEARCH_WAIT_MS = 2000  # service._query_vector waits this long for a query embedding before searching keyword-only
MODES = ("keyword", "vector", "hybrid")


def load(path: Path = DATASET) -> dict:
    return yaml.safe_load(path.read_text())


def percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else 0.0


async def embed(embedder: Any, data: dict, repeats: int = 1) -> dict[str, Any]:
    """Document vectors in batches and query vectors one at a time, as indexing and search send them. Timings are
    wall clock around each call, so they include the provider's network and queueing."""
    docs = data["documents"]
    dvecs: dict[str, list[float]] = {}
    started = time.perf_counter()
    for i in range(0, len(docs), BATCH):
        batch = docs[i:i + BATCH]
        for d, v in zip(batch, await embedder.documents([d["text"] for d in batch], purpose="evaluation")):
            dvecs[d["id"]] = v
    doc_seconds = time.perf_counter() - started
    qvecs: dict[str, list[float]] = {}
    latencies: list[float] = []
    for _ in range(repeats):
        for c in data["cases"]:
            t = time.perf_counter()
            qvecs[c["id"]] = await embedder.query(normalize_query(c["query"]), purpose="evaluation", wait_seconds=30)
            latencies.append((time.perf_counter() - t) * 1000)
    return {"documents": dvecs, "queries": qvecs, "doc_seconds": doc_seconds, "query_ms": latencies}


def rank(data: dict, qvecs: dict[str, list[float]] | None, dvecs: dict[str, list[float]] | None, expansion: bool = False) -> dict[str, dict[str, list[str]]]:
    """Each case's ranking per mode. keyword is what search serves without vectors (keyword plus exact identifiers)."""
    from ..rag.service import _exact_tokens  # the production token choice, so identifier ranking is the real one

    texts = {d["id"]: d["text"] for d in data["documents"]}
    with_path = {d["id"]: f"{d['path']}\n{d['text']}" for d in data["documents"]}
    bm25 = BM25(texts)
    out: dict[str, dict[str, list[str]]] = {}
    for c in data["cases"]:
        q = normalize_query(c["query"])
        kw = bm25.rank(q, POOL)
        ids = identifier_ranking(_exact_tokens(q, entities(q)), with_path)[:POOL]
        modes = {"keyword": hybrid(kw, ids, None)}
        terms = expansion_terms(q) if expansion else []
        extra = [bm25.rank(" ".join(terms), POOL)] if terms else []  # as service._rankings: one more ranking, only when there are terms
        if expansion:
            modes["keyword+expansion"] = hybrid(kw, ids, None, extra)
        if qvecs is not None and dvecs is not None:
            vec = vector_ranking(qvecs[c["id"]], dvecs)[:POOL]
            modes |= {"vector": vec, "hybrid": hybrid(kw, ids, vec)}
            if expansion:
                modes["hybrid+expansion"] = hybrid(kw, ids, vec, extra)
        out[c["id"]] = modes
    return out


def score(data: dict, rankings: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    cases = data["cases"]
    modes = list(next(iter(rankings.values())).keys())
    out: dict[str, Any] = {}
    for mode in modes:
        measured = [case_measures(rankings[c["id"]][mode], c["relevant"]) for c in cases]
        out[mode] = {"overall": summarize(measured), "by_category": grouped(cases, measured, "category"), "by_type": grouped(cases, measured, "type"),
                     "per_case": {c["id"]: round(m["ndcg@10"], 3) for c, m in zip(cases, measured)}}
    return out


def vector_health(vecs: list[list[float]]) -> list[str]:
    problems = []
    sizes = {len(v) for v in vecs}
    if len(sizes) != 1:
        problems.append(f"vector sizes differ: {sorted(sizes)}")
    for i, v in enumerate(vecs):
        n = math.sqrt(sum(x * x for x in v))
        if not math.isfinite(n) or abs(n - 1) > 1e-3:
            problems.append(f"vector {i} has norm {n}")
            break
    return problems


async def evaluate(embedder: Any, data: dict, expansion: bool = True) -> dict[str, Any]:
    got = await embed(embedder, data)
    rankings = rank(data, got["queries"], got["documents"], expansion)
    scored = score(data, rankings)
    all_vecs = list(got["documents"].values()) + list(got["queries"].values())
    return {"model": embedder.key, "spec": embedder.spec.describe(), "dimensions": len(all_vecs[0]) if all_vecs else None, "modes": scored,
            "separation": separation(data["cases"], got["queries"], got["documents"]), "vector_problems": vector_health(all_vecs),
            "latency": {"query_p50_ms": percentile(got["query_ms"], 0.5), "query_p95_ms": percentile(got["query_ms"], 0.95), "query_median_ms": round(median(got["query_ms"]), 1) if got["query_ms"] else 0,
                        "documents_per_second": round(len(got["documents"]) / got["doc_seconds"], 2) if got["doc_seconds"] else None},
            "rankings": rankings}


def decide(candidate: dict, serving: dict | None, data: dict, tolerance: float) -> list[dict]:
    """Deterministic gates on what was measured. No gate passes on missing evidence."""
    hyb, kw = candidate["modes"]["hybrid"]["overall"], candidate["modes"]["keyword"]["overall"]
    ident = [c for c in data["cases"] if c.get("type") == "identifier"]
    lost = [c["id"] for c in ident if not set(candidate["rankings"][c["id"]]["hybrid"][:3]) & {d for d, g in c["relevant"].items() if g == 3}
            and set(candidate["rankings"][c["id"]]["keyword"][:3]) & {d for d, g in c["relevant"].items() if g == 3}]
    out = [_measured("vector_adds_value", hyb["ndcg@10"] > kw["ndcg@10"], {"hybrid_ndcg@10": hyb["ndcg@10"], "keyword_ndcg@10": kw["ndcg@10"]}),
           gates._gate("exact_identifiers", lost, len(ident)),
           gates._gate("consistent_vectors", candidate["vector_problems"], 1),
           _measured("query_latency", candidate["latency"]["query_p95_ms"] <= SEARCH_WAIT_MS, {"p95_ms": candidate["latency"]["query_p95_ms"], "limit_ms": SEARCH_WAIT_MS})]
    if serving is None:
        out.append({"name": "not_worse_than_serving", "state": "not_applicable", "passed": False, "checked": 0, "failures": [], "evidence": "live",
                    "reason": "search has no vectors today, so there is no model to compare with"})
    else:
        base = serving["modes"]["hybrid"]["overall"]
        ok = hyb["ndcg@10"] >= base["ndcg@10"] - tolerance and hyb["recall@5"] >= base["recall@5"] - tolerance
        out.append(_measured("not_worse_than_serving", ok, {"candidate_ndcg@10": hyb["ndcg@10"], "serving_ndcg@10": base["ndcg@10"],
                                                            "candidate_recall@5": hyb["recall@5"], "serving_recall@5": base["recall@5"], "tolerance": tolerance}))
    return out


def _measured(name: str, ok: bool, value: Any) -> dict:
    return {"name": name, "state": "passed" if ok else "failed", "passed": bool(ok), "checked": 1, "failures": [] if ok else [str(value)], "value": value, "evidence": "live"}


def _public(result: dict) -> dict:
    """What a run keeps: measures, not rankings of every document."""
    return {k: v for k, v in result.items() if k != "rankings"}


async def run(data: dict, run_id: uuid.UUID) -> dict[str, Any]:
    """The evaluation job: the candidate named when the run started (or the active model) against the model that
    serves search now. Calls each model once per document and question, through its own budget."""
    from ..config import get_settings
    from ..db import session_scope
    from ..models import EvalRun, LLMConfig
    from ..rag import service

    async with session_scope() as db:
        record = await db.get(EvalRun, run_id)
        wanted = (record.versions or {}).get("candidate_config_id")
        candidate = await db.get(LLMConfig, uuid.UUID(wanted)) if wanted else await service.embedding_config(db)
        idx = await service.active_index(db)
        serving = await db.get(LLMConfig, idx.llm_config_id) if idx and idx.dimensions and idx.llm_config_id else None
        if serving is not None and service.model_key(serving) != idx.model_key:
            serving = None  # changed since the index was built: it no longer describes what search does
    if candidate is None or candidate.purpose != "embedding":
        return {"results": [], "gates": [gates._gate("candidate", ["no embedding model to evaluate"], 1)], "metrics": {"note": "no embedding model was named and none is active"}, "score": 0}
    cand = await evaluate(service._embedder(candidate), data)
    base = None
    if serving is not None and service.model_key(serving) != service.model_key(candidate):
        base = await evaluate(service._embedder(serving), data)
    out_gates = decide(cand, base, data, get_settings().rag_embedding_eval_tolerance)
    rows = [{"case": c["id"], "category": c.get("category"), "type": c.get("type"), "query": c["query"],
             "candidate": cand["rankings"][c["id"]]["hybrid"][:5], "candidate_ndcg@10": cand["modes"]["hybrid"]["per_case"][c["id"]],
             **({"serving": base["rankings"][c["id"]]["hybrid"][:5], "serving_ndcg@10": base["modes"]["hybrid"]["per_case"][c["id"]]} if base else {})}
            for c in data["cases"]]
    metrics = {"model": cand["model"], "candidate": _public(cand), "serving": _public(base) if base else None,
               "recommendation": "eligible to replace the serving model" if gates.gates_ok(out_gates) else "keep the serving model",
               "cases": len(rows), "documents": len(data["documents"])}
    return {"results": rows, "gates": out_gates, "metrics": metrics, "score": cand["modes"]["hybrid"]["overall"]["ndcg@10"],
            "candidate": {"key": cand["model"], "config_id": str(candidate.id), "name": candidate.name}}
