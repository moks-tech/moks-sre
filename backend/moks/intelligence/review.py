"""Independent evidence validation (stage 12).

The review never reads how a hypothesis was argued. It gets the claim and the evidence ids cited for it, and works
from the ledger: does each citation exist in this investigation, did the read succeed, is it inside the
investigation's environment and target, does the stored evidence carry a signal this kind of cause needs, and what in
the ledger speaks against it. A citation that exists but shows nothing relevant is reported as such; an assumption no
evidence shows is named; evidence outside the investigation's scope is dropped, whoever cited it.

The evidence reviewer agent (agents.py) adds its own challenges on top. Its verdict can lower this one, never raise it.
"""
from __future__ import annotations

from typing import Any

from .evidence import EvidenceItem
from .hypotheses import BY_KEY, Hypothesis
from .signals import Signal

ORDER = ("supported", "partially_supported", "contradicted", "unsupported")  # later is weaker


def weaker(a: str | None, b: str | None) -> str:
    known = [v for v in (a, b) if v in ORDER]
    return max(known, key=ORDER.index) if known else "unsupported"


def _relevant_keys(h: Hypothesis) -> set[str]:
    p = BY_KEY.get(h.pattern)
    if p is None:
        return set()
    return {k for r in p.required for k in r.any_of} | set(p.supporting)


def review(h: Hypothesis, items: dict[str, EvidenceItem], signals: list[Signal], *, environment: str, namespaces: set[str]) -> dict[str, Any]:
    by_ref: dict[str, list[Signal]] = {}
    for s in signals:
        by_ref.setdefault(s.ref, []).append(s)
    relevant = _relevant_keys(h)
    citations: list[dict[str, Any]] = []
    out_of_scope: list[str] = []
    for ref in h.refs:
        item = items.get(ref)
        if item is None:
            citations.append({"ref": ref, "valid": False, "reason": "not in this investigation's evidence"})
            continue
        if item.environment != environment or (item.namespace is not None and item.namespace not in namespaces):
            out_of_scope.append(ref)
            citations.append({"ref": ref, "valid": False, "reason": f"outside this investigation's scope ({item.environment}/{item.namespace})"})
            continue
        if item.status in ("failed", "denied"):
            citations.append({"ref": ref, "valid": False, "reason": f"the read {item.status}: it shows only that the source could not be read"})
            continue
        shows = sorted({s.key for s in by_ref.get(ref, []) if s.present and (not relevant or s.key in relevant)})
        if relevant and not shows:
            citations.append({"ref": ref, "valid": True, "relevant": False, "reason": "exists, but the stored evidence shows nothing this kind of cause needs"})
        else:
            citations.append({"ref": ref, "valid": True, "relevant": True, "shows": shows})
    valid_support = [c for c in citations if c["valid"] and c.get("relevant", True)]
    assumptions = [f"assumes {g.label}, which no evidence shows" for g in h.groups if not g.satisfied and not g.observed_absent]
    refuted = [f"{g.label} was checked and is absent ({', '.join(s.ref for s in g.absent[:2])})" for g in h.groups if g.observed_absent]
    conflicts = [{"ref": s.ref, "detail": s.detail or s.key} for s in h.contradicting]
    if not valid_support:
        verdict = "unsupported"
    elif refuted or sum(1 for _ in conflicts) >= 2:
        verdict = "contradicted"
    elif h.groups and all(g.satisfied for g in h.groups) and not conflicts:
        verdict = "supported"
    else:
        verdict = "partially_supported"
    return {"verdict": verdict, "by": "moks", "citations": citations, "unsupported_assumptions": assumptions, "refuted": refuted, "conflicts": conflicts,
            "out_of_scope": out_of_scope, "missing": [{"label": g.label, "probes": list(g.probes)} for g in h.missing if not g.observed_absent]}


def merge_agent(h: Hypothesis, code: dict[str, Any], agent: dict[str, Any] | None, items: dict[str, EvidenceItem]) -> dict[str, Any]:
    """The agent reviewer's view on top of the code's: its verdict only ever lowers the code's; what it cites against the
    hypothesis must exist and have been read; its notes are kept as model-written text, marked as such."""
    if not agent:
        return code
    against = [str(r) for r in agent.get("contradicting") or [] if str(r) in items and items[str(r)].status in ("ok", "partial")]
    invented = [str(r) for r in agent.get("contradicting") or [] if str(r) not in items]
    h.agent_contradicting = list(dict.fromkeys([*h.agent_contradicting, *against]))
    asked = str(agent.get("verdict") or "")
    verdict = weaker(code["verdict"], asked) if asked in ORDER else code["verdict"]
    notes = [" ".join(str(n).split())[:300] for n in (agent.get("unsupported_assumptions") or []) if str(n).strip()][:5]
    return {**code, "verdict": verdict, "by": "moks+agent" if verdict != code["verdict"] or against or notes else code["by"],
            "agent": {"verdict": asked if asked in ORDER else None, "contradicting": against, "citations_rejected": invented, "notes": notes, "model_written": True}}
