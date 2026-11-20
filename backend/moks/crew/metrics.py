"""How good an incident investigation was, against a scenario's expectations (datasets/crewai_incidents.yaml).

Seven measures, each from what MOKS stored (the report, its `crewai` block and the evidence rows), never from what a
model says about itself:

- completeness: the share of the sources a complete investigation of this incident reads (`expect.sources`) that were
  read successfully, times the share of workflow steps that finished.
- evidence_quality: the share of accepted findings that are confirmed observations about a resource the evidence names.
- root_cause_accuracy: 1 when the established root cause names the expected cause (`expect.root_cause_any`), 0.5 when only
  the top possible explanation does, 0 otherwise. For a scenario where no root cause can be established
  (`expect.root_cause: null`), 1 when none was and the expected missing telemetry is reported.
- citation_validity: valid citations over all citations the agents made (invented ids count against it).
- unsupported_conclusions: claims MOKS set aside plus hypotheses graded insufficient; lower is better.
- execution_seconds and tokens: what the run took.

`score` is the mean of completeness, evidence quality, root-cause accuracy, citation validity and (1 - the unsupported
rate), where the unsupported rate is unsupported conclusions over everything the agents concluded.
"""
from __future__ import annotations

from typing import Any

STEPS = ("plan", "kubernetes", "observability", "change", "knowledge", "hypotheses", "review", "rca")


def _has(text: str, words: list[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def score(case: dict[str, Any], report: dict[str, Any], tools_ok: set[str], seconds: float, tokens: int) -> dict[str, Any]:
    exp = case.get("expect") or {}
    crew = report.get("crewai") or {}
    inv = crew.get("investigation") or {}
    findings = inv.get("findings") or []
    hypotheses = inv.get("hypotheses") or []
    tasks = {t["task"]: t["status"] for t in inv.get("tasks") or [] if t.get("round", 1) == 1}

    wanted = exp.get("sources") or []
    covered = [s for s in wanted if s in tools_ok]
    steps_done = sum(1 for s in STEPS if tasks.get(s) in ("done", "partial", "restored")) / len(STEPS) if tasks else 0.0
    completeness = round((len(covered) / len(wanted) if wanted else 1.0) * steps_done, 3)

    solid = [f for f in findings if f.get("status") == "confirmed_observation" and f.get("resource_verified")]
    evidence_quality = round(len(solid) / len(findings), 3) if findings else 0.0

    cause = inv.get("root_cause")
    missing = " ".join(report.get("missing_evidence") or [])
    if "root_cause_any" not in exp and exp.get("root_cause", "x") is None:
        accuracy = 1.0 if cause is None and all(_has(missing, group if isinstance(group, list) else [group]) for group in exp.get("missing_any") or []) else 0.0
        matched = "no root cause established" if cause is None else cause.get("statement")
    else:
        words = exp.get("root_cause_any") or []
        top = next((h for h in sorted(hypotheses, key=lambda h: h.get("rank") or 99) if h.get("status") == "possible_explanation"), None)
        if cause and _has(cause.get("statement") or "", words):
            accuracy, matched = 1.0, cause["statement"]
        elif cause is None and top and _has(top.get("statement") or "", words):
            accuracy, matched = 0.5, top["statement"]
        else:
            accuracy, matched = 0.0, (cause or top or {}).get("statement")

    rejected = len((crew.get("validation") or {}).get("citations_rejected") or [])
    valid = sum(len(f.get("citations") or []) for f in findings) + sum(len(h.get("supporting") or []) for h in hypotheses)
    citation_validity = round(valid / (valid + rejected), 3) if valid + rejected else 0.0

    unsupported = len(inv.get("unsupported") or []) + sum(1 for h in hypotheses if h.get("status") == "insufficient_evidence")
    concluded = len(findings) + len(hypotheses) + len(inv.get("unsupported") or [])
    unsupported_rate = round(unsupported / concluded, 3) if concluded else 0.0

    sev = (inv.get("severity") or {}).get("level")
    checks = {"severity": sev in exp["severity_in"]} if exp.get("severity_in") else {}
    forbidden = [w for w in exp.get("forbid") or [] if w.lower() in str(report.get("summary") or "").lower()]
    if exp.get("forbid"):
        checks["forbidden_absent"] = not forbidden
    total = round((completeness + evidence_quality + accuracy + citation_validity + (1 - unsupported_rate)) / 5, 4)
    return {"id": case["id"], "score": total, "completeness": completeness, "sources_missed": [s for s in wanted if s not in covered], "steps_completed": round(steps_done, 3),
            "evidence_quality": evidence_quality, "root_cause_accuracy": accuracy, "root_cause": matched, "citation_validity": citation_validity,
            "citations_rejected": rejected, "unsupported_conclusions": unsupported, "unsupported_rate": unsupported_rate, "execution_seconds": round(seconds, 2),
            "tokens": tokens, "severity": sev, "checks": checks, "passed": all(checks.values()) if checks else True,
            "status": crew.get("status"), "fallback": crew.get("fallback")}


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("score", "completeness", "evidence_quality", "root_cause_accuracy", "citation_validity", "unsupported_rate", "execution_seconds", "tokens")
    n = len(results) or 1
    return {k: round(sum(r[k] for r in results) / n, 4) for k in keys} | {"cases": len(results), "unsupported_conclusions": sum(r["unsupported_conclusions"] for r in results)}
