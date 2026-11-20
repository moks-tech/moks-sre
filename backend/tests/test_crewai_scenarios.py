"""The ten CrewAI incident scenarios (moks/evals/datasets/crewai_incidents.yaml) through the real Investigator, the incident
flow and fixture Kubernetes, Prometheus, Loki and Git, with each case's scripted agents, scored by moks/crew/metrics.py.

A scripted model knows the answer, so this measures MOKS's pipeline, not a model: that every source a complete
investigation needs is reachable through the tools, that MOKS grades, rejects and composes as specified, and that the
metrics are computed right. The same cases with a real model: `moks eval crewai_incidents --mode llm` on the CrewAI worker.
"""
from __future__ import annotations

import uuid

import pytest

crewai = pytest.importorskip("crewai", reason="the crewai extra is not installed (backend/requirements-crewai.txt)")

from sqlalchemy import select  # noqa: E402

from moks import crew as crew_pkg, versions  # noqa: E402
from moks.config import get_settings  # noqa: E402
from moks.crew import metrics  # noqa: E402
from moks.db import session_scope  # noqa: E402
from moks.evals import crew_eval, runner  # noqa: E402
from moks.llm import budget  # noqa: E402
from moks.models import LLMConfig  # noqa: E402
from moks.orchestrator import engine  # noqa: E402
from moks.rag import access  # noqa: E402
from tests.crew_script import ScriptedSRE  # noqa: E402

DATA = runner.load("crewai_incidents")


def scripted(case: dict) -> dict:
    """The case's script on top of what every agent does by default."""
    workload = f"Deployment/{case['workload']}"
    base = {
        "plan": {"tools": ["namespace_health", "workload_status"],
                 "answer": {"severity": "SEV2", "affected": [{"resource": workload, "reason": "named in the incident", "cite": ["k8s_workloads"]}],
                            "objectives": ["what state are its pods in", "what do its metrics and logs show", "what changed", "has this happened before"]}},
        "kubernetes": {"tools": ["workload_pods"], "answer": {"findings": [], "gaps": []}},
        "observability": {"answer": {"findings": [], "gaps": []}},
        "change": {"tools": ["rollout_history"], "answer": {"findings": [], "gaps": []}},
        "knowledge": {"tools": [["search_runbooks", {"query": case["question"]}]], "answer": {"findings": [], "gaps": ["no runbook or past incident matched"]}},
        "review": {"tools": ["evidence_list"], "answer": {"verdicts": [{"id": "H1", "verdict": "supported", "reason": "the cited evidence agrees"}]}},
        "rca": {"answer": {"summary": "", "root_cause": "H1", "impact": "", "next_steps": ["Confirm the cause before changing anything"]}},
    }
    return {**base, **(case.get("script") or {})}


@pytest.fixture
async def crew_ready(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "crewai_enabled", True)
    monkeypatch.setattr(s, "crewai_tool_timeout_seconds", 5)
    monkeypatch.setattr(engine, "provider_for", lambda cfg: type("M", (), {"name": "fake", "cfg": None})())
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat"))).scalars():
            c.is_active = False
        await db.flush()
        cfg = LLMConfig(name=f"c-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="m", is_active=True)
        db.add(cfg)
    await versions.report(crew_pkg.WORKER, crew_pkg.installed_version())
    yield monkeypatch
    async with session_scope() as db:
        (await db.get(LLMConfig, cfg.id)).is_active = False


async def test_ten_incident_scenarios(client, crew_ready, capsys):
    env_id = await runner._eval_env("eval-crewai-incidents")
    results = []
    token = access.enter_realm(access.EVALUATION)
    try:
        for case in DATA["cases"]:
            crew_ready.setattr(budget, "complete", ScriptedSRE(scripted(case)))
            results.append(await crew_eval.run_case(case, env_id))
    finally:
        access.leave_realm(token)
    by_id = {r["id"]: r for r in results}
    assert len(results) == 10 and all(r["investigation_status"] == "completed" for r in results), [(r["id"], r["error"]) for r in results]
    for r in results:
        assert r["passed"], (r["id"], r["checks"], r["severity"])
        assert r["root_cause_accuracy"] == 1.0, (r["id"], r["root_cause"])
        assert r["completeness"] >= 0.99 or r["id"] == "missing-telemetry", (r["id"], r["sources_missed"])
        assert r["status"] == "completed" and r["fallback"] is None, r["id"]
    # the misbehaving scripts: an invented citation and an overclaiming summary are caught, a contradicted hypothesis is graded down
    assert by_id["http-500-errors"]["citations_rejected"] == 1 and by_id["http-500-errors"]["citation_validity"] < 1
    assert by_id["latency-after-deployment"]["unsupported_conclusions"] >= 1  # the CPU hypothesis the evidence contradicts
    assert by_id["crashloop-missing-config"]["unsupported_conclusions"] >= 1  # the reviewer sank the out-of-memory guess
    assert by_id["missing-telemetry"]["root_cause"] == "no root cause established"
    gate = {g["name"]: g for g in crew_eval.gates(results)}
    assert all(g["passed"] for g in gate.values()), {k: (g.get("value"), g["failures"]) for k, g in gate.items() if not g["passed"]}
    total = metrics.summary(results)
    with capsys.disabled():
        print("\n" + "case".ljust(32) + "score  compl  evid   rca    cite   unsup  sec   tokens")
        for r in results:
            print(f"{r['id'][:31].ljust(32)}{r['score']:.2f}   {r['completeness']:.2f}   {r['evidence_quality']:.2f}   {r['root_cause_accuracy']:.2f}   "
                  f"{r['citation_validity']:.2f}   {r['unsupported_conclusions']:>3}   {r['execution_seconds']:>4.1f}  {r['tokens']:>6}")
        print(f"{'mean'.ljust(32)}{total['score']:.2f}   {total['completeness']:.2f}   {total['evidence_quality']:.2f}   {total['root_cause_accuracy']:.2f}   "
              f"{total['citation_validity']:.2f}")
