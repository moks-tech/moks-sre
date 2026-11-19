"""The CrewAI console (docs/crewai-console.md), the parts that run without the crewai package: configuration
versions and their checks, cron schedules and the dispatcher, run numbering and history, permissions, settings,
retention and import / export. A crew running end to end with the console's steps, log and transcripts is in
test_crewai_crew.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text, update

from moks import crew, versions
from moks.config import get_settings
from moks.crew import config as crew_config
from moks.crew import console, cron
from moks.crew.history import History, purge
from moks.db import session_scope
from moks.models import AuditEvent, CrewRun, CrewSchedule, Investigation, InvestigationEvent, LLMConfig, RoleBinding
from tests.conftest import login, make_env, make_user

FLOW = crew_config.FLOW


@pytest.fixture
async def on(monkeypatch):
    """CrewAI switched on, a chat model active and a CrewAI worker reporting, as on mk4."""
    monkeypatch.setattr(get_settings(), "crewai_enabled", True)
    async with session_scope() as db:
        for c in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat"))).scalars():
            c.is_active = False
        await db.flush()
        cfg = LLMConfig(name=f"c-{uuid.uuid4().hex[:6]}", purpose="chat", provider="openai", model="m", is_active=True)
        db.add(cfg)
        await db.execute(text("DELETE FROM crew_settings"))
        await db.execute(text("UPDATE crew_configs SET active = false"))
        await db.execute(text("UPDATE crew_schedules SET enabled = false"))
    await versions.report(crew.WORKER, crew.PINNED)
    yield monkeypatch
    async with session_scope() as db:
        (await db.get(LLMConfig, cfg.id)).is_active = False
        await db.execute(text("DELETE FROM crew_settings"))
        await db.execute(text("UPDATE crew_configs SET active = false"))


async def people(client):
    """An environment, an investigator in it, an admin and a viewer of another environment."""
    tag = uuid.uuid4().hex[:6]
    env = f"cc-{tag}"
    await make_env(env, connectors=[{"type": "kubernetes", "name": "k", "config": {}}])
    await make_user(f"inv-{tag}@example.com", [f"investigator:{env}:*"])
    await make_user(f"adm-{tag}@example.com", ["admin"])
    await make_user(f"other-{tag}@example.com", ["investigator:elsewhere:*"])
    return env, tag


async def as_(client, email):
    client.cookies.clear()
    return await login(client, email)


async def run_one(client, h, env, workload="payments", **extra):
    r = await client.post(f"/moks/api/crewai/flows/{FLOW}/run", json={"inputs": {"environment": env, "namespace": "shop", "workload": workload, **extra}}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# ---- configuration --------------------------------------------------------------------

def test_the_code_defaults_pass_their_own_checks_and_an_overlay_keeps_only_edits():
    assert crew_config.check({}) == ([], [])
    merged = crew_config.merge({"agents": {"workload": {"role": "Pod detective"}}, "limits": {"max_iterations": 3}})
    assert merged["agents"]["workload"]["role"] == "Pod detective" and merged["agents"]["change"]["role"] == "Change investigator"
    assert crew_config.overlay_of(merged) == {"agents": {"workload": {"role": "Pod detective"}}, "limits": {"max_iterations": 3}}
    assert [d["field"] for d in crew_config.diff(crew_config.overlay_of(merged))] == ["agents.workload.role", "limits.max_iterations"]


@pytest.mark.parametrize("overlay,field,needle", [
    ({"agents": {"workload": {"tools": ["pod_logs", "exec_pod"]}}}, "agents.workload.tools", "not on this agent's allowlist"),
    ({"agents": {"change": {"tools": ["pod_logs"]}}}, "agents.change.tools", "not on this agent's allowlist"),  # a tool keeps its agent
    ({"agents": {"review": {"tools": ["pod_logs"]}}}, "agents.review.tools", "not editable"),
    ({"agents": {"workload": {"role": "  "}}}, "agents.workload.role", "must not be empty"),
    ({"agents": {"change": {"role": "Workload investigator"}}}, "agents", "its own role"),
    ({"agents": {"hacker": {"role": "x"}}}, "agents.hacker", "unknown"),
    ({"tasks": {"review": {"description": "x" * 7000}}}, "tasks.review.description", "at most"),
    ({"limits": {"max_tool_calls": 10_000}}, "limits.max_tool_calls", "a whole number"),
    ({"limits": {"timeout_seconds": True}}, "limits.timeout_seconds", "a whole number"),
    ({"structure": "hierarchical"}, "structure", "unknown section"),
    ({"defaults": {"kind": "Pod"}}, "defaults.kind", "one of"),
])
def test_checks_refuse_what_is_code_owned_or_out_of_bounds(overlay, field, needle):
    errors, _ = crew_config.check(overlay)
    assert any(e["field"] == field and needle in e["message"] for e in errors), errors


def test_checks_warn_without_blocking():
    errors, warnings = crew_config.check({"tasks": {"workload": {"description": "Look at {workload}."}}})
    assert not errors
    fields = {w["field"] for w in warnings}
    assert "tasks.workload.description" in fields  # {workload} is not a placeholder, and {target} went missing
    assert "tasks" in fields  # and so did {question}


def test_placeholders_are_replaced_not_formatted():
    assert crew_config.fill("{target} {question} {\"json\": 1}", "T", "Q") == 'T Q {"json": 1}'


# ---- cron -------------------------------------------------------------------------------

def test_cron_next_times_and_shorthands():
    friday = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    assert cron.next_after(cron.parse("0 9 * * mon-fri"), friday) == datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    assert cron.next_after(cron.parse("@daily"), friday) == datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)
    assert cron.next_after(cron.parse("30 */6 * * *"), friday) == datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
    assert cron.next_after(cron.parse("0 0 1 jan *"), friday) == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)
    # both day fields restricted: either matches, as in cron
    assert cron.next_after(cron.parse("0 0 13 * 5"), datetime(2026, 11, 1, tzinfo=timezone.utc)) == datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    assert cron.next_after(cron.parse("0 12 * * 7"), friday) == datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)  # 7 is Sunday


@pytest.mark.parametrize("expr,needle", [("* * * *", "five fields"), ("61 * * * *", "outside"), ("*/0 * * * *", "positive"), ("5-1 * * * *", "backwards"),
                                         ("* * * * *", "at most every 15 minutes"), ("*/5 * * * *", "at most every"), ("0 0 31 feb *", "never fires"), ("x * * * *", "not a number")])
def test_cron_refuses_bad_or_too_frequent_expressions(expr, needle):
    with pytest.raises(cron.CronError) as e:
        cron.validate(expr)
    assert needle in str(e.value)


# ---- runs ---------------------------------------------------------------------------------

async def test_two_events_in_flight_never_share_a_number(client, on):
    """Found while adding the console: a crew's posted "finished" note and its next model call emit at once, and the
    number was read after an await, so both could take it (UniqueViolation, and the crew stopped)."""
    import asyncio

    from moks.orchestrator.engine import Investigator

    env, tag = await people(client)
    run = await run_one(client, await as_(client, f"inv-{tag}@example.com"), env)
    inv = Investigator(uuid.UUID(run["id"]), "t")
    real = inv._fence

    async def slow(db):
        await asyncio.sleep(0.01)
        await real(db)
    inv._fence = slow
    await asyncio.gather(*(inv._emit("note", {"message": str(i)}) for i in range(5)))
    async with session_scope() as db:
        seqs = (await db.execute(select(InvestigationEvent.seq).where(InvestigationEvent.investigation_id == inv.id, InvestigationEvent.seq > 0))).scalars().all()
    assert sorted(seqs) == [1, 2, 3, 4, 5]


async def test_runs_are_numbered_per_flow_and_visible_only_where_the_investigation_is(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    a = await run_one(client, h, env)
    b = await run_one(client, h, env, workload="cart", kind="StatefulSet", minutes=30)
    assert b["run_number"] == a["run_number"] + 1 and a["trigger"] == "user"
    async with session_scope() as db:
        row = await db.get(CrewRun, uuid.UUID(b["id"]))
        queued = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == row.investigation_id, InvestigationEvent.seq == 0))).scalar_one()
    assert row.inputs == {"environment": env, "namespace": "shop", "kind": "StatefulSet", "workload": "cart", "minutes": 30}
    assert queued["crewai"]["workload"] == "cart" and queued["crewai"]["flow"] == FLOW and queued["crewai"]["config_version"] is None

    listed = (await client.get("/moks/api/crewai/runs", params={"environment": env})).json()
    assert [r["number"] for r in listed["runs"]] == [b["run_number"], a["run_number"]] and listed["active"] == 2
    assert [r["id"] for r in (await client.get("/moks/api/crewai/runs", params={"q": f"#{a['run_number']}", "flow": FLOW})).json()["runs"]] == [a["id"]]
    assert [r["id"] for r in (await client.get("/moks/api/crewai/runs", params={"q": "cart", "environment": env})).json()["runs"]] == [b["id"]]
    detail = (await client.get(f"/moks/api/crewai/runs/{b['id']}")).json()
    assert [s["status"] for s in detail["steps"]] == ["pending"] * 5 and detail["prev"]["id"] == a["id"] and detail["config"]["label"] == "the code's defaults"
    assert detail["can"]["cancel"] and not detail["can"]["rerun"]

    hx = await as_(client, f"other-{tag}@example.com")
    assert all(r["environment"] != env for r in (await client.get("/moks/api/crewai/runs")).json()["runs"])
    assert (await client.get(f"/moks/api/crewai/runs/{a['id']}")).status_code == 404
    assert (await client.get(f"/moks/api/crewai/runs/{a['id']}/logs")).status_code == 404
    assert (await client.post(f"/moks/api/crewai/runs/{a['id']}/rerun", json={}, headers=hx)).status_code == 404


async def test_bad_inputs_are_refused_with_the_field(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    for inputs, needle in (({"environment": env, "namespace": "Shop!", "workload": "x"}, "namespace"), ({"environment": env, "namespace": "shop"}, "workload: required"),
                           ({"environment": env, "namespace": "shop", "workload": "x", "kind": "Pod"}, "kind"),
                           ({"environment": env, "namespace": "shop", "workload": "x", "command": "rm"}, "not an input")):
        r = await client.post(f"/moks/api/crewai/flows/{FLOW}/run", json={"inputs": inputs}, headers=h)
        assert r.status_code == 422 and needle in r.json()["detail"], r.text
    r = await client.post(f"/moks/api/crewai/flows/{FLOW}/run", json={"inputs": {"environment": env, "namespace": "secure", "workload": "x"}},
                          headers=await as_(client, f"other-{tag}@example.com"))
    assert r.status_code == 403


async def test_the_legacy_route_is_the_console_flow(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    r = await client.post("/moks/api/investigations/crewai", json={"environment": env, "namespace": "shop", "workload": "payments"}, headers=h)
    assert r.status_code == 201, r.text
    async with session_scope() as db:
        assert (await db.get(CrewRun, uuid.UUID(r.json()["id"]))).flow == FLOW


async def test_log_lines_steps_and_text_download(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    run = await run_one(client, h, env)
    hist = await History(uuid.UUID(run["id"])).open()
    assert hist.enabled and hist.transcripts
    await hist.start("setup", "Set up run")
    hist.line("first")
    await hist.start("workload", "Workload investigator")
    cid = await hist.call("workload", provider="openai", model="m", system="sys", turns=[{"role": "user", "content": "hello"}], response="answer", tokens_in=3,
                          tokens_out=2, cost=0.01, duration_ms=5)
    hist.count("workload", model=True, tokens_in=3, tokens_out=2, cost=0.01)
    hist.line("Model call 1", data={"event": "llm_call"}, call_id=cid)
    await hist.close("succeeded")
    page = (await client.get(f"/moks/api/crewai/runs/{run['id']}/logs")).json()
    assert [(l["step"], l["message"]) for l in page["lines"]] == [("setup", "first"), ("workload", "Model call 1")]
    assert [s["status"] for s in page["steps"]][:2] == ["succeeded", "succeeded"] and page["steps"][1]["model_calls"] == 1
    assert (await client.get(f"/moks/api/crewai/runs/{run['id']}/logs", params={"after": page["last"]})).json()["lines"] == []
    call = (await client.get(f"/moks/api/crewai/calls/{cid}")).json()
    assert call["system"] == "sys" and call["response"] == "answer" and call["prompt"][0]["content"] == "hello"
    textlog = await client.get(f"/moks/api/crewai/runs/{run['id']}/logs", params={"format": "text"})
    assert "attachment" in textlog.headers["content-disposition"] and "[workload] INFO    Model call 1" in textlog.text
    # a run that ended outside the crew: its open step takes the run's outcome, the rest are skipped
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == uuid.UUID(run["id"])).values(status="failed", finished_at=datetime.now(timezone.utc), error="boom"))
    detail = (await client.get(f"/moks/api/crewai/runs/{run['id']}")).json()
    assert [s["status"] for s in detail["steps"]] == ["succeeded", "succeeded", "skipped", "skipped", "skipped"] and "boom" in detail["errors"]
    assert detail["can"]["rerun"] and detail["cost"]["by_agent"][0]["step"] == "workload"
    await as_(client, f"other-{tag}@example.com")
    assert (await client.get(f"/moks/api/crewai/calls/{cid}")).status_code == 404


async def test_rerun_uses_the_same_inputs_and_links_back(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    run = await run_one(client, h, env, minutes=45)
    assert (await client.post(f"/moks/api/crewai/runs/{run['id']}/rerun", json={}, headers=h)).status_code == 409  # still queued
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == uuid.UUID(run["id"])).values(status="completed", finished_at=datetime.now(timezone.utc)))
    again = (await client.post(f"/moks/api/crewai/runs/{run['id']}/rerun", json={"inputs": {"minutes": 90}}, headers=h)).json()
    assert again["trigger"] == "rerun"
    async with session_scope() as db:
        row = await db.get(CrewRun, uuid.UUID(again["id"]))
    assert row.rerun_of == uuid.UUID(run["id"]) and row.inputs["minutes"] == 90 and row.inputs["workload"] == "payments"
    assert (await client.get(f"/moks/api/crewai/runs/{run['id']}")).json()["reruns"][0]["id"] == again["id"]
    cancelled = (await client.post(f"/moks/api/crewai/runs/{again['id']}/cancel", json={}, headers=h)).json()
    assert cancelled["status"] == "cancelling"


# ---- flows on and off ------------------------------------------------------------------------

async def test_a_flow_switched_off_cannot_start_and_says_why(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    assert (await client.patch(f"/moks/api/crewai/flows/{FLOW}", json={"enabled": False}, headers=h)).status_code == 403
    ha = await as_(client, f"adm-{tag}@example.com")
    r = await client.patch(f"/moks/api/crewai/flows/{FLOW}", json={"enabled": False, "reason": "model outage"}, headers=ha)
    assert r.status_code == 200 and r.json()["enabled"] is False
    h = await as_(client, f"inv-{tag}@example.com")
    r = await client.post(f"/moks/api/crewai/flows/{FLOW}/run", json={"inputs": {"environment": env, "namespace": "shop", "workload": "x"}}, headers=h)
    assert r.status_code == 409 and "model outage" in r.json()["detail"]
    cfg = (await client.get("/moks/api/investigate/config")).json()["crewai"]
    assert cfg["available"] is False and "switched off" in cfg["reason"]
    assert (await client.get("/moks/api/crewai/flows")).json()["flows"][0]["enabled"] is False
    async with session_scope() as db:
        assert (await db.execute(select(AuditEvent).where(AuditEvent.action == "crewai.flow.disable"))).first() is not None


# ---- crew versions ------------------------------------------------------------------------------

async def test_versions_are_saved_activated_and_recorded_per_run(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    assert (await client.post("/moks/api/crewai/crews/workload/versions", json={"config": {}}, headers=h)).status_code == 403
    ha = await as_(client, f"adm-{tag}@example.com")
    bad = await client.post("/moks/api/crewai/crews/workload/versions", json={"config": {"agents": {"workload": {"tools": ["exec_pod"]}}}}, headers=ha)
    assert bad.status_code == 422 and "allowlist" in bad.json()["detail"]
    checked = (await client.post("/moks/api/crewai/crews/workload/check", json={"config": crew_config.merge({"agents": {"workload": {"role": "Pod detective"}}})}, headers=ha)).json()
    assert checked["errors"] == [] and checked["overlay"] == {"agents": {"workload": {"role": "Pod detective"}}}
    saved = (await client.post("/moks/api/crewai/crews/workload/versions", json={"config": checked["effective"], "note": "rename", "activate": True}, headers=ha)).json()
    v = saved["version"]
    detail = (await client.get("/moks/api/crewai/crews/workload")).json()
    assert detail["active"]["version"] == v and detail["effective"]["agents"]["workload"]["role"] == "Pod detective"
    assert detail["diff"] == [{"field": "agents.workload.role", "default": "Workload investigator", "value": "Pod detective"}]
    assert "untrusted_data" in detail["code_owned"]["rules"] and "JSON" in detail["code_owned"]["output_contract"]
    run = await run_one(client, ha, env)
    async with session_scope() as db:
        assert (await db.get(CrewRun, uuid.UUID(run["id"]))).config_version == v
    assert (await client.get(f"/moks/api/crewai/runs/{run['id']}")).json()["steps"][1]["label"] == "Pod detective"
    # back to the code's defaults; the queued run keeps the version it recorded
    assert (await client.post("/moks/api/crewai/crews/workload/activate", json={"version": None}, headers=ha)).json()["active_version"] is None
    assert (await client.get(f"/moks/api/crewai/runs/{run['id']}")).json()["config"]["version"] == v
    crew_cfg, used = await console.run_config({"config_version": v})
    assert crew_cfg["agents"]["workload"]["role"] == "Pod detective" and used == f"version {v}"
    assert (await client.post("/moks/api/crewai/crews/workload/activate", json={"version": 9999}, headers=ha)).status_code == 404


async def test_a_test_run_uses_the_draft_and_can_be_saved(client, on):
    env, tag = await people(client)
    ha = await as_(client, f"adm-{tag}@example.com")
    base = await run_one(client, ha, env)
    draft = {"tasks": {"workload": {"description": "Target: {target}.\n{question}\nOnly read the pods."}}}
    assert (await client.post("/moks/api/crewai/crews/workload/test", json={"config": draft, "run_id": base["id"]}, headers=ha)).status_code == 409  # not finished
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == uuid.UUID(base["id"])).values(status="completed", finished_at=datetime.now(timezone.utc)))
    t = (await client.post("/moks/api/crewai/crews/workload/test", json={"config": draft, "run_id": base["id"]}, headers=ha)).json()
    assert t["trigger"] == "test"
    async with session_scope() as db:
        row = await db.get(CrewRun, uuid.UUID(t["id"]))
        queued = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == row.investigation_id, InvestigationEvent.seq == 0))).scalar_one()
        audit_row = (await db.execute(select(AuditEvent).where(AuditEvent.resource_id == t["id"], AuditEvent.action == "investigation.create"))).scalar_one()
    assert row.rerun_of == uuid.UUID(base["id"]) and row.inputs == {"environment": env, "namespace": "shop", "kind": "Deployment", "workload": "payments"}
    assert queued["crewai"]["draft"] == draft and "draft" not in audit_row.details  # the prompts stay out of the audit log
    detail = (await client.get(f"/moks/api/crewai/runs/{t['id']}")).json()
    assert detail["config"]["draft"] is True and not detail["can"]["save_version"]  # not finished
    async with session_scope() as db:
        await db.execute(update(Investigation).where(Investigation.id == uuid.UUID(t["id"])).values(status="completed", finished_at=datetime.now(timezone.utc)))
    assert (await client.post(f"/moks/api/crewai/runs/{t['id']}/rerun", json={}, headers=ha)).status_code == 409
    saved = (await client.post(f"/moks/api/crewai/runs/{t['id']}/save-version", json={"activate": False}, headers=ha)).json()
    v = (await client.get(f"/moks/api/crewai/crews/workload/versions/{saved['version']}")).json()
    assert v["overlay"] == draft and v["active"] is False and v["note"].startswith("From test run #")


# ---- schedules -------------------------------------------------------------------------------

async def _schedule(client, ha, env, name=None, expr="0 * * * *"):
    name = name or f"hourly payments {uuid.uuid4().hex[:6]}"
    r = await client.post("/moks/api/crewai/schedules", json={"name": name, "cron": expr, "inputs": {"environment": env, "namespace": "shop", "workload": "payments"}}, headers=ha)
    assert r.status_code == 201, r.text
    return r.json()


async def _due(sid, ago=timedelta(minutes=1)):
    at = datetime.now(timezone.utc).replace(microsecond=0) - ago
    async with session_scope() as db:
        await db.execute(update(CrewSchedule).where(CrewSchedule.id == uuid.UUID(sid)).values(next_run_at=at))
    return at


async def test_schedules_need_admin_a_valid_cron_and_the_saver_s_access(client, on):
    env, tag = await people(client)
    h = await as_(client, f"inv-{tag}@example.com")
    body = {"name": "x", "cron": "0 * * * *", "inputs": {"environment": env, "namespace": "shop", "workload": "payments"}}
    assert (await client.post("/moks/api/crewai/schedules", json=body, headers=h)).status_code == 403
    ha = await as_(client, f"adm-{tag}@example.com")
    r = await client.post("/moks/api/crewai/schedules", json={**body, "cron": "* * * * *"}, headers=ha)
    assert r.status_code == 422 and "15 minutes" in r.json()["detail"]
    s = await _schedule(client, ha, env)
    assert s["enabled"] and s["owner"] == f"adm-{tag}@example.com" and len(s["upcoming"]) == 3
    assert (await client.post("/moks/api/crewai/schedules", json={**body, "name": s["name"]}, headers=ha)).status_code == 409
    preview = (await client.get("/moks/api/crewai/cron", params={"expr": "0 9 * * mon-fri"})).json()
    assert preview["valid"] and len(preview["next"]) == 5
    paused = (await client.patch(f"/moks/api/crewai/schedules/{s['id']}", json={"enabled": False}, headers=ha)).json()
    assert paused["enabled"] is False and paused["next_run_at"] is None and paused["version"] == 2
    resumed = (await client.patch(f"/moks/api/crewai/schedules/{s['id']}", json={"enabled": True}, headers=ha)).json()
    assert datetime.fromisoformat(resumed["next_run_at"]) > datetime.now(timezone.utc)  # never a time already past
    async with session_scope() as db:
        actions = set((await db.execute(select(AuditEvent.action).where(AuditEvent.resource_id == s["id"]))).scalars())
    assert {"crewai.schedule.create", "crewai.schedule.pause", "crewai.schedule.resume"} <= actions
    assert (await client.delete(f"/moks/api/crewai/schedules/{s['id']}", headers=ha)).json()["status"] == "deleted"


async def test_the_dispatcher_starts_a_due_schedule_once_as_its_owner(client, on):
    env, tag = await people(client)
    ha = await as_(client, f"adm-{tag}@example.com")
    s = await _schedule(client, ha, env)
    await _due(s["id"])
    done = [d for d in await console.dispatch_due() if d["schedule"] == s["id"]]
    assert [d["outcome"] for d in done] == ["started"]
    assert not [d for d in await console.dispatch_due() if d["schedule"] == s["id"]]  # moved on: no second start
    async with session_scope() as db:
        row = await db.get(CrewSchedule, uuid.UUID(s["id"]))
        run = await db.get(CrewRun, uuid.UUID(done[0]["investigation_id"]))
        inv = await db.get(Investigation, run.investigation_id)
    assert row.last_run_id == run.investigation_id and row.last_error is None and row.next_run_at > datetime.now(timezone.utc)
    assert run.trigger == "schedule" and run.schedule_id == row.id and inv.trigger == "schedule" and inv.status == "queued"
    assert inv.created_by == row.owner_id and inv.conversation_id == inv.id
    listed = (await client.get("/moks/api/crewai/schedules")).json()["schedules"]
    assert next(x for x in listed if x["id"] == s["id"])["last_run"]["number"] == run.run_number
    assert (await client.get(f"/moks/api/crewai/runs/{run.investigation_id}")).json()["run"]["schedule"] == s["name"]


async def test_a_schedule_whose_owner_lost_access_or_whose_flow_is_off_records_why(client, on):
    env, tag = await people(client)
    owner = await make_user(f"own-{tag}@example.com", ["admin"])
    ha = await as_(client, f"own-{tag}@example.com")
    s = await _schedule(client, ha, env)
    async with session_scope() as db:
        await db.execute(text("DELETE FROM role_bindings WHERE user_id = :u"), {"u": owner})
        db.add(RoleBinding(user_id=owner, role="viewer", environment="*", namespace="*"))
    await _due(s["id"])
    done = next(d for d in await console.dispatch_due() if d["schedule"] == s["id"])
    assert done["outcome"] == "refused" and "may no longer run it" in done["error"]
    async with session_scope() as db:
        assert "investigation.run" in (await db.get(CrewSchedule, uuid.UUID(s["id"]))).last_error
    ha = await as_(client, f"adm-{tag}@example.com")
    s2 = await _schedule(client, ha, env)
    await client.patch(f"/moks/api/crewai/flows/{FLOW}", json={"enabled": False}, headers=ha)
    await _due(s2["id"])
    assert "switched off" in next(d for d in await console.dispatch_due() if d["schedule"] == s2["id"])["error"]
    await client.patch(f"/moks/api/crewai/flows/{FLOW}", json={"enabled": True}, headers=ha)
    await _due(s2["id"], ago=timedelta(hours=2))  # nobody ran the scheduler for two hours: skipped, not caught up
    assert next(d for d in await console.dispatch_due() if d["schedule"] == s2["id"])["outcome"] == "missed"


async def test_the_worker_role_may_record_a_firing_but_not_edit_a_schedule(client, on):
    env, tag = await people(client)
    ha = await as_(client, f"adm-{tag}@example.com")
    s = await _schedule(client, ha, env)
    from moks import db_roles
    import asyncpg
    from tests.conftest import ADMIN_URL, DB_NAME

    conn = await asyncpg.connect(ADMIN_URL.rsplit("/", 1)[0] + f"/{DB_NAME}")
    await db_roles.apply(conn, DB_NAME)
    for sql in (f"UPDATE crew_schedules SET cron = '* * * * *' WHERE id = '{s['id']}'", f"UPDATE crew_schedules SET inputs = '{{}}' WHERE id = '{s['id']}'",
                "UPDATE crew_configs SET active = true", "INSERT INTO crew_settings (key, value) VALUES ('x', '{}')"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(f"SET ROLE moks_worker; {sql}")
        await conn.execute("RESET ROLE")
    await conn.execute(f"SET ROLE moks_worker; UPDATE crew_schedules SET last_error = 'x', next_run_at = now() WHERE id = '{s['id']}'")
    await conn.execute("RESET ROLE")
    await conn.close()


# ---- settings, retention, import and export ------------------------------------------------------

async def test_settings_switch_transcripts_off_and_retention_deletes_old_lines(client, on):
    env, tag = await people(client)
    ha = await as_(client, f"adm-{tag}@example.com")
    assert (await client.patch("/moks/api/crewai/settings", json={"transcripts": False, "transcript_days": 7}, headers=ha)).json()["history"]["transcripts"] is False
    assert (await client.patch("/moks/api/crewai/settings", json={"log_days": 0}, headers=ha)).status_code == 422
    run = await run_one(client, ha, env)
    hist = await History(uuid.UUID(run["id"])).open()
    assert hist.enabled and not hist.transcripts
    assert await hist.call("workload", provider="p", model="m", system="s", turns=[], response="r", tokens_in=0, tokens_out=0, cost=0, duration_ms=0) is None
    async with session_scope() as db:
        await db.execute(text("INSERT INTO crew_run_logs (investigation_id, step, at, message) VALUES (:i, 'setup', now() - interval '100 days', 'old'), (:i, 'setup', now(), 'new')"),
                         {"i": uuid.UUID(run["id"])})
    await purge()
    lines = (await client.get(f"/moks/api/crewai/runs/{run['id']}/logs")).json()["lines"]
    assert [l["message"] for l in lines] == ["new"]
    h = await as_(client, f"inv-{tag}@example.com")
    assert (await client.patch("/moks/api/crewai/settings", json={"transcripts": True}, headers=h)).status_code == 403


async def test_export_then_import_plans_first_and_only_adds_or_changes(client, on):
    env, tag = await people(client)
    ha = await as_(client, f"adm-{tag}@example.com")
    s = await _schedule(client, ha, env, name=f"exp-{tag}")
    await client.post("/moks/api/crewai/crews/workload/versions", json={"config": {"agents": {"review": {"goal": "Be brief."}}}, "activate": True}, headers=ha)
    data = (await client.get("/moks/api/crewai/export", headers=ha)).json()
    assert data["crews"]["workload"]["config"] == {"agents": {"review": {"goal": "Be brief."}}} and any(x["name"] == f"exp-{tag}" for x in data["schedules"])
    data["crews"]["workload"]["config"] = {"agents": {"review": {"goal": "Be thorough."}}}
    data["schedules"] = [{**x, "cron": "30 * * * *"} if x["name"] == f"exp-{tag}" else x for x in data["schedules"]] + [
        {"flow": FLOW, "name": f"new-{tag}", "cron": "0 6 * * *", "inputs": {"environment": env, "namespace": "shop", "workload": "cart"}, "enabled": True}]
    plan = (await client.post("/moks/api/crewai/import", json={"data": data}, headers=ha)).json()
    actions = {(p["kind"], p["name"]): p["action"] for p in plan["plan"]}
    assert not plan["applied"] and plan["errors"] == []
    assert actions[("crew", "workload")] == "change" and actions[("schedule", f"exp-{tag}")] == "change" and actions[("schedule", f"new-{tag}")] == "add"
    assert (await client.get("/moks/api/crewai/crews/workload")).json()["effective"]["agents"]["review"]["goal"] == "Be brief."  # a plan changes nothing
    applied = (await client.post("/moks/api/crewai/import", json={"data": data, "apply": True}, headers=ha)).json()
    assert applied["applied"]
    assert (await client.get("/moks/api/crewai/crews/workload")).json()["effective"]["agents"]["review"]["goal"] == "Be thorough."
    listed = {x["name"]: x for x in (await client.get("/moks/api/crewai/schedules")).json()["schedules"]}
    assert listed[f"exp-{tag}"]["cron"] == "30 * * * *" and f"new-{tag}" in listed and listed[f"exp-{tag}"]["id"] == s["id"]
    bad = (await client.post("/moks/api/crewai/import", json={"data": {**data, "crews": {"workload": {"config": {"limits": {"max_tool_calls": 10_000}}}}}, "apply": True}, headers=ha)).json()
    assert bad["errors"] and not bad["applied"]
    assert (await client.post("/moks/api/crewai/import", json={"data": {"format": "other"}}, headers=ha)).status_code == 422


async def test_viewers_read_the_console_but_change_nothing(client, on):
    env, tag = await people(client)
    await make_user(f"view-{tag}@example.com", [f"viewer:{env}:*"])
    hv = await as_(client, f"view-{tag}@example.com")
    for path in ("/moks/api/crewai/overview", "/moks/api/crewai/runs", "/moks/api/crewai/flows", f"/moks/api/crewai/flows/{FLOW}", "/moks/api/crewai/schedules",
                 "/moks/api/crewai/crews", "/moks/api/crewai/crews/workload", "/moks/api/crewai/settings"):
        r = await client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json().get("can_admin", False) is False
    assert (await client.get("/moks/api/crewai/export")).status_code == 403
    assert (await client.post(f"/moks/api/crewai/flows/{FLOW}/run", json={"inputs": {"environment": env, "namespace": "shop", "workload": "x"}}, headers=hv)).status_code == 403
    assert (await client.post("/moks/api/crewai/crews/workload/activate", json={"version": None}, headers=hv)).status_code == 403
