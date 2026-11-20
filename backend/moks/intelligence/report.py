"""The final investigation report (stage 15), and its mapping onto MOKS's Report.

Sections: executive summary, timeline, affected services, business and technical impact, confirmed observations,
ranked root-cause hypotheses with their scores, supporting and contradictory evidence, historical references,
verification steps, remediation, and confidence and limitations. Every sentence is built from the ledger by code;
model-written text appears only where an agent proposed a hypothesis, and is marked. Business impact is stated only
as far as evidence shows it (an Ingress, a production environment, an alert's severity); MOKS does not know what a
service is worth and never guesses.

The same content also fills the existing Report fields (summary, observed_facts, suspected_causes, missing_evidence,
next_steps, proposed_remediation), so every screen, API and check that reads a report keeps working unchanged, and
the engine's own validate_claims, verify_causes and relevance checks run on it as on any other report.
"""
from __future__ import annotations


from typing import Any

from ..orchestrator.report import Cause, Fact, Remediation, Report
from . import ENGINE, ENGINE_VERSION
from .evidence import EvidenceItem, RELIABILITY
from .hypotheses import BY_KEY, Hypothesis
from .scoring import NOTE as SCORING_NOTE, confidence_label
from .signals import Signal

SYMPTOM_KEYS = ("metric.latency_increase", "metric.error_rate_increase", "metric.traffic_drop", "alert.firing", "log.http_5xx", "log.db_pool_exhausted", "log.db_timeout",
                "log.exception", "log.oom", "k8s.oomkilled", "k8s.crashloop", "k8s.probe_failed", "trace.error", "k8s.workload_unavailable")
ML_NOTE = "Anomaly-model observations mean a window was unusual for the workload compared with its own history. They are leads, never a cause."


def onset(signals: list[Signal]) -> Signal | None:
    timed = [s for s in signals if s.present and s.at is not None and s.key in SYMPTOM_KEYS]
    return min(timed, key=lambda s: s.at) if timed else None


def _kind(key: str) -> str:
    if key.startswith(("deploy.", "git.")):
        return "change"
    if key.startswith("alert."):
        return "alert"
    if key.startswith("ml."):
        return "anomaly"
    if key in SYMPTOM_KEYS or key.startswith(("metric.", "log.", "trace.")):
        return "symptom"
    return "event"


def timeline(signals: list[Signal], start: Signal | None, limit: int = 40) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    rows = []
    for s in sorted((s for s in signals if s.present and s.at is not None), key=lambda s: s.at):
        k = (s.key, s.ref, s.at)
        if k in seen:
            continue
        seen.add(k)
        rows.append({"at": s.at.isoformat(), "kind": _kind(s.key), "signal": s.key, "title": s.detail or s.key, "evidence": [s.ref], "onset": start is not None and s is start})
    return rows[:limit]


def correlations(signals: list[Signal], items: dict[str, EvidenceItem], start: Signal | None) -> list[dict[str, Any]]:
    out = []
    for s in signals:
        if not (s.present and s.key.startswith("ml.anomaly:")):
            continue
        lag = round((s.at - start.at).total_seconds() / 60) if (s.at and start and start.at) else None
        item = items.get(s.ref)
        out.append({"feature": s.key.split(":", 1)[1], "service": s.resource, "at": s.at.isoformat() if s.at else None, "detail": s.detail, "value": s.value,
                    "lag_minutes": lag, "relation": None if lag is None else "coincides" if abs(lag) <= 30 else "precedes" if lag < 0 else "follows",
                    "shadow": bool(item and any("shadow" in b for b in item.confidence.basis)), "evidence": [s.ref]})
    return out


def _observations(signals: list[Signal], items: dict[str, EvidenceItem], service: str | None, limit: int = 14) -> list[dict[str, Any]]:
    best: dict[tuple, Signal] = {}
    for s in signals:
        if not s.present or s.ref not in items or items[s.ref].status not in ("ok", "partial") or s.source_type in ("knowledge", "historical_incident"):
            continue
        k = (s.key, s.resource)
        if k not in best:
            best[k] = s
    ranked = sorted(best.values(), key=lambda s: (s.key not in SYMPTOM_KEYS, -RELIABILITY.get(s.source_type, (0.5, ""))[0], s.at.timestamp() if s.at else float("inf")))
    out = []
    for s in ranked[:limit]:
        who = s.resource or service
        statement = f"{s.detail}" if who and who.split("/")[-1] in (s.detail or "") else f"{who}: {s.detail}" if who else s.detail
        out.append({"statement": statement[:600], "evidence": [s.ref], "signal": s.key, "source_type": s.source_type, "at": s.at.isoformat() if s.at else None})
    return out


def _impact(signals: list[Signal], records: list[dict], target: Any, production: bool) -> dict[str, Any]:
    technical = [{"statement": s.detail, "evidence": [s.ref]} for s in signals if s.present and s.key in (
        "metric.latency_increase", "metric.error_rate_increase", "k8s.workload_unavailable", "k8s.crashloop", "k8s.restarts", "k8s.oomkilled", "metric.traffic_drop")][:8]
    exposure = []
    for r in records:
        if r["tool"] == "k8s_triage" and r.get("ok") and isinstance(r.get("data"), dict):
            for ing in r["data"].get("ingresses") or []:
                for rule in ing.get("rules") or []:  # the connector's shape: one flat {host, path, service, port} per path
                    if target.service and rule.get("service") == target.service:
                        exposure.append({"host": rule.get("host") or "*", "path": rule.get("path") or "/", "evidence": [r["ref"]]})
    critical = [s for s in signals if s.present and s.key == "alert.firing" and "(critical" in s.detail]
    basis, refs = [], []
    if exposure:
        basis.append("serves " + ", ".join(f"{e['host']}{e['path']}" for e in exposure[:3]) + " through an Ingress")
        refs += [x for e in exposure for x in e["evidence"]]
    if production:
        basis.append("runs in a production environment")
    if critical:
        basis.append(f"a critical alert is firing ({critical[0].detail})")
        refs.append(critical[0].ref)
    if basis and technical:
        statement = f"{target.service or target.workload or 'The service'} " + "; ".join(basis) + ", so the symptoms above are likely to reach its users."
    elif basis:
        statement = f"{target.service or target.workload or 'The service'} " + "; ".join(basis) + ", but no user-facing symptom is in the evidence."
    else:
        statement = "Not established from evidence: MOKS has no record of who uses this service or what it is worth, and found no Ingress, production flag or critical alert for it."
    return {"technical": technical, "business": {"statement": statement, "basis": basis, "evidence": list(dict.fromkeys(refs)), "exposure": exposure}}


def _affected(target: Any, signals: list[Signal]) -> list[dict[str, Any]]:
    out = []
    if target.workload:
        out.append({"service": target.service or target.workload, "workload": f"{target.kind}/{target.workload}", "namespace": target.namespace, "role": "primary",
                    "evidence": list(dict.fromkeys(s.ref for s in signals if s.present and s.key in SYMPTOM_KEYS))[:6]})
    for d in sorted(target.dependencies):
        refs = [s.ref for s in signals if s.present and d in (s.detail or "")][:4]
        out.append({"service": d, "workload": None, "namespace": target.namespace, "role": "dependency", "evidence": refs})
    for svc in sorted({str(s.resource) for s in signals if s.present and s.key == "trace.error" and s.resource} - {target.service, target.workload, *target.dependencies}):
        out.append({"service": svc, "workload": None, "namespace": target.namespace, "role": "on the failing trace", "evidence": [s.ref for s in signals if s.resource == svc][:3]})
    for other in target.also:
        out.append({"service": other.split(" ", 1)[-1], "workload": other.replace(" ", "/"), "namespace": target.namespace, "role": "also unhealthy (not investigated in depth)", "evidence": []})
    return out


def _hyp_out(h: Hypothesis, rank: int) -> dict[str, Any]:
    p = BY_KEY.get(h.pattern)
    return {"id": h.id, "rank": rank, "pattern": h.pattern, "title": h.title, "category": h.category, "statement": h.statement, "origin": h.origin,
            "score": h.score, "review": h.review, "supporting": h.refs, "contradicting": [{"ref": s.ref, "detail": s.detail or s.key} for s in h.contradicting] +
            [{"ref": r, "detail": "cited against it by the evidence reviewer"} for r in h.agent_contradicting if r not in {s.ref for s in h.contradicting}],
            "groups": [{"label": g.label, "satisfied": g.satisfied, "observed_absent": g.observed_absent, "evidence": [s.ref for s in g.present], "absent_evidence": [s.ref for s in g.absent]}
                       for g in h.groups],
            "missing": [g.label for g in h.missing], "verification": list(p.verification) if p else [], "model_written": h.origin != "rules" and h.pattern == "agent",
            "explained_by": h.explained_by}


def _summary(target: Any, ranked: list[Hypothesis], observations: list[dict], gaps: list[dict], status: str) -> str:
    who = target.label()
    if not ranked:
        return (f"{who}: no failure pattern matched the evidence. {len(observations)} observation(s) were collected"
                + (f"; {len(gaps)} source(s) could not be read" if gaps else "") + ". See the observations and limitations below.")
    top = ranked[0]
    sc = top.score
    groups = f"{sum(1 for g in top.groups if g.satisfied)} of {len(top.groups)} required evidence groups observed" if top.groups else "proposed by the analyst agent from its citations"
    timing = next((f["explanation"] for f in sc.get("factors", []) if f["name"] == "temporal_correlation" and f["value"] not in (0.5,)), None)
    against = next((p for p in sc.get("penalties", []) if p["name"] == "contradictions" and p["value"] > 0), None)
    parts = [f"{who}: the leading hypothesis is {top.title.lower()} ({sc.get('band')}, {sc.get('score')}/100): {top.statement}.", f"{groups.capitalize()}."]
    if timing:
        parts.append(timing[0].upper() + timing[1:] + ".")
    if against:
        parts.append(f"Against it: {against['explanation']}.")
    if len(ranked) > 1:
        parts.append(f"{len(ranked) - 1} other hypothesis(es) ranked lower, led by {ranked[1].title.lower()} ({ranked[1].score.get('score')}/100).")
    if status != "completed" or gaps:
        parts.append(f"The analysis is {'partial' if status != 'completed' else 'limited'}: {len(gaps)} gap(s) are listed under limitations.")
    return " ".join(parts)[:2000]


def build(*, target: Any, incident: dict, classification: dict, ranked: list[Hypothesis], items: dict[str, EvidenceItem], signals: list[Signal], records: list[dict],
          gaps: list[dict], stages: list[dict], budget: dict, production: bool, remediation: list[dict], agents: dict, refused: list[dict], status: str,
          notes: list[str], not_shown: list[dict] | None = None) -> tuple[dict[str, Any], Report]:
    start = onset(signals)
    observations = _observations(signals, items, target.service)
    hyps = [_hyp_out(h, i + 1) for i, h in enumerate(ranked)]
    supporting = list(dict.fromkeys(r for h in ranked for r in h.refs if r in items))
    contradictory = [{"ref": c["ref"], "hypothesis": h["id"], "detail": c["detail"]} for h in hyps for c in h["contradicting"]]
    similar = [{"title": s.detail, "evidence": [s.ref], "similarity": s.value} for s in signals if s.present and s.key == "history.similar"]
    documents: list[dict[str, Any]] = []
    for r in records:
        if r["tool"] == "kb_search" and r.get("ok") and isinstance(r.get("data"), list):
            for hit in r["data"][:5]:
                c = hit.get("citation") or {}
                documents.append({"title": hit.get("title"), "heading": hit.get("heading"), "path": c.get("path"), "source": c.get("source"), "repository": c.get("repository"),
                                  "evidence": [r["ref"]], "kind": "postmortem" if "postmortem" in str((r.get("args") or {}).get("query", "")) else "runbook"})
    verification = [{"step": v, "hypothesis": h.id} for h in ranked[:3] for v in (BY_KEY[h.pattern].verification if h.pattern in BY_KEY else ())]
    verification += [{"step": f"Collect {g.label} (not read in this run)", "hypothesis": h.id, "probes": list(g.probes)} for h in ranked[:3] for g in h.missing if not g.observed_absent]
    gap_lines = [f"{g['source']}: {g['reason']}" for g in gaps]
    limitations = {"gaps": gaps, "notes": list(dict.fromkeys(notes)), "scoring": SCORING_NOTE, "ml": ML_NOTE if any(s.source_type == "ml_anomaly" for s in signals) else None,
                   "refused": refused, "status": status, "hypotheses_not_shown": not_shown or [],
                   "stages_not_completed": [{"stage": s["key"], "status": s["status"], "reason": s["reason"]} for s in stages if s["status"] not in ("completed",)]}
    block = {
        "engine": ENGINE, "version": ENGINE_VERSION, "status": status, "target": target.out(), "incident": incident, "classification": classification,
        "pipeline": stages, "budget": budget, "executive_summary": _summary(target, ranked, observations, gaps, status),
        "onset": {"at": start.at.isoformat(), "signal": start.key, "evidence": [start.ref]} if start else None,
        "timeline": timeline(signals, start), "affected_services": _affected(target, signals), "impact": _impact(signals, records, target, production),
        "observations": observations, "hypotheses": hyps, "supporting_evidence": supporting, "contradictory_evidence": contradictory,
        "historical_references": {"similar_incidents": similar, "documents": [d for d in documents if d["kind"] == "postmortem"]},
        "runbooks": [d for d in documents if d["kind"] == "runbook"], "anomaly_correlations": correlations(signals, items, start),
        "verification_steps": verification, "remediation": remediation, "limitations": limitations, "agents": agents,
        "evidence_items": [i.model_dump() for i in items.values()], "signals": [s.out() for s in signals[:300]],
    }
    model_text = any(h.pattern == "agent" for h in ranked)
    report = Report(
        summary=block["executive_summary"],
        observed_facts=[Fact(statement=o["statement"], evidence=o["evidence"]) for o in observations],
        suspected_causes=[Cause(cause=f"{h.title}: {h.statement}"[:1000], confidence=confidence_label(h.score.get("band", "insufficient")),  # type: ignore[arg-type]
                                evidence=[r for r in h.refs if r in items and items[r].status in ("ok", "partial")][:10],
                                reasoning=(f"Score {h.score.get('score')}/100 ({h.score.get('band')}, not a probability). "
                                           + " ".join(f"{f['name'].replace('_', ' ')} {f['value']:.2f}: {f['explanation']}." for f in h.score.get("factors", [])[:3]))[:1200],
                                contradicting=h.against[:10], origin="model" if h.pattern == "agent" else "rules") for h in ranked],
        missing_evidence=list(dict.fromkeys([*gap_lines, *(f"{g.label} (for {h.title.lower()})" for h in ranked[:3] for g in h.missing if not g.observed_absent)]))[:20],
        next_steps=list(dict.fromkeys(v["step"] for v in verification))[:10],
        proposed_remediation=[Remediation(action=r["action"][:600], target=r.get("target", "")[:300], expected_impact=r.get("expected_impact", "")[:600], risk=r.get("risk", "")[:600],
                                          rollback=r.get("rollback", "")[:600]) for r in remediation[:8]],
        generated_by="llm" if model_text else "rules", notes=list(dict.fromkeys(notes)))
    report.validation = {"valid_refs": sorted(items, key=lambda r: int(r[1:]))}
    return block, report


def remediation_for(ranked: list[Hypothesis], target: Any, limit: int = 3) -> list[dict[str, Any]]:
    """Allowlisted actions to consider for the strongest hypotheses. Each needs an approved action plan; nothing here runs."""
    out = []
    for h in ranked[:limit]:
        p = BY_KEY.get(h.pattern)
        if p is None or h.score.get("band") in ("insufficient",):
            continue
        for r in p.remediation:
            out.append({"action": r.action, "hypothesis": h.id, "hypothesis_title": h.title, "target": f"{target.kind}/{target.workload}" if target.workload else target.namespace,
                        "risk": r.risk, "rollback": r.rollback, "action_kind": r.action_kind,
                        "expected_impact": f"addresses {h.title.lower()} if it is the cause; verify first",
                        "requires_approval": True, "executable": False, "evidence": h.refs[:6]})
    return out
