"""Retrieval measures and an in-memory copy of MOKS's hybrid ranking, for comparing embedding models.

Pure functions: no database, no model, no network. The embedding evaluation (embedding_eval.py) and the offline
benchmark (embedding_bench.py) both score with these, so a number in a report and a number behind an activation
gate mean the same thing.

The hybrid ranking mirrors rag/service.py: a keyword ranking, the exact-identifier ranking counted twice, and the
vector ranking, fused by reciprocal rank (k=60). The keyword ranking here is BM25 over a light English stemmer,
an approximation of PostgreSQL's ts_rank_cd; the identifier and fusion steps are the production code itself.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from statistics import mean
from typing import Iterable

from ..rag.backends import rrf

KS = (1, 3, 5, 10)


# ---- measures ---------------------------------------------------------------------------------

def recall_at(ranked: list[str], relevant: dict[str, int], k: int) -> float:
    return len(set(ranked[:k]) & set(relevant)) / len(relevant) if relevant else 0.0


def precision_at(ranked: list[str], relevant: dict[str, int], k: int) -> float:
    return len(set(ranked[:k]) & set(relevant)) / k


def mrr_at(ranked: list[str], relevant: dict[str, int], k: int = 10) -> float:
    return next((1 / (i + 1) for i, d in enumerate(ranked[:k]) if d in relevant), 0.0)


def ndcg_at(ranked: list[str], relevant: dict[str, int], k: int = 10) -> float:
    """Graded: gain 2^grade - 1, so a document that answers (3) counts far more than background (1)."""
    dcg = sum((2 ** relevant.get(d, 0) - 1) / math.log2(i + 2) for i, d in enumerate(ranked[:k]))
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def average_precision(ranked: list[str], relevant: dict[str, int]) -> float:
    hits, total = 0, 0.0
    for i, d in enumerate(ranked):
        if d in relevant:
            hits += 1
            total += hits / (i + 1)
    return total / len(relevant) if relevant else 0.0


def top_answer_at(ranked: list[str], relevant: dict[str, int], k: int = 1) -> float:
    """Whether a best-graded document is in the first k: did the answer itself come first."""
    best = max(relevant.values(), default=0)
    return float(any(relevant.get(d) == best for d in ranked[:k]))


def case_measures(ranked: list[str], relevant: dict[str, int]) -> dict[str, float]:
    out = {f"recall@{k}": recall_at(ranked, relevant, k) for k in KS}
    out |= {"precision@5": precision_at(ranked, relevant, 5), "mrr@10": mrr_at(ranked, relevant), "ndcg@10": ndcg_at(ranked, relevant),
            "map": average_precision(ranked, relevant), "answer@1": top_answer_at(ranked, relevant, 1), "answer@3": top_answer_at(ranked, relevant, 3)}
    return out


def summarize(rows: list[dict[str, float]]) -> dict[str, float]:
    keys = rows[0].keys() if rows else []
    return {k: round(mean(r[k] for r in rows), 4) for k in keys}


def grouped(cases: list[dict], measures: list[dict[str, float]], by: str, metric: str = "ndcg@10") -> dict[str, float]:
    groups: dict[str, list[float]] = {}
    for case, m in zip(cases, measures):
        groups.setdefault(case.get(by, "other"), []).append(m[metric])
    return {g: round(mean(v), 4) for g, v in sorted(groups.items())}


# ---- vectors ----------------------------------------------------------------------------------

def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def vector_ranking(query: list[float], docs: dict[str, list[float]]) -> list[str]:
    """Vectors are unit length (embeddings.normalize), so the dot product is the cosine pgvector ranks by."""
    return [d for d, _ in sorted(((d, dot(query, v)) for d, v in docs.items()), key=lambda p: (-p[1], p[0]))]


def separation(cases: list[dict], qvecs: dict[str, list[float]], dvecs: dict[str, list[float]]) -> dict[str, float]:
    """How far apart a model puts what answers a question and what does not, in cosine. `margin` is the mean
    similarity to graded-3 documents minus the mean to hard negatives; `auc` is the chance that a random relevant
    document scores above a random irrelevant one. Both are independent of any ranking cut-off."""
    rel, neg, other, pairs, wins = [], [], [], 0, 0.0
    for c in cases:
        q = qvecs[c["id"]]
        best = [dot(q, dvecs[d]) for d, g in c["relevant"].items() if g == 3]
        hard = [dot(q, dvecs[d]) for d in c.get("hard_negatives", [])]
        rest = [dot(q, v) for d, v in dvecs.items() if d not in c["relevant"]]
        rel += best
        neg += hard
        other += rest
        for r in (dot(q, dvecs[d]) for d in c["relevant"]):
            for o in rest:
                pairs += 1
                wins += 1.0 if r > o else 0.5 if r == o else 0.0
    return {"relevant_cosine": round(mean(rel), 4) if rel else 0.0, "hard_negative_cosine": round(mean(neg), 4) if neg else 0.0,
            "irrelevant_cosine": round(mean(other), 4) if other else 0.0, "margin": round(mean(rel) - mean(neg), 4) if rel and neg else 0.0,
            "auc": round(wins / pairs, 4) if pairs else 0.0}


# ---- keyword and identifier rankings ------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("a an and are as at be but by for from has have how i if in into is it its my no not of on or our so that the their then there these they this to was we what when where which why will with you your does did can".split())
_SUFFIXES = ("ations", "ation", "ingly", "ings", "ing", "edly", "ed", "ies", "es", "s", "ly")


def stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)] + ("y" if suf == "ies" else "")
    return word


def terms(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {d: Counter(terms(t)) for d, t in docs.items()}
        self.len = {d: sum(c.values()) for d, c in self.tf.items()}
        self.avg = mean(self.len.values()) if self.len else 0
        df: Counter = Counter()
        for c in self.tf.values():
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def rank(self, query: str, limit: int | None = None) -> list[str]:
        q = set(terms(query))
        scores = {}
        for d, tf in self.tf.items():
            s = sum(self.idf.get(t, 0) * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * self.len[d] / self.avg)) for t in q if t in tf)
            if s > 0:
                scores[d] = s
        ranked = [d for d, _ in sorted(scores.items(), key=lambda p: (-p[1], p[0]))]
        return ranked[:limit] if limit else ranked


def identifier_ranking(tokens: list[str], docs: dict[str, str]) -> list[str]:
    """PostgresBackend.identifiers: case-insensitive substring match of up to six tokens, most matches first."""
    if not tokens:
        return []
    hay = {d: t.lower() for d, t in docs.items()}
    scored = [(d, sum(1 for t in tokens[:6] if t.lower() in h)) for d, h in hay.items()]
    return [d for d, n in sorted(scored, key=lambda p: (-p[1], p[0])) if n > 0]


def hybrid(keyword: list[str], identifiers: list[str], vector: list[str] | None, extra: Iterable[list[str]] = ()) -> list[str]:
    rankings = [keyword]
    if identifiers:
        rankings += [identifiers, identifiers]  # as in service._rankings: an exact identifier outweighs any other single signal
    rankings += list(extra)
    if vector is not None:
        rankings.append(vector)
    return [d for d, _ in rrf(*rankings)]
