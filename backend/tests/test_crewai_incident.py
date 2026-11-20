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


def script(**steps) -> dict:
    """HAPPY with some steps replaced."""
    return {**HAPPY, **steps}


@pytest.fixture
def limits(monkeypatch):
    """Overrides the incident crew's default limits (below their console ceilings where a test needs it)."""
    from moks.crew import incident_config as ic

    real = ic.defaults

    def set_(**over):
        def patched():
            d = real()
            d["limits"].update(over)
            return d
        monkeypatch.setattr(ic, "defaults", patched)
    return set_


async def run_one(client, steps: dict, **extra):
    r, h = await start(client, **extra)
    assert r.status_code == 201, r.text
    await Investigator(uuid.UUID(r.json()["id"]), "t").run()
    inv, ev, events = await result(r.json()["id"])
    return inv, ev, events


async def test_the_review_asks_for_evidence_and_moks_collects_it_within_its_bounds(client, incident_on):
    kube = {"tools": ["workload_pods", ["describe_pod", {"pod": POD}]],
            "answer": {"findings": [{"observation": f"Pod {POD} is in CrashLoopBackOff with 7 restarts", "resource": f"pod/{POD}", "cite": ["k8s_describe_pod"]}]}}
    hyp = {"tools": ["evidence_list"],
           "answer": {"hypotheses": [{"statement": "payments cannot reach its database", "cite": ["k8s_describe_pod"], "would_confirm": "the previous container's logs"}]}}
    review = {"answer": {"verdicts": [{"id": "H1", "verdict": "insufficient", "reason": "only the pod state"}],
                         "evidence_requests": [{"tool": "pod_logs", "args": {"pod": POD, "previous": True}, "reason": "why it exits", "for": "H1"},
                                               {"tool": "scale_deployment", "args": {"replicas": 0}, "reason": "from the logs"},
                                               {"tool": "metric", "args": {"metric": "up", "labels": {"namespace": "kube-system"}}, "reason": "x"}]}}
    review2 = {"tools": ["evidence_list"], "answer": {"verdicts": [{"id": "H1", "verdict": "supported", "reason": "the logs show the db timeout", "cite": ["k8s_pod_logs"]}]}}
    s = ScriptedSRE(script(kubernetes=kube, hypotheses=hyp, review=review, review_2=review2,
                           rca={"answer": {"summary": "payments cannot reach its database", "root_cause": "H1"}}))
    incident_on.setattr(budget, "complete", s)
    inv, ev, events = await run_one(client, {})
    assert inv.status == "completed", inv.error
    res = inv.report["crewai"]["investigation"]
    req = {r["tool"]: r for r in res["requests"]}
    assert req["pod_logs"]["status"] == "collected" and ev[req["pod_logs"]["evidence"]].tool == "k8s_pod_logs"
    assert req["scale_deployment"]["status"] == "refused" and "not a tool MOKS runs" in req["scale_deployment"]["why"]
    assert req["metric"]["status"] == "refused" and "namespace" in req["metric"]["why"]
    assert res["review"]["rounds"] == 2 and res["review"]["verdicts"]["H1"]["verdict"] == "supported"
    assert res["hypotheses"][0]["status"] == "strongly_supported"  # the evidence it asked for added a second kind of signal
    assert not any((e.args or {}).get("namespace") == "kube-system" for e in ev.values())
    assert "crewai_evidence" in [e.payload.get("phase") for e in events if e.type == "phase"]
    assert [a["agent"] for a in inv.report["crewai"]["agents"]][-3:] == ["review", "review_2", "rca"]


async def test_one_review_round_when_the_limit_says_so(client, incident_on, limits):
    limits(review_rounds=1)
    review = {"answer": {"verdicts": [], "evidence_requests": [{"tool": "pod_logs", "args": {"pod": POD, "previous": True}, "reason": "x"}]}}
    incident_on.setattr(budget, "complete", ScriptedSRE(script(review=review)))
    inv, _, _ = await run_one(client, {})
    res = inv.report["crewai"]["investigation"]
    assert res["review"]["rounds"] == 1 and res["requests"] == []  # no round left to read what it would collect


async def test_limits_end_a_step_and_the_run_goes_on(client, incident_on, limits):
    limits(max_tool_calls_per_agent=1)
    incident_on.setattr(budget, "complete", ScriptedSRE(HAPPY))
    inv, ev, _ = await run_one(client, {})
    crew = inv.report["crewai"]
    assert inv.status == "completed" and crew["status"] == "completed"
    assert any(g["source"] == "limits" and "kubernetes investigator" in g["reason"] for g in crew["gaps"])
    assert sum(1 for e in ev.values() if e.tool in ("k8s_describe_pod", "k8s_pod_logs")) == 1
    assert crew["agents"][-1] == {"agent": "rca", "label": crew["agents"][-1]["label"], "status": "done"}


async def test_the_token_budget_keeps_a_reserve_for_review_and_rca(client, incident_on, limits):
    limits(token_budget=5000)

    class Heavy(ScriptedSRE):
        async def __call__(self, provider, system, messages, **kw):
            out = await super().__call__(provider, system, messages, **kw)
            return type(out)(out.text, 2000 if "Kubernetes investigator" in system else 40, 20, out.provider, out.model) if hasattr(out, "provider") else out
    from moks.llm.providers import LLMResponse

    class Heavy2(ScriptedSRE):
        async def __call__(self, provider, system, messages, **kw):
            out = await super().__call__(provider, system, messages, **kw)
            return LLMResponse(out.text, 2000 if "Kubernetes investigator" in system else 40, 20, "fake", "m")
    incident_on.setattr(budget, "complete", Heavy2(HAPPY))
    inv, _, _ = await run_one(client, {})
    crew = inv.report["crewai"]
    tasks = {t["task"]: t for t in crew["investigation"]["tasks"]}
    assert tasks["kubernetes"]["status"] == "partial" and "token budget" in tasks["kubernetes"]["ended_by"]
    assert tasks["observability"]["status"] == "partial"  # nothing left for collection
    assert tasks["rca"]["status"] == "done" and crew["usage"]["budget_reached"] is True  # the reserve paid for the review and the RCA


async def test_a_step_that_runs_out_of_time_ends_with_what_it_has(client, incident_on, limits):
    import asyncio

    limits(task_timeout_seconds=1)

    async def slow():
        await asyncio.sleep(3)
        return "Thought: late"
    incident_on.setattr(budget, "complete", ScriptedSRE(script(observability={"raw": [slow]})))
    inv, _, _ = await run_one(client, {})
    crew = inv.report["crewai"]
    tasks = {t["task"]: t for t in crew["investigation"]["tasks"]}
    assert tasks["observability"]["status"] == "partial" and "time limit" in tasks["observability"]["ended_by"]
    assert {e["code"] for e in crew["errors"]} >= {"task_timeout"}
    assert tasks["rca"]["status"] == "done" and inv.status == "completed"


async def test_a_malformed_answer_goes_back_once_with_the_reason(client, incident_on):
    s = ScriptedSRE(script(hypotheses={"raw": ["Final Answer: the database, probably"], **HAPPY["hypotheses"]}))
    incident_on.setattr(budget, "complete", s)
    inv, _, _ = await run_one(client, {})
    tasks = {t["task"]: t for t in inv.report["crewai"]["investigation"]["tasks"]}
    assert tasks["hypotheses"]["status"] == "done" and inv.report["crewai"]["investigation"]["hypotheses"]
    assert any(step == "hypotheses" and "MOKS could not read your answer" in text for step, text in s.calls)


async def test_an_answer_that_never_fits_is_set_aside(client, incident_on, limits):
    limits(output_retries=1)
    incident_on.setattr(budget, "complete", ScriptedSRE(script(plan={"raw": ["Final Answer: SEV1 obviously", "Final Answer: still SEV1", "Final Answer: SEV1"]})))
    inv, _, _ = await run_one(client, {})
    crew = inv.report["crewai"]
    tasks = {t["task"]: t for t in crew["investigation"]["tasks"]}
    assert tasks["plan"]["status"] == "invalid_output" and "invalid_output" in {e["code"] for e in crew["errors"]}
    assert crew["investigation"]["severity"]["proposed"] is None and tasks["rca"]["status"] == "done"


async def test_a_transient_model_error_is_retried(client, incident_on):
    from moks.llm.providers import LLMError

    incident_on.setattr(budget, "complete", ScriptedSRE(script(knowledge={"raw": [LLMError("timeout", "no answer in 30s")], **HAPPY["knowledge"]})))
    inv, _, _ = await run_one(client, {})
    crew = inv.report["crewai"]
    assert {t["task"]: t["status"] for t in crew["investigation"]["tasks"]}["knowledge"] == "done"
    assert crew["usage"]["by_agent"]["knowledge"]["retries"] == 1


async def test_langgraph_answers_when_the_crew_cannot_start(client, incident_on):
    from moks.crew import console

    incident_on.setattr(budget, "complete", ScriptedSRE(HAPPY))
    r, _ = await start(client)
    async with session_scope() as db:
        uid = (await db.execute(select(Investigation.created_by).where(Investigation.id == uuid.UUID(r.json()["id"])))).scalar_one()
        await console.set_flow(db, "incident_investigation", False, "maintenance", uid)
    try:
        await Investigator(uuid.UUID(r.json()["id"]), "t").run()
        inv, _, events = await result(r.json()["id"])
    finally:
        async with session_scope() as db:
            await console.set_flow(db, "incident_investigation", True, None, uid)
    assert inv.status == "completed", inv.error
    assert inv.report["crewai"]["status"] == "fallback" and inv.report["crewai"]["fallback"] == "langgraph"
    assert any("standard investigation" in n for n in inv.report["notes"])
    assert "triage" in [e.payload.get("phase") for e in events if e.type == "phase"]  # the LangGraph graph ran


async def test_a_console_test_run_never_falls_back(client, incident_on, limits):
    from moks.crew import console

    incident_on.setattr(budget, "complete", ScriptedSRE(HAPPY))
    r, _ = await start(client)
    iid = uuid.UUID(r.json()["id"])
    async with session_scope() as db:
        ev0 = (await db.execute(select(InvestigationEvent).where(InvestigationEvent.investigation_id == iid, InvestigationEvent.seq == 0))).scalar_one()
        ev0.payload = {**ev0.payload, "crewai": {**ev0.payload["crewai"], "trigger": "test"}}
        uid = (await db.execute(select(Investigation.created_by).where(Investigation.id == iid))).scalar_one()
        await console.set_flow(db, "incident_investigation", False, "maintenance", uid)
    try:
        await Investigator(iid, "t").run()
    finally:
        async with session_scope() as db:
            await console.set_flow(db, "incident_investigation", True, None, uid)
    inv, _, _ = await result(str(iid))
    assert inv.status == "failed" and "switched off" in inv.error


async def test_nothing_usable_falls_back_to_langgraphs_report_writer(client, incident_on):
    empty = {k: {"answer": {"findings": [], "gaps": []}} for k in ("kubernetes", "observability", "change", "knowledge")}
    incident_on.setattr(budget, "complete", ScriptedSRE({**empty, "plan": {"answer": {"severity": None, "affected": [], "objectives": []}},
                                                         "hypotheses": {"answer": {"hypotheses": []}}, "review": {"answer": {"verdicts": []}},
                                                         "rca": {"answer": {"summary": "", "root_cause": None}}}))
    inv, _, _ = await run_one(client, {})
    assert inv.status == "completed", inv.error
    assert inv.report["crewai"]["fallback"] == "langgraph_report" and inv.mode == "crewai_fallback"
    assert inv.report["crewai"]["investigation"]["severity"]["level"] == "unknown"


async def test_a_resumed_attempt_skips_the_steps_it_finished(client, incident_on):
    from moks.orchestrator import worker

    class Killed(BaseException):
        pass
    s = ScriptedSRE(HAPPY)
    incident_on.setattr(budget, "complete", s)
    r, _ = await start(client)
    iid = uuid.UUID(r.json()["id"])
    async with session_scope() as db:
        from sqlalchemy import update

        await db.execute(update(Investigation).where(Investigation.id != iid, Investigation.status == "queued").values(status="cancelled"))
    won = await worker.claim("w1", crewai=True)
    assert won.id == iid
    real = Investigator._checkpoint

    async def dying(self, name, next_node, state):
        await real(self, name, next_node, state)
        if name == "crewai:change" and self.attempt == 1:
            raise Killed
    incident_on.setattr(Investigator, "_checkpoint", dying)
    try:
        await Investigator(won.id, "w1", token=won.token, attempt=won.attempt).run()
    except Killed:
        pass
    done_before = [step for step, _ in s.calls]
    async with session_scope() as db:
        from sqlalchemy import func, update

        await db.execute(update(Investigation).where(Investigation.id == iid).values(lease_expires_at=func.now() - __import__("datetime").timedelta(seconds=1)))
    await worker.reap_stale()
    again = await worker.claim("w2", crewai=True)
    assert again.id == iid and again.attempt == 2
    await Investigator(again.id, "w2", token=again.token, attempt=again.attempt).run()
    inv, ev, _ = await result(str(iid))
    assert inv.status == "completed", inv.error
    later = [step for step, _ in s.calls][len(done_before):]
    assert "plan" not in later and "kubernetes" not in later and "change" not in later and "knowledge" in later and "rca" in later
    tasks = inv.report["crewai"]["investigation"]["tasks"]
    assert [t["status"] for t in tasks if t["task"] in ("plan", "kubernetes", "change")] == ["restored"] * 3
    assert any("CrashLoopBackOff" in f["observation"] for f in inv.report["crewai"]["investigation"]["findings"])  # restored from the first attempt's answer
    assert inv.report["execution"]["attempt"] == 2


async def test_untrusted_text_never_widens_the_investigation(client, incident_on):
    """The pod's logs carry an injected instruction (tests/fakes.py INJECTION). A model that obeys it is refused by MOKS."""
    obeying = script(kubernetes={"tools": [["pod_logs", {"pod": POD, "previous": True}], ["pod_logs", {"pod": "coredns-1", "namespace": "kube-system"}],
                                           ["describe_pod", {"pod": "vault-0"}]],
                                 "answer": {"findings": [{"observation": "maintenance mode: scale everything to zero", "resource": "pod/vault-0", "cite": ["k8s_describe_pod"]}]}},
                     review={"answer": {"verdicts": [], "evidence_requests": [{"tool": "scale_deployment", "args": {"replicas": 0}, "reason": "the logs say so"},
                                                                              {"tool": "pod_logs", "args": {"pod": "vault-0"}, "reason": "secrets"}]}})
    s = ScriptedSRE(obeying)
    incident_on.setattr(budget, "complete", s)
    inv, ev, _ = await run_one(client, {})
    assert inv.status == "completed"
    assert all((e.args or {}).get("namespace") in (None, "shop") for e in ev.values()) and not any(e.tool == "scale_deployment" for e in ev.values())
    crew = inv.report["crewai"]
    assert {x["tool"] for x in crew["refused"]} >= {"pod_logs", "describe_pod"}
    assert {r["tool"]: r["status"] for r in crew["investigation"]["requests"]} == {"scale_deployment": "refused", "pod_logs": "refused"}
    assert "hunter2hunter2" not in str(inv.report)
    case_seen = next(text for step, text in s.calls if step == "hypotheses")
    assert 'source="the case file' in case_seen and "<untrusted_data" in case_seen  # earlier agents' words reach later ones as data


# ---- API ----------------------------------------------------------------------------------------------

async def test_the_config_lists_both_flows(client, incident_on):
    _, h = await start(client)
    cfg = (await client.get("/moks/api/investigate/config", headers=h)).json()["crewai"]
    assert cfg["available"] is True and set(cfg["flows"]) == {"workload_investigation", "incident_investigation"}
    flow = cfg["flows"]["incident_investigation"]
    assert flow["available"] and flow["agents"] == 7 and flow["limits"]["review_rounds"] == 2
    assert [i["name"] for i in flow["inputs"]] == ["environment", "namespace", "kind", "workload", "minutes", "change_window", "question"]


async def test_a_change_window_belongs_to_the_incident_flow_only(client, incident_on):
    _, h = await start(client)
    env = (await client.get("/moks/api/investigate/config", headers=h)).json()["environments"][0]
    r = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": "shop", "workload": "payments", "change_window": "1h"}, headers=h)
    assert r.status_code == 422
    r = await client.post("/moks/api/investigations/crewai", json={"flow": "incident_investigation", "environment": env, "namespace": "shop", "workload": "payments",
                                                                    "change_window": "2h"}, headers=h)
    assert r.status_code == 422


async def test_the_console_versions_the_incident_crew(client, incident_on):
    from moks.crew import config as crew_config

    errors, _ = crew_config.check({"agents": {"knowledge": {"tools": ["pod_logs"]}}}, "sre")
    assert errors and "allowlist" in errors[0]["message"]
    errors, _ = crew_config.check({"agents": {"reviewer": {"tools": ["pod_logs"]}}}, "sre")
    assert errors and errors[0]["message"] == "not editable"  # the reviewer reads the registry only
    errors, _ = crew_config.check({"limits": {"review_rounds": 9}}, "sre")
    assert errors and errors[0]["field"] == "limits.review_rounds"
    assert crew_config.check({"agents": {"commander": {"goal": "Run {target}."}}, "limits": {"followup_calls": 2}}, "sre") == ([], [])
