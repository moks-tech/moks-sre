"""Offline embedding benchmark: quality, latency, throughput, CPU and memory for candidate models, on MOKS's SRE
dataset, through the same Embedder and providers production uses (only the budget meter is left out).

    python -m moks.evals.embedding_bench candidates.yaml --out docs/benchmarks/embeddings

candidates.yaml:

    threads: 4                      # torch threads per candidate; mk4 pods get about 4 vCPUs
    reference: bge-m3               # the candidate the others are tested against (the model serving today)
    offline: false                  # true: local directories only, nothing downloaded
    candidates:
      - name: bge-small-en-v1.5
        provider: sentence_transformers   # or ollama, huggingface, openai, ... with endpoint and credential_ref
        model: /models/BAAI__bge-small-en-v1.5
        model_id: BAAI/bge-small-en-v1.5  # what the report calls it
        revision: 5c38ec7c...              # recorded, and part of the model key
        query_prefix: "Represent this sentence for searching relevant passages: "

Each candidate runs in its own process, so peak memory is that model's alone. Writes results.json and report.md.
Numbers from a laptop are relative: the report says which machine measured them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import resource
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from . import embedding_eval
from .retrieval_metrics import case_measures, summarize

BOOTSTRAP_SAMPLES = 10_000
PRODUCTION_CHUNK_CHARS = 1300  # ~325 tokens, the chunk size rag/chunking.py produces with rag_chunk_tokens 350
THROUGHPUT_CHUNKS = 64
QUERY_REPEATS = 3


def _rss_mb() -> float:
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale


def _cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def config_of(c: dict) -> SimpleNamespace:
    """A stand-in for an LLMConfig row: what provider_for and the Embedder read."""
    options = {k: c[k] for k in ("query_prefix", "document_prefix", "max_input_chars", "revision") if c.get(k) not in (None, "")}
    return SimpleNamespace(name=c["name"], purpose="embedding", provider=c["provider"], model=c["model"], endpoint=c.get("endpoint"), region=c.get("region"),
                           credential_ref=c.get("credential_ref") or {"type": "none"}, timeout_seconds=c.get("timeout_seconds", 600), embedding_dimensions=c.get("dimensions"),
                           embedding_options=options, input_price_per_mtok=0, output_price_per_mtok=0, max_concurrency=1, id=None)


def offline_embedder(cfg: SimpleNamespace):
    from ..llm.providers import provider_for
    from ..rag.embeddings import Embedder, normalize

    class Offline(Embedder):
        """The production Embedder without the budget meter, which needs a database."""

        async def _call(self, inputs: list[str], **kwargs: Any) -> list[list[float]]:
            return normalize(await self.provider.embed(inputs))

    return Offline(cfg, provider_for(cfg))


def production_chunks(data: dict) -> list[str]:
    """Chunk-sized inputs made of the dataset's own text, for throughput at the length production sends."""
    text = "\n".join(d["text"] for d in data["documents"])
    return [text[(i * 997) % len(text):][:PRODUCTION_CHUNK_CHARS] for i in range(THROUGHPUT_CHUNKS)]


async def measure(c: dict, threads: int, data: dict) -> dict[str, Any]:
    if c["provider"] == "sentence_transformers":
        import torch

        torch.set_num_threads(threads)
    base_rss = _rss_mb()
    cfg = config_of(c)
    embedder = offline_embedder(cfg)
    t = time.perf_counter()
    if hasattr(embedder.provider, "_model"):
        await embedder.provider._model()  # loading, measured apart from the first inference
    load_s = time.perf_counter() - t
    t = time.perf_counter()
    await embedder.query("warm up the model before measuring")
    cold_ms = (time.perf_counter() - t) * 1000
    result = await embedding_eval.evaluate(embedder, data)
    lat = []
    for _ in range(QUERY_REPEATS):
        for case in data["cases"]:
            t = time.perf_counter()
            await embedder.query(case["query"])
            lat.append((time.perf_counter() - t) * 1000)
    chunks = production_chunks(data)
    cpu0, t = _cpu(), time.perf_counter()
    for i in range(0, len(chunks), embedding_eval.BATCH):
        await embedder.documents(chunks[i:i + embedding_eval.BATCH])
    wall, cpu = time.perf_counter() - t, _cpu() - cpu0
    dims = result["dimensions"] or 0
    result.pop("rankings")
    return {**result, **c, "model_key": result["model"], "threads": threads, "load_seconds": round(load_s, 2), "cold_query_ms": round(cold_ms, 1),
            "query_latency_ms": {"p50": embedding_eval.percentile(lat, 0.5), "p95": embedding_eval.percentile(lat, 0.95), "samples": len(lat)},
            "throughput": {"chunk_chars": PRODUCTION_CHUNK_CHARS, "chunks": len(chunks), "chunks_per_second": round(len(chunks) / wall, 2),
                           "cpu_seconds_per_chunk": round(cpu / len(chunks), 4), "batch": embedding_eval.BATCH},
            "memory_mb": {"process_peak": round(_rss_mb(), 1), "before_model": round(base_rss, 1), "model_and_inference": round(_rss_mb() - base_rss, 1)},
            "storage_bytes_per_vector": 4 * dims + 8}


def paired_bootstrap(a: dict[str, float], b: dict[str, float], samples: int = BOOTSTRAP_SAMPLES, seed: int = 7) -> dict[str, float]:
    """Mean per-question difference a - b with a 95% interval, resampling questions. An interval that contains 0
    means this dataset cannot tell the two apart."""
    ids = sorted(set(a) & set(b))
    diffs = [a[i] - b[i] for i in ids]
    rnd = random.Random(seed)
    means = sorted(sum(diffs[rnd.randrange(len(diffs))] for _ in diffs) / len(diffs) for _ in range(samples))
    lo, hi = means[int(0.025 * samples)], means[int(0.975 * samples)]
    return {"mean": round(sum(diffs) / len(diffs), 4), "low": round(lo, 4), "high": round(hi, 4), "significant": not (lo <= 0 <= hi)}


def significance(results: list[dict], baseline: dict, reference: str | None) -> dict[str, Any]:
    """Each candidate against keyword-only search and against the reference (the model serving today)."""
    ref = next((r for r in results if r["name"] == reference), None)
    out: dict[str, Any] = {}
    for r in results:
        row = {"hybrid_vs_keyword": paired_bootstrap(r["modes"]["hybrid"]["per_case"], baseline["keyword"]["per_case"])}
        if ref is not None and r is not ref:
            row |= {f"{mode}_vs_reference": paired_bootstrap(r["modes"][mode]["per_case"], ref["modes"][mode]["per_case"]) for mode in ("hybrid", "vector")}
        out[r["name"]] = row
    return {"reference": reference, "metric": "ndcg@10", "samples": BOOTSTRAP_SAMPLES, "candidates": out}


def keyword_baseline(data: dict) -> dict[str, Any]:
    """What search serves with no vectors: keyword plus exact identifiers. The same for every model."""
    rankings = embedding_eval.rank(data, None, None, expansion=True)
    return embedding_eval.score(data, rankings)


def run_one(spec: str) -> None:
    job = json.loads(spec)
    data = embedding_eval.load(Path(job["dataset"])) if job.get("dataset") else embedding_eval.load()
    print(json.dumps(asyncio.run(measure(job["candidate"], job["threads"], data))))


def markdown(results: list[dict], baseline: dict, host: dict, data: dict, sig: dict | None = None) -> str:
    def f(x: float) -> str:
        return f"{x:.3f}"

    lines = [f"# Embedding benchmark ({host['date']})", "",
             f"Dataset `{data['name']}` v{data.get('version', 1)}: {len(data['documents'])} synthetic SRE documents, {len(data['cases'])} questions with graded relevance.",
             f"Measured on {host['machine']} ({host['platform']}), torch threads per model: {results[0]['threads'] if results else '-'}. "
             "Latency and throughput are relative to this machine; quality numbers are not.", "",
             "## Quality (hybrid = keyword + exact identifiers + vector, fused as search does)", "",
             "| Model | Dims | nDCG@10 hybrid | nDCG@10 vector | Recall@5 hybrid | MRR@10 hybrid | Answer@1 hybrid | Separation AUC | Margin |",
             "|---|---|---|---|---|---|---|---|---|"]
    kw = baseline["keyword"]["overall"]
    lines.append(f"| keyword only (no model) | - | {f(kw['ndcg@10'])} | - | {f(kw['recall@5'])} | {f(kw['mrr@10'])} | {f(kw['answer@1'])} | - | - |")
    for r in results:
        h, v = r["modes"]["hybrid"]["overall"], r["modes"]["vector"]["overall"]
        lines.append(f"| {r['name']} | {r['dimensions']} | {f(h['ndcg@10'])} | {f(v['ndcg@10'])} | {f(h['recall@5'])} | {f(h['mrr@10'])} | {f(h['answer@1'])} | {f(r['separation']['auc'])} | {f(r['separation']['margin'])} |")
    lines += ["", "## Cost to run", "", "| Model | Load s | Query p50 ms | Query p95 ms | Chunks/s (1300 chars) | CPU s/chunk | Peak RSS MB | Bytes/vector |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['load_seconds']} | {r['query_latency_ms']['p50']} | {r['query_latency_ms']['p95']} | {r['throughput']['chunks_per_second']} | "
                     f"{r['throughput']['cpu_seconds_per_chunk']} | {r['memory_mb']['process_peak']} | {r['storage_bytes_per_vector']} |")
    cats = sorted(baseline["keyword"]["by_category"])
    lines += ["", "## nDCG@10 (hybrid) by category", "", "| Model | " + " | ".join(cats) + " |", "|---|" + "---|" * len(cats)]
    lines.append("| keyword only | " + " | ".join(f(baseline["keyword"]["by_category"][c]) for c in cats) + " |")
    for r in results:
        lines.append(f"| {r['name']} | " + " | ".join(f(r["modes"]["hybrid"]["by_category"][c]) for c in cats) + " |")
    types = sorted(baseline["keyword"]["by_type"])
    lines += ["", "## nDCG@10 (hybrid) by question type", "", "| Model | " + " | ".join(types) + " |", "|---|" + "---|" * len(types)]
    lines.append("| keyword only | " + " | ".join(f(baseline["keyword"]["by_type"][t]) for t in types) + " |")
    for r in results:
        lines.append(f"| {r['name']} | " + " | ".join(f(r["modes"]["hybrid"]["by_type"][t]) for t in types) + " |")
    if sig and sig.get("candidates"):
        def ci(x: dict | None) -> str:
            return "-" if not x else f"{x['mean']:+.3f} [{x['low']:+.3f}, {x['high']:+.3f}]{' *' if x['significant'] else ''}"
        lines += ["", f"## Is the difference real? (paired bootstrap over questions, {sig['samples']} samples, nDCG@10, 95% interval; * = significant)", "",
                  f"| Model | hybrid vs keyword only | hybrid vs {sig['reference']} | vector vs {sig['reference']} |", "|---|---|---|---|"]
        for name, row in sig["candidates"].items():
            lines.append(f"| {name} | {ci(row.get('hybrid_vs_keyword'))} | {ci(row.get('hybrid_vs_reference'))} | {ci(row.get('vector_vs_reference'))} |")
    if any("hybrid+expansion" in r["modes"] for r in results):
        lines += ["", "## Query expansion (nDCG@10)", "", "| Model | hybrid | hybrid + expansion |", "|---|---|---|"]
        k2 = baseline.get("keyword+expansion", {}).get("overall", {})
        lines.append(f"| keyword only | {f(kw['ndcg@10'])} | {f(k2.get('ndcg@10', 0))} |")
        for r in results:
            lines.append(f"| {r['name']} | {f(r['modes']['hybrid']['overall']['ndcg@10'])} | {f(r['modes'].get('hybrid+expansion', {}).get('overall', {}).get('ndcg@10', 0))} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("candidates", nargs="?")
    ap.add_argument("--out", default="embedding-benchmark")
    ap.add_argument("--dataset", default="")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    ap.add_argument("--report-only", help="write report.md again from an earlier results.json")
    a = ap.parse_args(argv)
    if a.one:
        return run_one(a.one)
    if a.report_only:
        saved = json.loads(Path(a.report_only).read_text())
        data = embedding_eval.load(Path(a.dataset)) if a.dataset else embedding_eval.load()
        report = markdown(saved["candidates"], saved["keyword_baseline"], saved["host"], data, saved.get("significance"))
        Path(a.report_only).with_name("report.md").write_text(report)
        print(report)
        return None
    plan = yaml.safe_load(Path(a.candidates).read_text())
    data = embedding_eval.load(Path(a.dataset)) if a.dataset else embedding_eval.load()
    threads = int(plan.get("threads", 4))
    env = {**os.environ, "OMP_NUM_THREADS": str(threads), "TOKENIZERS_PARALLELISM": "false", "HF_HUB_DISABLE_TELEMETRY": "1"}
    if plan.get("offline"):  # local model directories only: nothing is downloaded
        env["HF_HUB_OFFLINE"] = "1"
    results = []
    for c in plan["candidates"]:
        print(f"measuring {c['name']} ...", file=sys.stderr, flush=True)
        job = json.dumps({"candidate": c, "threads": threads, "dataset": a.dataset})
        done = subprocess.run([sys.executable, "-m", "moks.evals.embedding_bench", "--one", job], capture_output=True, text=True, env=env)
        if done.returncode != 0:
            print(done.stderr[-2000:], file=sys.stderr)
            raise SystemExit(f"{c['name']} failed")
        results.append(scrub(json.loads(done.stdout.strip().splitlines()[-1])))
    baseline = keyword_baseline(data)
    sig = significance(results, baseline, plan.get("reference"))
    host = {"machine": platform.machine(), "platform": platform.platform(), "processor": platform.processor(), "python": platform.python_version(),
            "date": time.strftime("%Y-%m-%d")}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"host": host, "dataset": {"name": data["name"], "version": data.get("version"), "documents": len(data["documents"]), "cases": len(data["cases"])},
                                                   "keyword_baseline": baseline, "significance": sig, "classifier": classifier_accuracy(data), "candidates": results}, indent=1))
    (out / "report.md").write_text(markdown(results, baseline, host, data, sig))
    print((out / "report.md").read_text())


def scrub(result: dict) -> dict:
    """A local model directory names the machine and the person who ran this; a report names the model."""
    path, model_id = result.get("model"), result.get("model_id")
    if not path or not model_id or path == model_id:
        return result
    return json.loads(json.dumps(result).replace(json.dumps(path)[1:-1], model_id))


def classifier_accuracy(data: dict) -> dict[str, Any]:
    """How often rag.query.classify agrees with the dataset's labelled question types."""
    from ..rag.query import classify, entities, normalize_query
    from ..rag.service import identifiers

    rows = []
    for c in data["cases"]:
        q = normalize_query(c["query"])
        got = classify(q, entities(q), identifiers(q))["type"]
        rows.append({"case": c["id"], "label": c["type"], "got": got})
    hits = sum(r["label"] == r["got"] for r in rows)
    return {"accuracy": round(hits / len(rows), 3), "misses": [r for r in rows if r["label"] != r["got"]]}


__all__ = ["main", "measure", "keyword_baseline", "classifier_accuracy", "case_measures", "summarize"]

if __name__ == "__main__":
    main()
