"""Transparent hypothesis scoring (docs/UNIFIED_INVESTIGATION.md#scoring).

    score = 100 x clamp( 0.35 evidence_strength + 0.15 source_reliability + 0.20 temporal_correlation
                       + 0.15 dependency_relevance + 0.15 historical_similarity
                       - contradictions (at most 0.35) - missing_evidence (at most 0.25)
                       - explained_by_stronger (0.15 when everything it rests on is evidence for a stronger hypothesis), 0, 1 )

Every factor is a number between 0 and 1 computed from the ledger by a rule stated here, shown with the evidence ids
it used and a sentence saying why. The score ranks hypotheses within one investigation; it is not a probability and is
not calibrated against outcomes, and the report says so. No model writes any part of it: a model's own "confidence"
is never read. Caps then hold what the evidence cannot carry: a hypothesis only an agent proposed stays below
"strong", and the reviewer's verdict can lower a score, never raise it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .evidence import RELIABILITY, SHADOW_RELIABILITY, EvidenceItem
from .hypotheses import BY_KEY, Hypothesis
from .signals import Signal

METHOD = "moks.scoring/v1"
WEIGHTS = {"evidence_strength": 0.35, "source_reliability": 0.15, "temporal_correlation": 0.20, "dependency_relevance": 0.15, "historical_similarity": 0.15}
CONTRADICTION_CAP = 0.35
MISSING_CAP = 0.25
MISSING_UNREAD = 0.12       # nothing that could show the required evidence was read
MISSING_INCONCLUSIVE = 0.06  # it was read and showed neither the condition nor its absence
TAU_MINUTES = 45.0           # a change this long before the onset keeps 1/e of its temporal weight
CO_ONSET_MINUTES = 5.0
EXPLAINED = 0.15            # a symptom-level hypothesis whose every required observation a stronger one already accounts for
EXPLAINER_FLOOR = 45         # only a moderate or strong hypothesis explains another away
AGENT_ONLY_CAP = 60          # an agent's proposal outside the library: at most "moderate"
VERDICT_CAPS = {"contradicted": 40, "unsupported": 15}
BANDS = ((70, "strong"), (45, "moderate"), (25, "weak"), (0, "insufficient"))
NOTE = ("Scores rank hypotheses within this investigation by stated rules (evidence strength, source reliability, timing, relevance to the "
        "target, similar past incidents, minus contradictions and missing evidence). They are not probabilities and are not calibrated.")


@dataclass
class Context:
    """What scoring knows about the investigation."""
    items: dict[str, EvidenceItem]
    workload: str | None = None
    namespace: str | None = None
    service: str | None = None
    pods: frozenset[str] = frozenset()
    dependencies: frozenset[str] = frozenset()
    read_tools: set[str] = field(default_factory=set)  # tools that returned a result
    probe_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)  # probe key -> tools that can satisfy it


def band(score: int) -> str:
    return next(b for floor, b in BANDS if score >= floor)


def confidence_label(b: str) -> str:
    """The Report's low/medium/high, for engines and screens that predate the score."""
    return {"strong": "high", "moderate": "medium"}.get(b, "low")


def _reliability(item: EvidenceItem) -> float:
    if item.source_type == "ml_anomaly" and any("shadow" in b for b in item.confidence.basis):
        return SHADOW_RELIABILITY[0]
    return RELIABILITY.get(item.source_type, RELIABILITY["other"])[0]


LEADS = ("knowledge", "historical_incident")  # documents and past incidents: scored once, under historical_similarity


def _cited(h: Hypothesis, ctx: Context, live: bool = False) -> list[EvidenceItem]:
    out = [ctx.items[r] for r in h.refs if r in ctx.items and ctx.items[r].status in ("ok", "partial")]
    return [i for i in out if i.source_type not in LEADS] if live else out


def _strength(h: Hypothesis, ctx: Context) -> tuple[float, str, list[str]]:
    cited = _cited(h, ctx)
    types = {i.source_type for i in cited if i.source_type not in LEADS}
    corroboration = min(1.0, max(0, len(types) - 1) / 2)
    if h.groups:
        best = []
        for g in h.groups:
            scores = [ctx.items[s.ref].confidence.score for s in g.present if s.ref in ctx.items]
            best.append(max(scores, default=0.0))
        coverage = sum(best) / len(best)
        met = sum(1 for g in h.groups if g.satisfied)
        why = f"{met} of {len(h.groups)} required evidence groups observed (quality-weighted coverage {coverage:.2f}); {len(types)} independent live source type(s)"
    else:
        coverage = 0.6 * (sum(i.confidence.score for i in cited) / len(cited)) if cited else 0.0
        why = f"proposed outside the pattern library: coverage from its {len(cited)} citation(s) only, at 60%; {len(types)} independent live source type(s)"
    return round(0.7 * coverage + 0.3 * corroboration, 3), why, [i.id for i in cited]


def _reliability_factor(h: Hypothesis, ctx: Context) -> tuple[float, str, list[str]]:
    cited = _cited(h, ctx, live=True)
    if not cited:
        return 0.0, "no usable live evidence cited", []
    value = sum(_reliability(i) for i in cited) / len(cited)
    kinds = sorted({i.source_type for i in cited})
    return round(value, 3), f"mean stated reliability of the cited live sources ({', '.join(kinds)}); documents and past incidents count under historical similarity", [i.id for i in cited]


def _earliest(signals: list[Signal], keys: tuple[str, ...]) -> Signal | None:
    timed = [s for s in signals if s.present and s.at is not None and s.key in keys]
    return min(timed, key=lambda s: s.at) if timed else None


def _temporal(h: Hypothesis, signals: list[Signal], onset: datetime | None) -> tuple[float, str, list[str]]:
    p = BY_KEY.get(h.pattern)
    mine = [s for s in signals if s.ref in set(h.refs)]
    if p is None:
        return 0.5, "no cause and symptom signals defined for an agent's proposal: timing neither supports nor weakens it", []
    symptom = _earliest(signals, p.symptoms)
    start = symptom.at if symptom else onset
    timed = [s for s in mine if s.present and s.at is not None and s.key in p.causes]
    cause = None
    if timed and start is not None:  # the change nearest before the onset; one that only came after is judged as such
        before = [s for s in timed if (start - s.at).total_seconds() / 60 >= -CO_ONSET_MINUTES]
        cause = max(before, key=lambda s: s.at) if before else min(timed, key=lambda s: s.at)
    elif timed:
        cause = min(timed, key=lambda s: s.at)
    if cause is None or start is None:
        what = "no timed cause or change signal" if cause is None else "no timed symptom"
        return 0.5, f"{what} in the evidence: timing neither supports nor weakens it", [s.ref for s in (cause, symptom) if s]
    lag = (start - cause.at).total_seconds() / 60
    refs = [cause.ref] + ([symptom.ref] if symptom else [])
    what = f"{cause.detail or cause.key} ({cause.ref})"
    if abs(lag) <= CO_ONSET_MINUTES:
        return 1.0, f"{what} is within {CO_ONSET_MINUTES:.0f} minutes of the onset", refs
    if lag < 0:
        return 0.1, f"{what} came {-lag:.0f} minutes after the onset: a cause cannot follow its effect", refs
    value = math.exp(-lag / TAU_MINUTES)
    return round(max(value, 0.15), 3), f"{what} preceded the onset by {lag:.0f} minutes (weight exp(-{lag:.0f}/{TAU_MINUTES:.0f}))", refs


def _relation(item: EvidenceItem, ctx: Context) -> float:
    if item.source_type in ("knowledge", "historical_incident", "git"):
        return 0.5  # not about one resource; neither near nor far
    name = item.resource.name or ""
    if ctx.workload and (name == ctx.workload or name in ctx.pods or (ctx.service and item.service == ctx.service)):
        return 1.0
    if name in ctx.dependencies or (item.service and item.service in ctx.dependencies):
        return 0.8
    if ctx.namespace and item.namespace == ctx.namespace:
        return 0.6
    return 0.2


def _relevance(h: Hypothesis, ctx: Context) -> tuple[float, str, list[str]]:
    cited = _cited(h, ctx, live=True)
    if not cited:
        return 0.0, "no usable live evidence cited", []
    rel = [_relation(i, ctx) for i in cited]
    direct = sum(1 for r in rel if r >= 0.8)
    return round(sum(rel) / len(rel), 3), f"{direct} of {len(cited)} cited item(s) are about {ctx.workload or 'the target'} or a dependency it calls", [i.id for i in cited]


def _history(h: Hypothesis, signals: list[Signal]) -> tuple[float, str, list[str]]:
    p = BY_KEY.get(h.pattern)
    if p is None or p.topic is None:
        return 0.0, "no topic to compare with past incidents", []
    past = [s for s in signals if s.present and s.key == f"history.cause:{p.topic}"]
    if past:
        best = max(past, key=lambda s: s.value or 0)
        return round(min(1.0, best.value or 0), 3), f"a similar approved incident was resolved as {p.topic.replace('_', ' ')}: {best.detail}", [best.ref]
    docs = [s for s in signals if s.present and s.key == f"kb.topic:{p.topic}"]
    if docs:
        return 0.3, f"runbooks or postmortems describe {p.topic.replace('_', ' ')} ({docs[0].detail}); no approved incident matched", [docs[0].ref]
    return 0.0, "no similar past incident or document on this topic", []


def _contradictions(h: Hypothesis, ctx: Context) -> tuple[float, str, list[str]]:
    total, refs, parts = 0.0, [], []
    for s in h.contradicting:
        item = ctx.items.get(s.ref)
        if item is None or item.status not in ("ok", "partial"):
            continue
        total += _reliability(item) * 0.25
        refs.append(s.ref)
        parts.append(f"{s.ref}: {s.detail or s.key}")
    for r in h.agent_contradicting:
        if r in ctx.items and r not in refs and ctx.items[r].status in ("ok", "partial"):
            total += 0.1  # the reviewer's citation: shown, weighted lightly because code did not read it as a contradiction
            refs.append(r)
            parts.append(f"{r}: cited against it by the evidence reviewer")
    value = round(min(CONTRADICTION_CAP, total), 3)
    return value, "; ".join(parts[:4]) or "nothing in the evidence speaks against it", refs


def _missing(h: Hypothesis, ctx: Context) -> tuple[float, str, list[str]]:
    total, parts = 0.0, []
    for g in h.missing:
        if g.observed_absent:
            continue  # read and absent is a contradiction, counted there
        tools = {t for p in g.probes for t in ctx.probe_tools.get(p, ())}
        if tools & ctx.read_tools:
            total += MISSING_INCONCLUSIVE
            parts.append(f"{g.label}: read, inconclusive")
        else:
            total += MISSING_UNREAD
            parts.append(f"{g.label}: not collected")
    return round(min(MISSING_CAP, total), 3), "; ".join(parts) or "every required group was observed", []


def score(h: Hypothesis, signals: list[Signal], ctx: Context, onset: datetime | None) -> dict[str, Any]:
    factors = []
    for name, fn in (("evidence_strength", lambda: _strength(h, ctx)), ("source_reliability", lambda: _reliability_factor(h, ctx)),
                     ("temporal_correlation", lambda: _temporal(h, signals, onset)), ("dependency_relevance", lambda: _relevance(h, ctx)),
                     ("historical_similarity", lambda: _history(h, signals))):
        value, why, refs = fn()
        factors.append({"name": name, "weight": WEIGHTS[name], "value": value, "contribution": round(WEIGHTS[name] * value, 4), "explanation": why, "evidence": refs})
    penalties = []
    for name, fn in (("contradictions", lambda: _contradictions(h, ctx)), ("missing_evidence", lambda: _missing(h, ctx))):
        value, why, refs = fn()
        penalties.append({"name": name, "value": value, "explanation": why, "evidence": refs})
    by = h.explained_by
    penalties.append({"name": "explained_by_stronger", "value": EXPLAINED if by else 0.0,
                      "explanation": f"everything it rests on is also evidence for {by['id']} ({by['title'].lower()}), which explains it" if by else "no stronger hypothesis accounts for its evidence",
                      "evidence": list(by.get("refs") or []) if by else []})
    raw = sum(f["contribution"] for f in factors) - sum(p["value"] for p in penalties)
    value = int(round(100 * max(0.0, min(1.0, raw))))
    caps = []
    if h.pattern not in BY_KEY and value > AGENT_ONLY_CAP:
        caps.append({"cap": AGENT_ONLY_CAP, "reason": "proposed by an agent outside the pattern library: no required evidence defines it"})
        value = AGENT_ONLY_CAP
    verdict = (h.review or {}).get("verdict")
    if verdict in VERDICT_CAPS and value > VERDICT_CAPS[verdict]:
        caps.append({"cap": VERDICT_CAPS[verdict], "reason": f"the independent review found it {verdict}"})
        value = VERDICT_CAPS[verdict]
    return {"score": value, "band": band(value), "raw": round(raw, 4), "factors": factors, "penalties": penalties, "caps": caps, "method": METHOD, "not_a_probability": True}


def _defining(h: Hypothesis) -> set[str]:
    return {s.key for g in h.groups for s in g.present}


def explain_away(ranked: list[Hypothesis]) -> list[Hypothesis]:
    """Hypotheses that are symptoms of another one: everything they rest on is evidence for a moderate or strong hypothesis g,
    and g either ranks above them or is a pattern theirs lists in `symptom_of` (probe failures from CPU throttling). A
    hypothesis whose defining evidence g names as g's cause is upstream of g and is never explained by it: a traffic surge
    that drives CPU saturation is the deeper cause, not a symptom. Returns those it marked."""
    marked = []
    for i, h in enumerate(ranked):
        own = {s.ref for g in h.groups for s in g.present}
        if not own or h.pattern not in BY_KEY:
            continue
        for j, g in enumerate(ranked):
            if g is h or g.pattern not in BY_KEY or g.explained_by is not None or g.score.get("score", 0) < EXPLAINER_FLOOR:
                continue
            upstream = _defining(h) & set(BY_KEY[g.pattern].causes)
            if own <= set(g.refs) and not upstream and (j < i or g.pattern in BY_KEY[h.pattern].symptom_of):
                h.explained_by = {"id": g.id, "title": g.title, "refs": sorted(own, key=lambda r: int(r[1:]))}
                marked.append(h)
                break
    return marked
