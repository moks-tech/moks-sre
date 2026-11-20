"""The CrewAI incident flow end to end (docs/crewai-sre-engine.md): seven agents played by a scripted model through
llm.budget, FakeKubernetes for the cluster, nothing on a network. Needs the crewai extra; skipped where it is not installed."""
from __future__ import annotations

import uuid

import pytest

crewai = pytest.importorskip("crewai", reason="the crewai extra is not installed (backend/requirements-crewai.txt)")

from sqlalchemy import select  # noqa: E402

from moks import crew as crew_pkg, versions  # noqa: E402
from moks.config import get_settings  # noqa: E402
from moks.connectors import registry  # noqa: E402
from moks.db import session_scope  # noqa: E402
from moks.llm import budget  # noqa: E402
from moks.models import CrewRunStep, Evidence, Investigation, InvestigationCheckpoint, InvestigationEvent, LLMConfig  # noqa: E402
from moks.orchestrator import engine  # noqa: E402
from moks.orchestrator.engine import Investigator  # noqa: E402
from tests.conftest import login, make_env, make_user  # noqa: E402
from tests.crew_script import ScriptedSRE  # noqa: E402
from tests.fakes import FakeKubernetes  # noqa: E402

POD = "payments-5c4-xyz"

HAPPY = {
    "plan": {"tools": ["namespace_health", "workload_status"],
             "answer": {"severity": "SEV1", "affected": [{"resource": "Deployment/payments", "reason": "no replica ready", "cite": ["k8s_workloads"]},
                                                         {"resource": "Deployment/billing", "reason": "invented", "cite": ["k8s_workloads"]}],
                        "objectives": ["why do the payments pods crash", "did a change precede it"]}},
    "kubernetes": {"tools": ["workload_pods", ["describe_pod", {"pod": POD}], ["pod_logs", {"pod": POD, "previous": True}]],
                   "answer": {"findings": [{"observation": f"Pod {POD} is in CrashLoopBackOff with 7 restarts", "resource": f"pod/{POD}", "cite": ["k8s_describe_pod"]},
                                           {"observation": "The previous container logged a db timeout before it exited", "resource": f"pod/{POD}", "cite": ["k8s_pod_logs"]},
                                           {"observation": "An observation with an invented citation", "resource": "pod/x", "evidence": ["E99"]}],
                              "gaps": []}},
    "observability": {"answer": {"findings": [], "gaps": ["no Prometheus connector in this environment"]}},
    "change": {"tools": ["rollout_history"],
               "answer": {"findings": [{"observation": "Revision 3 of payments rolled out with image example/app:3", "resource": "Deployment/payments", "cite": ["k8s_rollout_history"]}]}},
    "knowledge": {"answer": {"findings": [], "gaps": ["no runbook matched"]}},
    "hypotheses": {"tools": ["evidence_list"],
                   "answer": {"hypotheses": [{"statement": "payments exits on start-up because it cannot reach its database", "cite": ["k8s_describe_pod", "k8s_pod_logs"],
                                              "would_confirm": "the database's reachability from the pod"},
                                             {"statement": "The new image is broken", "cite": ["k8s_rollout_history"], "would_confirm": "a rollback"},
                                             {"statement": "Something invented", "supporting": ["E77"]}]}},
    "review": {"tools": ["evidence_list"],
               "answer": {"verdicts": [{"id": "H1", "verdict": "supported", "reason": "the pod state and its logs agree"},
                                       {"id": "H2", "verdict": "insufficient", "reason": "one source only"}],
                          "missing_telemetry": ["no metrics for payments"], "evidence_requests": []}},
    "rca": {"answer": {"summary": "payments crash-loops: its containers exit on start-up after a database timeout.", "root_cause": "H1", "contributing": ["H2"],
                       "timeline": [], "impact": "payments has no ready replica", "next_steps": ["Check the database endpoint payments uses"]}},
}


@pytest.fixture
async def incident_on(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    s = get_settings()
    monkeypatch.setattr(s, "crewai_enabled", True)
    monkeypatch.setattr(s, "crewai_tool_timeout_seconds", 2)
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
    registry._cache.clear()


async def start(client, workload="payments", namespace="shop", **extra):
    env = f"ci-{uuid.uuid4().hex[:6]}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"{env}@example.com", [f"investigator:{env}:*"])
    h = await login(client, f"{env}@example.com")
    body = {"flow": "incident_investigation", "environment": env, "namespace": namespace, "workload": workload, **extra}
    r = await client.post("/moks/api/investigations/crewai", json=body, headers=h)
    return r, h


async def result(iid):
    async with session_scope() as db:
        inv = await db.get(Investigation, uuid.UUID(iid))
        ev = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id))).scalars().all()
        events = (await db.execute(select(InvestigationEvent).where(InvestigationEvent.investigation_id == inv.id).order_by(InvestigationEvent.seq))).scalars().all()
        return inv, {e.ref: e for e in ev}, events


async def test_seven_agents_produce_a_graded_rca(client, incident_on):
    script = ScriptedSRE(HAPPY)
    incident_on.setattr(budget, "complete", script)
    written = []
    original = Investigator._checkpoint

    async def spy(self, node, next_node, state):
        written.append((node, next_node, (state.get("crewai") or {}).get("done")))
        await original(self, node, next_node, state)
    incident_on.setattr(Investigator, "_checkpoint", spy)
    r, _ = await start(client, change_window="6h")
    assert r.status_code == 201, r.text
    iid = r.json()["id"]
    await Investigator(uuid.UUID(iid), "t").run()
    inv, ev, events = await result(iid)
    assert inv.status == "completed", inv.error
    crew = inv.report["crewai"]
    res = crew["investigation"]
    assert crew["flow"] == "incident_investigation" and crew["status"] == "completed", crew.get("stopped")
    assert [a["agent"] for a in crew["agents"]] == ["plan", "kubernetes", "observability", "change", "knowledge", "hypotheses", "review", "rca"]
    assert {s for s, _ in script.calls} >= {"plan", "kubernetes", "change", "hypotheses", "review", "rca"}
    findings = {f["observation"]: f for f in res["findings"]}
    crash = findings[f"Pod {POD} is in CrashLoopBackOff with 7 restarts"]
    assert crash["status"] == "confirmed_observation" and crash["resource_verified"] and crash["citations"][0] in ev
    assert set(crash) >= {"id", "observation", "evidence_source", "resource", "timestamp", "time_range", "citations", "confidence", "criteria", "status", "hypotheses"}
    assert "H1" in crash["hypotheses"]
    assert "E99" in crew["validation"]["citations_rejected"] and "E77" in crew["validation"]["citations_rejected"]
    h = {x["id"]: x for x in res["hypotheses"]}
    assert h["H1"]["status"] == "strongly_supported" and h["H1"]["rank"] == 1  # live evidence from two kinds of signal, upheld by the reviewer
    assert h["H2"]["status"] == "possible_explanation"  # one kind of signal, the reviewer found it insufficient
    assert {c["name"] for c in h["H1"]["criteria"] if c["met"]} >= {"live_support", "independent_signals", "reviewer_upheld", "no_contradiction"}
    assert res["root_cause"]["hypothesis"] == "H1" and res["severity"]["proposed"] == "SEV1"
    assert res["severity"]["level"] in ("SEV1", "SEV2") and any("0 of 1 replicas ready" in c for c in res["severity"]["criteria"])
    assert [a["resource"] for a in res["affected"]] == ["Deployment/payments"]  # billing is not in the evidence it cites
    # the report: confirmed observations as facts, graded hypotheses as causes
    rep = inv.report
    assert any("CrashLoopBackOff" in f["statement"] for f in rep["observed_facts"])
    assert rep["suspected_causes"][0]["confidence"] == "high"
    assert "no metrics for payments" in rep["missing_evidence"]
    assert any(t["task"] == "plan" and t["status"] == "done" for t in res["tasks"])
    assert crew["usage"]["total"]["model_calls"] >= 10 and set(crew["usage"]["by_agent"]) >= {"commander", "kubernetes", "reviewer", "rca"}
    # streaming: one phase per step, and the console's steps
    phases = [e.payload.get("phase") for e in events if e.type == "phase"]
    assert phases.count("crewai_plan") == 1 and "crewai_review" in phases and "crewai_rca" in phases
    async with session_scope() as db:
        steps = {s.step: s for s in (await db.execute(select(CrewRunStep).where(CrewRunStep.investigation_id == inv.id))).scalars()}
        assert await db.get(InvestigationCheckpoint, inv.id) is None  # removed once the investigation finished
    assert {"setup", "plan", "kubernetes", "hypotheses", "review", "rca", "validate"} <= set(steps)
    assert steps["kubernetes"].tool_calls == 2 and steps["kubernetes"].model_calls >= 4  # workload_pods was read while resolving the pods: served from the registry
    assert [n for n, _, _ in written] == ["crewai:plan", "crewai:kubernetes", "crewai:observability", "crewai:change", "crewai:knowledge", "crewai:hypotheses",
                                         "crewai:review", "crewai:rca"]
    assert written[3][1] == "crewai:knowledge" and written[-1][2][-1] == "rca"
