"""Runs `kind: crewai` datasets (datasets/crewai_incidents.yaml): every case through the real Investigator and the CrewAI
incident flow, against fixture connectors (crew_fixtures.py), scored by moks/crew/metrics.py.

The crew needs the crewai package, so this runs where a CrewAI worker runs: `moks eval crewai_incidents --mode llm` in
the moks-crewai pod measures the active chat model. Rule-based mode does not apply (there is no crew without a model).
backend/tests/test_crewai_scenarios.py runs the same cases with a scripted model, which measures MOKS's pipeline.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from ..crew import metrics
from ..crew.incident_config import FLOW
from ..db import session_scope
from ..models import AUTOMATION_USER_ID, Evidence, Investigation, InvestigationEvent
from ..orchestrator.engine import Investigator
from ..rag import access
from .crew_fixtures import connectors_for

# Thresholds for the gates over the whole dataset (docs/crewai-sre-engine.md, section 7).
THRESHOLDS = {"citation_validity": (">=", 0.9), "root_cause_accuracy": (">=", 0.7), "evidence_quality": (">=", 0.6), "completeness": (">=", 0.7),
              "unsupported_rate": ("<=", 0.3)}


async def run_case(case: dict[str, Any], env_id: uuid.UUID, *, use_llm: bool = True, created_by: uuid.UUID = AUTOMATION_USER_ID, worker: str = "eval-crewai") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        inv = Investigation(created_by=created_by, environment_id=env_id, namespace=case["namespace"], question=case["question"],
                            time_from=now - timedelta(minutes=case.get("minutes", 60)), time_to=now, status="running", trigger="eval")
        db.add(inv)
        await db.flush()
        db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued", payload={"crewai": {
            "flow": FLOW, "workload": case["workload"], "kind": case.get("kind", "Deployment"), "change_window": case.get("change_window", "24h"),
            "trigger": "eval", "config_version": None}}))
        inv_id = inv.id
    started = time.monotonic()
    await Investigator(inv_id, worker, connectors_override=connectors_for(case), use_llm=use_llm).run()
    seconds = time.monotonic() - started
    async with session_scope() as db:
        inv = await db.get(Investigation, inv_id)
        rows = (await db.execute(select(Evidence.tool, Evidence.ok).where(Evidence.investigation_id == inv_id))).all()
    out = metrics.score(case, inv.report or {}, {t for t, ok in rows if ok}, seconds, int(inv.tokens_in or 0) + int(inv.tokens_out or 0))
    return {**out, "investigation_id": str(inv_id), "investigation_status": inv.status, "error": inv.error, "title": case.get("title")}


def gates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = metrics.summary(results)
    out = []
    for name, (op, limit) in THRESHOLDS.items():
        value = total[name]
        ok = value >= limit if op == ">=" else value <= limit
        out.append({"name": name, "state": "passed" if ok else "failed", "passed": ok, "checked": len(results), "value": value, "evidence": "deterministic",
                    "failures": [f"{r['id']}: {r[name]}" for r in results if (r[name] < limit if op == ">=" else r[name] > limit)][:20]})
    failed = [r for r in results if not r["passed"] or r["investigation_status"] != "completed"]
    out.append({"name": "expectations", "state": "failed" if failed else "passed", "passed": not failed, "checked": len(results), "evidence": "deterministic",
                "failures": [f"{r['id']}: {r['checks'] or r['error']}" for r in failed][:20]})
    return out


async def run(data: dict[str, Any], env_id: uuid.UUID, mode: str) -> dict[str, Any]:
    from ..crew import installed_version

    if mode != "llm":
        raise ValueError("the CrewAI incident scenarios measure the crew, which needs a model: run them with --mode llm where the CrewAI worker runs")
    if installed_version() is None:
        raise ValueError("the crewai package is not installed here: run `moks eval crewai_incidents --mode llm` in the moks-crewai pod")
    token = access.enter_realm(access.EVALUATION)  # anything a run retrieves comes from the evaluation realm, never production
    try:
        results = [await run_case(case, env_id) for case in data["cases"]]
    finally:
        access.leave_realm(token)
    g = gates(results)
    return {"results": results, "gates": g, "metrics": {**metrics.summary(results), "mode": mode}, "score": metrics.summary(results)["score"]}
