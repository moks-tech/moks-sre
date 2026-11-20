"""End-to-end investigation benchmark: the same recorded incidents through every engine MOKS has.

Engines: `langgraph` (the graph), `crewai` (the workload crew), `crewai_improved` (a further crew flow, named when it
exists in this build) and `unified` (this package). Each case runs through the real Investigator against the
scenario's fixture connectors (fixtures.py), so authorization, the executor, validation and the report are the
production code. An engine that cannot run here (no crewai, no model, no such flow, no workload to give it) is reported
as not run, with the reason; it is never scored as zero.

Measures, per case and per engine:
- root_cause: rank of the first cause that matches the expected root cause (its pattern key, or its words for engines
  that have no patterns); accuracy@1 and @3.
- evidence_coverage: share of the source types a correct investigation needs (expect.evidence_sources) that the engine
  read successfully.
- citation_correctness: share of citations (facts and causes) that name stored evidence of this investigation whose read
  succeeded; for causes, also evidence a rule could tie to that kind of cause (unified's own signals; for other
  engines, any successful live read).
- false_conclusions: causes stated at medium or high confidence that the scenario forbids (a wrong pattern, or any cause
  at all in a control case with nothing wrong).
- investigation_seconds, tool_calls, tokens, cost_usd: resource consumption from the run itself.

This measures behaviour on synthetic scenarios with known answers. It does not measure accuracy on real incidents.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from ..config import get_settings
from . import FLOW
from .evidence import source_type

ENGINES = ("langgraph", "crewai", "crewai_improved", "unified")
DATASET = Path(__file__).resolve().parent.parent / "evals" / "datasets" / "investigation_benchmark.yaml"
CONFIDENT = ("medium", "high")


def load(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or DATASET).read_text())


def _matches(text: str, words: list[str]) -> bool:
    return any(re.search(w, text, re.I) for w in words)


def _cause_key(cause: dict[str, Any], unified: dict[str, Any] | None, index: int) -> str | None:
    hyps = (unified or {}).get("hypotheses") or []
    return hyps[index]["pattern"] if index < len(hyps) else None


def score_case(case: dict[str, Any], report: dict[str, Any], rows: list[Any], seconds: float, inv: Any) -> dict[str, Any]:
    exp = case.get("expect") or {}
    unified = report.get("unified")
    causes = report.get("suspected_causes") or []
    truth, ok_keys = exp.get("root_cause"), set(exp.get("acceptable") or [])
    words, ok_words = exp.get("root_cause_words") or [], exp.get("acceptable_words") or []
    rank = rank_ok = None
    for i, c in enumerate(causes):
        key = _cause_key(c, unified, i)
        text = f"{c.get('cause', '')} {c.get('reasoning', '')}"
        # an engine with pattern keys is judged by them; the others by the words the scenario lists
        exact = (key == truth) if key is not None else bool(words and _matches(text, words))
        near = exact or ((key in ok_keys) if key is not None else bool(ok_words and _matches(text, ok_words)))
        if exact and rank is None:
            rank = i + 1
        if near and rank_ok is None:
            rank_ok = i + 1
    by_ref = {r.ref: r for r in rows}
    read = {source_type(r.tool, r.args or {}) for r in rows if r.ok}
    wanted = set(exp.get("evidence_sources") or [])
    coverage = round(len(wanted & read) / len(wanted), 3) if wanted else None
    signal_refs: dict[str, set[str]] = {}
    if unified:
        for s in unified.get("signals") or []:
            if s.get("present"):
                signal_refs.setdefault(s["ref"], set()).add(s["key"])
    cited = ok = 0
    for f in report.get("observed_facts") or []:
        for r in f.get("evidence") or []:
            cited += 1
            ok += int(r in by_ref and by_ref[r].ok)
    for i, c in enumerate(causes):
        for r in c.get("evidence") or []:
            cited += 1
            row = by_ref.get(r)
            good = row is not None and row.ok
            if good and unified:
                good = bool(signal_refs.get(r))  # the cited row carries a signal a rule read from it
            ok += int(good)
    forbid_keys, forbid_words = set(exp.get("forbidden") or []), exp.get("forbidden_words") or []
    false = []
    for i, c in enumerate(causes):
        if c.get("confidence") not in CONFIDENT:
            continue
        key = _cause_key(c, unified, i)
        text = f"{c.get('cause', '')}"
        if exp.get("no_fault") or (key and key in forbid_keys) or (forbid_words and _matches(text, forbid_words)):
            false.append(text[:160])
    return {"case": case["id"], "status": inv.status, "rank": rank, "top1": rank == 1, "top3": rank is not None and rank <= 3,
            "rank_acceptable": rank_ok, "top1_acceptable": rank_ok == 1,
            "no_fault_respected": (not false) if exp.get("no_fault") else None, "evidence_coverage": coverage, "sources_read": sorted(read),
            "citations": cited, "citations_correct": ok, "citation_correctness": round(ok / cited, 3) if cited else None, "false_conclusions": false,
            "seconds": round(seconds, 2), "tool_calls": inv.tool_calls, "tokens": (inv.tokens_in or 0) + (inv.tokens_out or 0), "cost_usd": str(inv.cost_usd or Decimal("0")),
            "mode": inv.mode, "top_cause": (causes[0].get("cause") or "")[:200] if causes else None}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ran = [r for r in rows if r.get("status") not in (None, "not_run")]
    if not ran:
        return {"cases_run": 0, "not_run": len(rows), "reasons": sorted({r.get("reason") for r in rows if r.get("reason")})}
    judged = [r for r in ran if r.get("expected_root_cause")]
    cov = [r["evidence_coverage"] for r in ran if r.get("evidence_coverage") is not None]
    cited = sum(r["citations"] for r in ran)
    secs = sorted(r["seconds"] for r in ran)
    return {"cases_run": len(ran), "not_run": len(rows) - len(ran),
            "accuracy_at_1": round(sum(1 for r in judged if r["top1"]) / len(judged), 3) if judged else None,
            "accuracy_at_3": round(sum(1 for r in judged if r["top3"]) / len(judged), 3) if judged else None,
            "acceptable_at_1": round(sum(1 for r in judged if r["top1_acceptable"]) / len(judged), 3) if judged else None,
            "evidence_coverage": round(sum(cov) / len(cov), 3) if cov else None,
            "citation_correctness": round(sum(r["citations_correct"] for r in ran) / cited, 3) if cited else None,
            "false_conclusions": sum(len(r["false_conclusions"]) for r in ran),
            "seconds_median": secs[len(secs) // 2], "seconds_max": secs[-1], "tool_calls_mean": round(sum(r["tool_calls"] for r in ran) / len(ran), 1),
            "tokens_total": sum(r["tokens"] for r in ran), "cost_usd_total": str(sum(Decimal(r["cost_usd"]) for r in ran)),
            "completed": sum(1 for r in ran if r["status"] == "completed")}


async def _engine_ready(engine: str, use_llm: bool, improved_flow: str | None) -> str | None:
    """Why an engine cannot run here, or None."""
    from ..crew import config as crew_config
    from ..intelligence import agents

    if engine == "langgraph":
        return None
    if engine == "unified":
        return None if not use_llm or agents.installed() else "crewai is not installed at the pinned version, so the agents cannot run (the rule-based pipeline can: run without --llm)"
    if not get_settings().crewai_enabled:
        return "MOKS_CREWAI_ENABLED is not true here"
    if not agents.installed():
        return "crewai is not installed at the pinned version in this process"
    if not use_llm:
        return "a CrewAI crew needs a model: run with --llm"
    if engine == "crewai_improved":
        if not improved_flow:
            return "no improved CrewAI flow was named (--improved-flow)"
        if improved_flow not in crew_config.FLOWS:
            return f"flow {improved_flow!r} is not registered in this build"
    return None


async def _env(name: str) -> uuid.UUID:
    from ..db import session_scope
    from ..models import AUTOMATION_USER_ID, Environment, RoleBinding

    async with session_scope() as db:
        env = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
        if env is None:
            env = Environment(name=name, display_name="Benchmark (fixtures)", kind="minikube", allowed_namespaces=["*"], denied_namespaces=[], max_query_window_minutes=360)
            db.add(env)
            await db.flush()
        if not (await db.execute(select(RoleBinding).where(RoleBinding.user_id == AUTOMATION_USER_ID, RoleBinding.environment == "eval-*"))).first():
            db.add(RoleBinding(user_id=AUTOMATION_USER_ID, role="investigator", environment="eval-*", namespace="*", source="automation"))
        return env.id


async def run_case(case: dict[str, Any], engine: str, *, env_name: str, use_llm: bool, improved_flow: str | None, tag: str) -> dict[str, Any]:
    from ..crew import config as crew_config
    from ..db import session_scope
    from ..models import AUTOMATION_USER_ID, Evidence, Investigation, InvestigationEvent
    from ..orchestrator.engine import Investigator
    from . import fixtures

    why = await _engine_ready(engine, use_llm, improved_flow)
    if why is None and engine in ("crewai", "crewai_improved") and not case.get("workload"):
        why = "the workload crew needs a named workload and this case gives none (identification is part of the test)"
    if why:
        return {"case": case["id"], "engine": engine, "status": "not_run", "reason": why}
    env_id = await _env(env_name)
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        inv = Investigation(created_by=AUTOMATION_USER_ID, environment_id=env_id, namespace=case["namespace"], question=case["question"],
                            time_from=now - timedelta(minutes=case.get("minutes", 60)), time_to=now, status="running", trigger="eval")
        db.add(inv)
        await db.flush()
        block = None
        if engine == "unified":
            block = {"flow": FLOW, "trigger": "benchmark", "agents": use_llm, "realm": "evaluation", **({"workload": case["workload"], "kind": case.get("kind", "Deployment")} if case.get("workload") else {})}
        elif engine in ("crewai", "crewai_improved"):
            block = {"flow": crew_config.FLOW if engine == "crewai" else improved_flow, "workload": case["workload"], "kind": case.get("kind", "Deployment"), "trigger": "benchmark"}
        db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued", payload={"environment": env_name, "namespace": case["namespace"], **({"crewai": block} if block else {})}))
        inv_id = inv.id
    started = time.monotonic()
    await Investigator(inv_id, f"benchmark-{tag}", connectors_override=fixtures.connectors(case, now), use_llm=use_llm).run()
    seconds = time.monotonic() - started
    async with session_scope() as db:
        inv = await db.get(Investigation, inv_id)
        rows = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv_id))).scalars().all()
        out = score_case(case, inv.report or {}, list(rows), seconds, inv)
    return {**out, "engine": engine, "investigation_id": str(inv_id), "expected_root_cause": (case.get("expect") or {}).get("root_cause")}


async def run(data: dict[str, Any] | None = None, *, engines: tuple[str, ...] = ENGINES, use_llm: bool = False, improved_flow: str | None = None,
              cases: list[str] | None = None) -> dict[str, Any]:
    """Every case through every engine. The fixtures' database rows live in the evaluation realm under this run's environment
    and are removed at the end, also when a run fails."""
    from ..rag import access
    from . import fixtures

    data = data or load()
    tag = uuid.uuid4().hex[:8]
    env_name = f"eval-bench-{tag}"
    started = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    token = access.enter_realm(access.EVALUATION)
    try:
        for case in data["cases"]:
            if cases and case["id"] not in cases:
                continue
            await fixtures.seed(case, env_name, f"{tag}-{case['id']}"[:60])
            try:
                for engine in engines:
                    results.append(await run_case(case, engine, env_name=env_name, use_llm=use_llm, improved_flow=improved_flow, tag=tag))
            finally:
                await fixtures.cleanup(env_name, f"{tag}-{case['id']}"[:60])
    finally:
        access.leave_realm(token)
    by_engine = {e: summarize([r for r in results if r["engine"] == e]) for e in engines}
    return {"dataset": data.get("name"), "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(), "mode": "llm" if use_llm else "rules",
            "engines": by_engine, "cases": results, "note": data.get("note") or "", "limits": "Synthetic scenarios with known answers; not accuracy on real incidents."}


def markdown(out: dict[str, Any]) -> str:
    cols = ("cases_run", "accuracy_at_1", "accuracy_at_3", "acceptable_at_1", "evidence_coverage", "citation_correctness", "false_conclusions", "seconds_median", "tool_calls_mean", "tokens_total", "cost_usd_total")
    lines = [f"Benchmark `{out.get('dataset')}`, mode {out.get('mode')}, {out.get('finished_at', '')[:19]}Z", "", "| engine | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for e, s in out["engines"].items():
        if not s.get("cases_run"):
            lines.append(f"| {e} | not run: {'; '.join(s.get('reasons') or ['no case'])} |" + " |" * (len(cols) - 1))
            continue
        lines.append(f"| {e} | " + " | ".join("" if s.get(c) is None else str(s.get(c)) for c in cols) + " |")
    lines += ["", "| case | engine | rank | coverage | citations | false | seconds | tools |", "|---|---|---|---|---|---|---|---|"]
    for r in out["cases"]:
        if r.get("status") == "not_run":
            continue
        lines.append(f"| {r['case']} | {r['engine']} | {r['rank'] or '-'} | {r['evidence_coverage']} | {r['citations_correct']}/{r['citations']} | {len(r['false_conclusions'])} | {r['seconds']} | {r['tool_calls']} |")
    lines += ["", out.get("limits", "")]
    return "\n".join(lines)


async def run_eval(data: dict[str, Any], run_id: Any) -> dict[str, Any]:
    """The `benchmark` evaluation kind (evals/runner.py): every engine that can run here, with gates on the unified pipeline."""
    from ..evals.gates import _gate, summary

    out = await run(data, use_llm=bool(data.get("llm")))
    u = [r for r in out["cases"] if r["engine"] == "unified" and r.get("status") != "not_run"]
    gates = [
        _gate("unified_completes", [r["case"] for r in u if r["status"] != "completed"], len(u)),
        _gate("unified_citations_correct", [r["case"] for r in u if r.get("citation_correctness") is not None and r["citation_correctness"] < 1.0], len(u)),
        _gate("unified_no_false_conclusions", [f"{r['case']}: {r['false_conclusions'][0]}" for r in u if r["false_conclusions"]], len(u)),
        _gate("unified_root_cause_in_top_3", [r["case"] for r in u if r.get("expected_root_cause") and not r["top3"]], sum(1 for r in u if r.get("expected_root_cause"))),
    ]
    rows = []
    for r in out["cases"]:
        if r.get("status") == "not_run":
            continue
        checks = {"completed": r["status"] == "completed", "no_false_conclusions": not r["false_conclusions"],
                  "citations_correct": r.get("citation_correctness") in (None, 1.0)}
        if r.get("expected_root_cause") or r.get("rank") is not None:
            checks["root_cause_in_top_3"] = bool(r["top3"])
        rows.append({**r, "case": f"{r['case']} [{r['engine']}]", "checks": checks, "score": round(sum(checks.values()) / len(checks), 4)})
    metrics = {"engines": out["engines"], "gates": summary(gates), "model": None,
               "not_run": [{"case": r["case"], "engine": r["engine"], "reason": r["reason"]} for r in out["cases"] if r.get("status") == "not_run"]}
    score = (out["engines"].get("unified") or {}).get("accuracy_at_1") or 0
    return {"results": rows, "gates": gates, "metrics": metrics, "score": score}


def write(out: dict[str, Any], json_path: str | None, md_path: str | None) -> None:
    if json_path:
        Path(json_path).write_text(json.dumps(out, indent=1, default=str))
    if md_path:
        Path(md_path).write_text(markdown(out) + "\n")
