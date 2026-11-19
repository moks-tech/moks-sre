"""The CrewAI console (docs/crewai-console.md): run history with logs like GitHub Actions, the flow catalog with Run
with parameters, schedules, versioned crew configuration with Test run, settings and import / export.

Reading follows investigations: a run is visible to whoever may read its investigation (investigation.read in its
environment and namespace), and lists leave the rest out. Starting a run (Run, Re-run, Run now, Test run) needs
investigation.run where it runs, checked by the same start_investigation every investigation passes. Changing the
console (crew versions, schedules, flows on or off, settings, import) needs crewai.admin, which only the admin role
holds; every change is audited as crewai.*.
"""
from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext, client_ip
from ..config import get_settings
from ..crew import config as crew_config
from ..crew import console, cron
from ..crew.history import HISTORY_DEFAULTS
from ..crew.tools import BY_NAME
from ..db import get_db
from ..models import Conversation, CrewConfig, CrewRun, CrewRunCall, CrewRunLog, CrewRunStep, CrewSchedule, Environment, Investigation, InvestigationEvent, LLMUsage, User
from ..rbac import P
from .investigations import TERMINAL, CreateIn, _load_visible, start_investigation

router = APIRouter(prefix="/crewai", tags=["crewai"])
admin_only = needs(P.CREWAI_ADMIN)

PAGE = 50
SCAN = 5000  # newest runs considered for a list; older ones are reached through the flow's history
STEP_LABELS = {"setup": "Set up run", "validate": "Validate the report"}
STATUSES = ("queued", "running", "completed", "failed", "cancelled")


# ---- shared ---------------------------------------------------------------------------

def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _seconds(inv: Investigation) -> float | None:
    if inv.started_at is None:
        return None
    end = inv.finished_at or datetime.now(timezone.utc)
    return max(0.0, (end - inv.started_at).total_seconds())


def _crew_status(inv: Investigation) -> str | None:
    return ((inv.report or {}).get("crewai") or {}).get("status")


def _row(run: CrewRun, inv: Investigation, env: str, email: str | None, schedule: str | None = None) -> dict[str, Any]:
    flow = crew_config.FLOWS.get(run.flow) or {}
    i = run.inputs or {}
    return {"id": str(inv.id), "number": run.run_number, "flow": run.flow, "flow_label": flow.get("label", run.flow), "trigger": run.trigger,
            "status": inv.status, "crew_status": _crew_status(inv), "environment": env, "namespace": inv.namespace, "workload": i.get("workload"), "kind": i.get("kind"),
            "question": inv.question, "actor": email, "created_at": _iso(run.created_at), "started_at": _iso(inv.started_at), "finished_at": _iso(inv.finished_at),
            "seconds": _seconds(inv), "cost_usd": str(inv.cost_usd), "tokens_in": inv.tokens_in, "tokens_out": inv.tokens_out, "tool_calls": inv.tool_calls,
            "error": inv.error, "summary": (inv.report or {}).get("summary") if inv.report else None, "config_version": run.config_version,
            "schedule_id": str(run.schedule_id) if run.schedule_id else None, "schedule": schedule, "rerun_of": str(run.rerun_of) if run.rerun_of else None,
            "conversation_id": str(inv.conversation_id) if inv.conversation_id else None}


def _base_query():
    return (select(CrewRun, Investigation, Environment.name, User.email, CrewSchedule.name)
            .join(Investigation, Investigation.id == CrewRun.investigation_id).join(Environment, Environment.id == Investigation.environment_id)
            .outerjoin(User, User.id == Investigation.created_by).outerjoin(CrewSchedule, CrewSchedule.id == CrewRun.schedule_id)
            .outerjoin(Conversation, Conversation.id == Investigation.conversation_id).where(Conversation.deleted_at.is_(None)))


async def _visible(db: AsyncSession, ctx: AuthContext, q, limit: int = SCAN) -> list[tuple]:
    rows = (await db.execute(q.order_by(CrewRun.created_at.desc()).limit(limit))).all()
    return [r for r in rows if ctx.principal.can(P.INVESTIGATION_READ, r[2], r[1].namespace)]


async def _run_or_404(request: Request, db: AsyncSession, ctx: AuthContext, run_id: uuid.UUID) -> tuple[CrewRun, Investigation, Environment]:
    inv, env = await _load_visible(request, db, ctx, run_id)
    run = await db.get(CrewRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run, inv, env


async def _audit(db: AsyncSession, request: Request, ctx: AuthContext, action: str, resource_type: str, resource_id: str, details: dict[str, Any] | None = None,
                 environment: str | None = None, namespace: str | None = None) -> None:
    await audit.record(db, action=action, actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type=resource_type, resource_id=resource_id,
                       environment=environment, namespace=namespace, decision="allow", details=details or {}, ip=client_ip(request))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def _stats(rows: list[tuple]) -> dict[str, Any]:
    finished = [r[1] for r in rows if r[1].status in TERMINAL]
    ok = [i for i in finished if i.status == "completed"]
    times = [s for s in (_seconds(i) for i in finished) if s is not None]
    return {"runs": len(rows), "finished": len(finished), "succeeded": len(ok), "failed": sum(1 for i in finished if i.status == "failed"),
            "success_rate": round(len(ok) / len(finished), 3) if finished else None,
            "median_seconds": statistics.median(times) if times else None, "p95_seconds": _pct(times, 0.95),
            "cost_usd": str(sum((r[1].cost_usd or Decimal("0") for r in rows), Decimal("0")))}


# ---- starting runs ------------------------------------------------------------------------

async def start_run(request: Request, ctx: AuthContext, db: AsyncSession, flow: str, inputs: dict[str, Any], *, trigger: str = "user",
                    rerun_of: uuid.UUID | None = None, schedule_id: uuid.UUID | None = None, draft: dict[str, Any] | None = None) -> dict[str, Any]:
    """Queues one CrewAI run through start_investigation (rate limit, queue limit, RBAC, scope, audit, conversation)
    and numbers it. The configuration is recorded at queue time: the active version, or a test run's draft."""
    if not get_settings().crewai_enabled:
        raise HTTPException(404, "Not Found")
    if flow not in crew_config.FLOWS:
        raise HTTPException(404, f"Unknown flow {flow!r}")
    state = (await console.flows_state(db))[flow]
    if not state["enabled"]:
        raise HTTPException(409, f"The {crew_config.FLOWS[flow]['label'].lower()} flow is switched off in the CrewAI console" + (f": {state['reason']}" if state.get("reason") else ""))
    why = await console.available(db)
    if why:
        raise HTTPException(503, f"CrewAI investigation unavailable: {why}")
    try:
        clean = console.clean_inputs(flow, inputs, defaults=await console.flow_defaults(db, flow))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if draft is not None:
        errors, _ = crew_config.check(draft)
        if errors:
            raise HTTPException(422, "The crew configuration does not pass its checks: " + "; ".join(f"{e['field']}: {e['message']}" for e in errors[:3]))
    block = await console.queue_block(db, flow, draft)
    question = clean.get("question") or f"Why is {clean['kind']} {clean['workload']} in namespace {clean['namespace']} unhealthy?"
    inner = CreateIn(question=question, environment=clean["environment"], namespace=clean["namespace"], minutes=clean.get("minutes"))
    out = await start_investigation(inner, request, ctx, db, crewai={"workload": clean["workload"], "kind": clean["kind"], "trigger": trigger,
                                                                     **{k: v for k, v in block.items() if k != "config_digest"}})
    run = await console.record_run(db, uuid.UUID(out["id"]), flow=flow, trigger=trigger, inputs=clean, created_by=ctx.user.id, block=block,
                                   schedule_id=schedule_id, rerun_of=rerun_of)
    return {**out, "run_number": run.run_number, "flow": flow, "trigger": trigger}


# ---- overview and runs ----------------------------------------------------------------------

@router.get("/overview")
async def overview(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    week = await _visible(db, ctx, _base_query().where(CrewRun.created_at > now - timedelta(days=7)))
    day = [r for r in week if r[0].created_at > now - timedelta(days=1)]
    active = await _visible(db, ctx, _base_query().where(Investigation.status.in_(("queued", "running"))), 100)
    failures = await _visible(db, ctx, _base_query().where(Investigation.status == "failed"), 200)
    per_day = []
    for d in range(6, -1, -1):
        start = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = [r for r in week if start <= r[0].created_at < start + timedelta(days=1)]
        per_day.append({"date": start.date().isoformat(), **{s: sum(1 for r in rows if r[1].status == s) for s in STATUSES}})
    flows = []
    states = await console.flows_state(db)
    for name, spec in crew_config.FLOWS.items():
        recent = await _visible(db, ctx, _base_query().where(CrewRun.flow == name), 10)
        mine = [r for r in week if r[0].flow == name]
        flows.append({"name": name, "label": spec["label"], "enabled": states[name]["enabled"], "reason": states[name].get("reason"),
                      "last": [{"id": str(r[1].id), "number": r[0].run_number, "status": r[1].status, "crew_status": _crew_status(r[1])} for r in recent],
                      "last_run": _row(*recent[0]) if recent else None, "week": _stats(mine)})
    return {"day": _stats(day), "week": _stats(week), "running": [_row(*r) for r in active], "per_day": per_day, "flows": flows,
            "failures": [_row(*r) for r in failures[:5]], "can_admin": ctx.principal.can(P.CREWAI_ADMIN, "*", "*")}


@router.get("/runs")
async def runs(flow: str | None = None, status: str | None = None, trigger: str | None = None, q: str | None = None, environment: str | None = None,
               page: int = 1, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    query = _base_query()
    if flow:
        query = query.where(CrewRun.flow == flow)
    if trigger:
        query = query.where(CrewRun.trigger == trigger)
    if environment:
        query = query.where(Environment.name == environment)
    if status == "active":
        query = query.where(Investigation.status.in_(("queued", "running")))
    elif status == "partial":
        query = query.where(Investigation.status == "completed", Investigation.report["crewai"]["status"].astext == "partial")
    elif status in STATUSES:
        query = query.where(Investigation.status == status)
    text_q = (q or "").strip()[:200]
    if text_q.startswith("#") and text_q[1:].isdigit():
        query = query.where(CrewRun.run_number == int(text_q[1:]))
    elif text_q:
        try:
            query = query.where(or_(Investigation.id == uuid.UUID(text_q), Investigation.conversation_id == uuid.UUID(text_q)))
        except ValueError:
            like = f"%{text_q.replace('%', '').replace('_', '')}%"
            query = query.where(or_(Investigation.question.ilike(like), CrewRun.inputs["workload"].astext.ilike(like), Investigation.namespace.ilike(like),
                                    Investigation.error.ilike(like), User.email.ilike(like)))
    rows = await _visible(db, ctx, query)
    page = max(1, page)
    return {"runs": [_row(*r) for r in rows[(page - 1) * PAGE: page * PAGE]], "total": len(rows), "page": page, "pages": max(1, -(-len(rows) // PAGE)),
            "active": sum(1 for r in rows if r[1].status in ("queued", "running")), "capped": len(rows) >= SCAN}


def _steps(run: CrewRun, inv: Investigation, rows: list[CrewRunStep], crew: dict[str, Any]) -> list[dict[str, Any]]:
    by = {r.step: r for r in rows}
    expected = [("setup", STEP_LABELS["setup"])] + [(k, crew["agents"][k]["role"]) for k in crew_config.AGENT_KEYS] + [("validate", STEP_LABELS["validate"])]
    out = []
    terminal = inv.status in TERMINAL
    for step, label in expected:
        r = by.get(step)
        if r is None:
            status = "skipped" if terminal else ("pending" if inv.status in ("queued", "running") else "skipped")
            out.append({"step": step, "label": label, "status": status, "started_at": None, "finished_at": None, "seconds": None, "error": None,
                        "model_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": "0"})
            continue
        status = r.status
        if status == "running" and terminal:  # the worker died or the run ended outside the crew
            status = {"completed": "succeeded", "cancelled": "cancelled"}.get(inv.status, "failed")
        end = r.finished_at or (inv.finished_at if terminal else None)
        out.append({"step": step, "label": r.label or label, "status": status, "started_at": _iso(r.started_at), "finished_at": _iso(end),
                    "seconds": (end - r.started_at).total_seconds() if end else (datetime.now(timezone.utc) - r.started_at).total_seconds(),
                    "error": r.error, "model_calls": r.model_calls, "tool_calls": r.tool_calls, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
                    "cost_usd": str(r.cost_usd)})
    return out


async def _queued_block(db: AsyncSession, inv_id: uuid.UUID) -> dict[str, Any]:
    payload = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == inv_id, InvestigationEvent.seq == 0))).scalar_one_or_none() or {}
    return payload.get("crewai") if isinstance(payload.get("crewai"), dict) else {}


async def _run_crew(db: AsyncSession, run: CrewRun, block: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The configuration a run used, and how to describe it."""
    if isinstance(block.get("draft"), dict):
        return crew_config.merge(block["draft"]), {"label": "test draft (not saved)", "version": None, "draft": True, "digest": run.config_digest}
    if run.config_version:
        row = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew_config.CREW, CrewConfig.version == run.config_version))).scalar_one_or_none()
        return crew_config.merge(row.config if row else None), {"label": f"version {run.config_version}", "version": run.config_version, "draft": False, "digest": run.config_digest}
    return crew_config.merge(None), {"label": "the code's defaults", "version": None, "draft": False, "digest": ""}


@router.get("/runs/{run_id}")
async def run_detail(run_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    run, inv, env = await _run_or_404(request, db, ctx, run_id)
    user = await db.get(User, inv.created_by)
    sched = await db.get(CrewSchedule, run.schedule_id) if run.schedule_id else None
    block = await _queued_block(db, inv.id)
    crew, used = await _run_crew(db, run, block)
    steps = (await db.execute(select(CrewRunStep).where(CrewRunStep.investigation_id == inv.id).order_by(CrewRunStep.position))).scalars().all()
    neighbours = await _visible(db, ctx, _base_query().where(CrewRun.flow == run.flow, CrewRun.run_number.in_((run.run_number - 1, run.run_number + 1))), 10)
    reruns = await _visible(db, ctx, _base_query().where(CrewRun.rerun_of == inv.id), 20)
    parent = (await _visible(db, ctx, _base_query().where(CrewRun.investigation_id == run.rerun_of), 1)) if run.rerun_of else []
    models = (await db.execute(select(LLMUsage.provider, LLMUsage.model, func.count(), func.sum(LLMUsage.tokens_in), func.sum(LLMUsage.tokens_out), func.sum(LLMUsage.cost_usd))
                               .where(LLMUsage.investigation_id == inv.id).group_by(LLMUsage.provider, LLMUsage.model))).all()
    if not models:  # nothing metered (a provider that reports no usage): the transcripts' own counts, when they are kept
        models = (await db.execute(select(CrewRunCall.provider, CrewRunCall.model, func.count(), func.sum(CrewRunCall.tokens_in), func.sum(CrewRunCall.tokens_out),
                                          func.sum(CrewRunCall.cost_usd)).where(CrewRunCall.investigation_id == inv.id).group_by(CrewRunCall.provider, CrewRunCall.model))).all()
    rep = inv.report or {}
    crewai = rep.get("crewai") or {}
    derived = _steps(run, inv, list(steps), crew)
    terminal = inv.status in TERMINAL
    return {
        "run": _row(run, inv, env.name, user.email if user else None, sched.name if sched else None),
        "steps": derived, "inputs": run.inputs, "config": used,
        "report": {"summary": rep.get("summary"), "findings": len(rep.get("observed_facts") or []), "causes": len(rep.get("suspected_causes") or []),
                   "missing_evidence": (rep.get("missing_evidence") or [])[:20], "next_steps": (rep.get("next_steps") or [])[:10], "mode": inv.mode,
                   "crewai": {k: crewai.get(k) for k in ("status", "stopped", "gaps", "validation", "refused", "agents", "tool_calls", "limits", "config")} if crewai else None},
        "cost": {"by_agent": [{k: s[k] for k in ("step", "label", "model_calls", "tool_calls", "tokens_in", "tokens_out", "cost_usd")} for s in derived if s["model_calls"] or s["tool_calls"]],
                 "by_model": [{"model": f"{p}/{m}", "calls": c, "tokens_in": int(ti or 0), "tokens_out": int(to or 0), "cost_usd": str(cost or 0)} for p, m, c, ti, to, cost in models]},
        "errors": [e for e in [inv.error, crewai.get("stopped") and f"Crew stopped early: {crewai['stopped']}"] if e] + [f"{s['label']}: {s['error']}" for s in derived if s["error"] and s["status"] == "failed"],
        "prev": next(({"id": str(r[1].id), "number": r[0].run_number} for r in neighbours if r[0].run_number == run.run_number - 1), None),
        "next": next(({"id": str(r[1].id), "number": r[0].run_number} for r in neighbours if r[0].run_number == run.run_number + 1), None),
        "reruns": [{"id": str(r[1].id), "number": r[0].run_number, "status": r[1].status, "trigger": r[0].trigger} for r in reruns],
        "rerun_of": {"id": str(parent[0][1].id), "number": parent[0][0].run_number} if parent else None,
        "can": {"rerun": terminal and run.trigger != "test" and ctx.principal.can(P.INVESTIGATION_RUN, env.name, inv.namespace),
                "cancel": not terminal and (inv.created_by == ctx.user.id or ctx.principal.can(P.ADMIN_POLICIES, "*", "*")),
                "save_version": terminal and run.trigger == "test" and ctx.principal.can(P.CREWAI_ADMIN, "*", "*"),
                "test": terminal and ctx.principal.can(P.CREWAI_ADMIN, "*", "*") and ctx.principal.can(P.INVESTIGATION_RUN, env.name, inv.namespace)},
    }


def _line(r: CrewRunLog) -> dict[str, Any]:
    return {"id": r.id, "step": r.step, "at": _iso(r.at), "level": r.level, "message": r.message, "data": r.data, "call_id": r.call_id}


@router.get("/runs/{run_id}/logs", response_model=None)
async def run_logs(run_id: uuid.UUID, request: Request, after: int = 0, limit: int = 2000, format: str = "json", ctx: AuthContext = Depends(get_auth),
                   db: AsyncSession = Depends(get_db, scope="function")):
    """The run's log after line `after`: JSON pages for the viewer (it polls while the run is live) or the whole log as text."""
    run, inv, _ = await _run_or_404(request, db, ctx, run_id)
    q = select(CrewRunLog).where(CrewRunLog.investigation_id == inv.id, CrewRunLog.id > after).order_by(CrewRunLog.id)
    if format == "text":
        rows = (await db.execute(q.limit(50_000))).scalars().all()
        body = "\n".join(f"{_iso(r.at)} [{r.step}] {r.level.upper():7} {r.message}" for r in rows) + "\n"
        return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="crewai-{run.flow}-{run.run_number}.log"'})
    rows = (await db.execute(q.limit(max(1, min(limit, 5000))))).scalars().all()
    steps = (await db.execute(select(CrewRunStep).where(CrewRunStep.investigation_id == inv.id).order_by(CrewRunStep.position))).scalars().all()
    crew, _ = await _run_crew(db, run, await _queued_block(db, inv.id))
    return {"lines": [_line(r) for r in rows], "last": rows[-1].id if rows else after, "status": inv.status, "done": inv.status in TERMINAL,
            "steps": _steps(run, inv, list(steps), crew)}


@router.get("/calls/{call_id}")
async def call_detail(call_id: int, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    row = await db.get(CrewRunCall, call_id)
    if row is None:
        raise HTTPException(404, "Transcript not found (transcripts may be off, or past their retention)")
    await _run_or_404(request, db, ctx, row.investigation_id)
    return {"id": row.id, "run_id": str(row.investigation_id), "step": row.step, "model": f"{row.provider}/{row.model}", "system": row.system, "prompt": row.prompt,
            "response": row.response, "tokens_in": row.tokens_in, "tokens_out": row.tokens_out, "cost_usd": str(row.cost_usd), "duration_ms": row.duration_ms,
            "outcome": row.outcome, "error": row.error, "at": _iso(row.created_at)}


class RerunIn(BaseModel):
    inputs: dict[str, Any] | None = None


@router.post("/runs/{run_id}/rerun", status_code=201)
async def rerun(run_id: uuid.UUID, body: RerunIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Runs a finished run again with the same inputs, or edited ones, on the crew's active configuration."""
    run, inv, _ = await _run_or_404(request, db, ctx, run_id)
    if inv.status not in TERMINAL:
        raise HTTPException(409, "The run has not finished yet")
    if run.trigger == "test":
        raise HTTPException(409, "A test run is repeated from the crew page (Test run), with the draft you want to try")
    return await start_run(request, ctx, db, run.flow, {**(run.inputs or {}), **(body.inputs or {})}, trigger="rerun", rerun_of=inv.id)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    from .investigations import cancel

    await _run_or_404(request, db, ctx, run_id)
    return await cancel(run_id, request, ctx, db)


class SaveIn(BaseModel):
    note: str = Field(default="", max_length=2000)
    activate: bool = False


@router.post("/runs/{run_id}/save-version", status_code=201)
async def save_test_version(run_id: uuid.UUID, body: SaveIn, request: Request, ctx: AuthContext = Depends(admin_only),
                            db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """A test run's draft as a new crew version."""
    run, inv, _ = await _run_or_404(request, db, ctx, run_id)
    draft = (await _queued_block(db, inv.id)).get("draft")
    if run.trigger != "test" or not isinstance(draft, dict):
        raise HTTPException(409, "Only a test run's draft can be saved as a version")
    return await _save(request, ctx, db, crew_config.FLOWS[run.flow]["crew"], draft, body.note or f"From test run #{run.run_number}", body.activate)


# ---- flows ----------------------------------------------------------------------------

async def _flow(db: AsyncSession, ctx: AuthContext, name: str, states: dict[str, dict[str, Any]], week: list[tuple]) -> dict[str, Any]:
    spec = crew_config.FLOWS[name]
    recent = await _visible(db, ctx, _base_query().where(CrewRun.flow == name), 10)
    scheds = (await db.execute(select(func.count()).select_from(CrewSchedule).where(CrewSchedule.flow == name))).scalar_one()
    active = await console.active_config(db, spec["crew"])
    return {"name": name, "label": spec["label"], "description": spec["description"], "crew": spec["crew"], "inputs": spec["inputs"],
            "defaults": await console.flow_defaults(db, name), "enabled": states[name]["enabled"], "reason": states[name].get("reason"),
            "changed_at": states[name].get("changed_at"), "schedules": scheds, "config_version": active.version if active else None,
            "last": [{"id": str(r[1].id), "number": r[0].run_number, "status": r[1].status, "crew_status": _crew_status(r[1])} for r in recent],
            "last_run": _row(*recent[0]) if recent else None, "week": _stats([r for r in week if r[0].flow == name])}


@router.get("/flows")
async def flows(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    states = await console.flows_state(db)
    week = await _visible(db, ctx, _base_query().where(CrewRun.created_at > datetime.now(timezone.utc) - timedelta(days=7)))
    envs = (await db.execute(select(Environment.name).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    return {"flows": [await _flow(db, ctx, n, states, week) for n in crew_config.FLOWS], "crewai_enabled": get_settings().crewai_enabled,
            "unavailable": await console.available(db), "environments": [e for e in envs if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e)],
            "can_admin": ctx.principal.can(P.CREWAI_ADMIN, "*", "*")}


@router.get("/flows/{name}")
async def flow_detail(name: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    if name not in crew_config.FLOWS:
        raise HTTPException(404, f"Unknown flow {name!r}")
    states = await console.flows_state(db)
    week = await _visible(db, ctx, _base_query().where(CrewRun.flow == name, CrewRun.created_at > datetime.now(timezone.utc) - timedelta(days=7)))
    recent = await _visible(db, ctx, _base_query().where(CrewRun.flow == name), 25)
    return {**(await _flow(db, ctx, name, states, week)), "runs": [_row(*r) for r in recent], "schedule_list": await _schedules(db, ctx, name)}


class RunIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.post("/flows/{name}/run", status_code=201)
async def run_flow(name: str, body: RunIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await start_run(request, ctx, db, name, body.inputs)


class FlowIn(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=300)


@router.patch("/flows/{name}")
async def set_flow(name: str, body: FlowIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Switches a flow on or off. Off: nobody can start it, schedules skip it (and say why), runs already queued fail at start."""
    if name not in crew_config.FLOWS:
        raise HTTPException(404, f"Unknown flow {name!r}")
    state = await console.set_flow(db, name, body.enabled, body.reason, ctx.user.id)
    await _audit(db, request, ctx, "crewai.flow.enable" if body.enabled else "crewai.flow.disable", "crewai_flow", name, {"reason": body.reason})
    return {"name": name, **state}


# ---- schedules ---------------------------------------------------------------------------

def _upcoming(expr: str, count: int = 3) -> list[str]:
    try:
        return [t.isoformat() for t in cron.upcoming(cron.parse(expr), datetime.now(timezone.utc), count)]
    except cron.CronError:
        return []


async def _schedules(db: AsyncSession, ctx: AuthContext, flow: str | None = None) -> list[dict[str, Any]]:
    q = select(CrewSchedule).order_by(CrewSchedule.flow, CrewSchedule.name)
    if flow:
        q = q.where(CrewSchedule.flow == flow)
    rows = (await db.execute(q)).scalars().all()
    emails = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_({r.owner_id for r in rows} | {r.updated_by for r in rows if r.updated_by} or {uuid.uuid4()})))).scalars()}
    last = {r.investigation_id: r for r in (await db.execute(select(CrewRun).where(CrewRun.investigation_id.in_([r.last_run_id for r in rows if r.last_run_id] or [uuid.uuid4()])))).scalars()}
    status = {i.id: i.status for i in (await db.execute(select(Investigation).where(Investigation.id.in_(list(last) or [uuid.uuid4()])))).scalars()}
    out = []
    for r in rows:
        env, ns = (r.inputs or {}).get("environment"), (r.inputs or {}).get("namespace")
        if not ctx.principal.can(P.INVESTIGATION_READ, env, ns) and not ctx.principal.can(P.CREWAI_ADMIN, "*", "*"):
            continue
        lr = last.get(r.last_run_id) if r.last_run_id else None
        out.append({"id": str(r.id), "flow": r.flow, "flow_label": crew_config.FLOWS.get(r.flow, {}).get("label", r.flow), "name": r.name, "cron": r.cron,
                    "inputs": r.inputs, "enabled": r.enabled, "owner": emails.get(r.owner_id), "version": r.version, "next_run_at": _iso(r.next_run_at) if r.enabled else None,
                    "upcoming": _upcoming(r.cron) if r.enabled else [], "last_run_at": _iso(r.last_run_at), "last_error": r.last_error,
                    "last_run": {"id": str(lr.investigation_id), "number": lr.run_number, "status": status.get(lr.investigation_id)} if lr else None,
                    "updated_at": _iso(r.updated_at), "updated_by": emails.get(r.updated_by)})
    return out


@router.get("/schedules")
async def schedules(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return {"schedules": await _schedules(db, ctx), "can_admin": ctx.principal.can(P.CREWAI_ADMIN, "*", "*"), "min_interval_minutes": cron.MIN_INTERVAL_MINUTES}


@router.get("/cron")
async def cron_preview(expr: str, ctx: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    try:
        parsed, times = cron.validate(expr)
    except cron.CronError as exc:
        return {"valid": False, "error": str(exc), "next": []}
    return {"valid": True, "error": None, "expr": parsed.expr, "next": [t.isoformat() for t in times]}


class ScheduleIn(BaseModel):
    flow: str = crew_config.FLOW
    name: str = Field(min_length=1, max_length=120)
    cron: str = Field(min_length=1, max_length=120)
    inputs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    cron: str | None = Field(default=None, min_length=1, max_length=120)
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None


async def _checked_schedule(db: AsyncSession, ctx: AuthContext, flow: str, expr: str, inputs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The schedule runs as the person saving it, so that person must be able to run it there now."""
    if flow not in crew_config.FLOWS:
        raise HTTPException(422, f"flow: unknown flow {flow!r}")
    try:
        parsed, _ = cron.validate(expr)
        clean = console.clean_inputs(flow, inputs)
    except (cron.CronError, ValueError) as exc:
        raise HTTPException(422, (f"cron: {exc}" if isinstance(exc, cron.CronError) else str(exc))) from None
    env = (await db.execute(select(Environment).where(Environment.name == clean["environment"]))).scalar_one_or_none()
    if env is None:
        raise HTTPException(422, f"environment: {clean['environment']} does not exist")
    if not ctx.principal.can(P.INVESTIGATION_RUN, env.name, clean["namespace"]):
        raise HTTPException(403, f"A schedule runs as the person who saves it, and you need investigation.run in {env.name}/{clean['namespace']}")
    return parsed.expr, clean


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    expr, clean = await _checked_schedule(db, ctx, body.flow, body.cron, body.inputs)
    if (await db.execute(select(CrewSchedule.id).where(CrewSchedule.flow == body.flow, CrewSchedule.name == body.name.strip()))).first():
        raise HTTPException(409, f"A schedule named {body.name.strip()!r} already exists for this flow")
    now = datetime.now(timezone.utc)
    row = CrewSchedule(flow=body.flow, name=body.name.strip(), cron=expr, inputs=clean, enabled=body.enabled, owner_id=ctx.user.id, created_by=ctx.user.id,
                       updated_by=ctx.user.id, next_run_at=console.next_fire(expr, now))
    db.add(row)
    await db.flush()
    await _audit(db, request, ctx, "crewai.schedule.create", "crewai_schedule", str(row.id), {"name": row.name, "cron": expr, "inputs": clean, "enabled": row.enabled},
                 clean["environment"], clean["namespace"])
    return next(s for s in await _schedules(db, ctx) if s["id"] == str(row.id))


@router.patch("/schedules/{sid}")
async def update_schedule(sid: uuid.UUID, body: SchedulePatch, request: Request, ctx: AuthContext = Depends(admin_only),
                          db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Edits, pauses or resumes a schedule. The editor becomes its owner. A changed time or a resume counts from now,
    so it never fires a time that has already passed."""
    row = await db.get(CrewSchedule, sid)
    if row is None:
        raise HTTPException(404, "Schedule not found")
    before = {"name": row.name, "cron": row.cron, "inputs": row.inputs, "enabled": row.enabled}
    expr, clean = await _checked_schedule(db, ctx, row.flow, body.cron or row.cron, body.inputs if body.inputs is not None else row.inputs)
    if body.name and body.name.strip() != row.name:
        if (await db.execute(select(CrewSchedule.id).where(CrewSchedule.flow == row.flow, CrewSchedule.name == body.name.strip(), CrewSchedule.id != sid))).first():
            raise HTTPException(409, f"A schedule named {body.name.strip()!r} already exists for this flow")
        row.name = body.name.strip()
    now = datetime.now(timezone.utc)
    retime = expr != row.cron or (body.enabled is True and not row.enabled)
    row.cron, row.inputs = expr, clean
    if body.enabled is not None:
        row.enabled = body.enabled
    if retime or row.next_run_at is None:
        row.next_run_at = console.next_fire(expr, now)
    row.owner_id = row.updated_by = ctx.user.id
    row.version += 1
    row.updated_at = now
    await db.flush()
    action = "crewai.schedule.pause" if before["enabled"] and not row.enabled else "crewai.schedule.resume" if not before["enabled"] and row.enabled else "crewai.schedule.update"
    await _audit(db, request, ctx, action, "crewai_schedule", str(row.id), {"before": before, "after": {"name": row.name, "cron": row.cron, "inputs": row.inputs, "enabled": row.enabled}},
                 clean["environment"], clean["namespace"])
    return next(s for s in await _schedules(db, ctx) if s["id"] == str(row.id))


@router.delete("/schedules/{sid}")
async def delete_schedule(sid: uuid.UUID, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    row = await db.get(CrewSchedule, sid)
    if row is None:
        raise HTTPException(404, "Schedule not found")
    await _audit(db, request, ctx, "crewai.schedule.delete", "crewai_schedule", str(row.id), {"name": row.name, "cron": row.cron, "inputs": row.inputs})
    await db.delete(row)
    return {"status": "deleted"}


@router.post("/schedules/{sid}/run", status_code=201)
async def run_schedule_now(sid: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Run now: the schedule's inputs, started by and as the person who clicks, outside its timetable."""
    row = await db.get(CrewSchedule, sid)
    if row is None:
        raise HTTPException(404, "Schedule not found")
    return await start_run(request, ctx, db, row.flow, row.inputs, schedule_id=row.id)


# ---- crews -------------------------------------------------------------------------------

def _crew_or_404(crew: str) -> str:
    if crew != crew_config.CREW:
        raise HTTPException(404, f"Unknown crew {crew!r}")
    return crew


def _version(row: CrewConfig, emails: dict) -> dict[str, Any]:
    return {"version": row.version, "note": row.note, "active": row.active, "digest": row.digest, "created_by": emails.get(row.created_by), "created_at": _iso(row.created_at),
            "activated_at": _iso(row.activated_at), "fields": len(crew_config.diff(row.config)), "upstream_changed": row.base_digest != crew_config.base_digest()}


@router.get("/crews")
async def crews(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    out = []
    for crew in (crew_config.CREW,):
        rows = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew).order_by(CrewConfig.version.desc()))).scalars().all()
        active = next((r for r in rows if r.active), None)
        out.append({"crew": crew, "label": "Workload investigation crew", "flows": [n for n, f in crew_config.FLOWS.items() if f["crew"] == crew],
                    "agents": [crew_config.merge(active.config if active else None)["agents"][k]["role"] for k in crew_config.AGENT_KEYS],
                    "active_version": active.version if active else None, "versions": len(rows), "upstream_changed": bool(active and active.base_digest != crew_config.base_digest()),
                    "updated_at": _iso(rows[0].created_at) if rows else None})
    return {"crews": out, "can_admin": ctx.principal.can(P.CREWAI_ADMIN, "*", "*")}


@router.get("/crews/{crew}")
async def crew_detail(crew: str, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _crew_or_404(crew)
    rows = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew).order_by(CrewConfig.version.desc()))).scalars().all()
    emails = {u.id: u.email for u in (await db.execute(select(User).where(User.id.in_({r.created_by for r in rows if r.created_by} or {uuid.uuid4()})))).scalars()}
    active = next((r for r in rows if r.active), None)
    overlay = active.config if active else {}
    finished = await _visible(db, ctx, _base_query().where(CrewRun.flow == crew_config.FLOW, Investigation.status.in_(TERMINAL), CrewRun.trigger != "test"), 20)
    return {"crew": crew, "label": "Workload investigation crew", "flows": [n for n, f in crew_config.FLOWS.items() if f["crew"] == crew],
            "defaults": crew_config.defaults(), "effective": crew_config.merge(overlay), "overlay": overlay, "diff": crew_config.diff(overlay),
            "active": _version(active, emails) if active else None, "versions": [_version(r, emails) for r in rows],
            "tools": {k: [{"name": n, "description": BY_NAME[n].description, "moks_tool": BY_NAME[n].tool} for n in names] for k, names in crew_config.TOOLS_BY_AGENT.items()},
            "ceilings": {k: list(v) for k, v in crew_config.ceilings().items()}, "text_limits": crew_config.TEXT_LIMITS, "placeholders": list(crew_config.PLACEHOLDERS),
            "code_owned": {"rules": crew_config.rules("<random tag>"), "output_contract": crew_config.OUTPUT_CONTRACT,
                           "structure": "Three agents in sequence (workload, change, review); no delegation, memory, planning or reasoning; read-only tools only; "
                                        "citations and confidence are checked in code after the crew finishes."},
            "runs_to_test": [_row(*r) for r in finished], "can_admin": ctx.principal.can(P.CREWAI_ADMIN, "*", "*")}


@router.get("/crews/{crew}/versions/{version}")
async def crew_version(crew: str, version: int, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _crew_or_404(crew)
    row = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew, CrewConfig.version == version))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Version not found")
    return {**_version(row, {}), "overlay": row.config, "effective": crew_config.merge(row.config), "diff": crew_config.diff(row.config)}


class ConfigIn(BaseModel):
    config: dict[str, Any]


@router.post("/crews/{crew}/check")
async def crew_check(crew: str, body: ConfigIn, ctx: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    _crew_or_404(crew)
    errors, warnings = crew_config.check(body.config)
    overlay = crew_config.overlay_of(crew_config.merge(body.config)) if not errors else None
    return {"errors": errors, "warnings": warnings, "overlay": overlay, "diff": crew_config.diff(overlay) if overlay is not None else [],
            "effective": crew_config.merge(overlay) if overlay is not None else None}


class VersionIn(BaseModel):
    config: dict[str, Any]
    note: str = Field(default="", max_length=2000)
    activate: bool = False


async def _save(request: Request, ctx: AuthContext, db: AsyncSession, crew: str, config: dict[str, Any], note: str, activate: bool) -> dict[str, Any]:
    errors, warnings = crew_config.check(config)
    if errors:
        raise HTTPException(422, "The configuration does not pass its checks: " + "; ".join(f"{e['field']}: {e['message']}" for e in errors[:5]))
    overlay = crew_config.overlay_of(crew_config.merge(config))
    row = await console.save_version(db, crew, overlay, note, ctx.user.id, activate)
    await _audit(db, request, ctx, "crewai.crew.save", "crewai_crew", crew, {"version": row.version, "activated": activate, "digest": row.digest, "fields": [d["field"] for d in crew_config.diff(overlay)]})
    return {**_version(row, {row.created_by: ctx.user.email}), "warnings": warnings}


@router.post("/crews/{crew}/versions", status_code=201)
async def crew_save(crew: str, body: VersionIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await _save(request, ctx, db, _crew_or_404(crew), body.config, body.note, body.activate)


class ActivateIn(BaseModel):
    version: int | None = None  # None: the code's defaults


@router.post("/crews/{crew}/activate")
async def crew_activate(crew: str, body: ActivateIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _crew_or_404(crew)
    previous = await console.active_config(db, crew)
    if body.version is not None:
        row = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew, CrewConfig.version == body.version))).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "Version not found")
        errors, _ = crew_config.check(row.config)
        if errors:
            raise HTTPException(422, f"Version {body.version} no longer passes its checks: " + "; ".join(f"{e['field']}: {e['message']}" for e in errors[:3]))
    await console.activate_version(db, crew, body.version)
    await _audit(db, request, ctx, "crewai.crew.activate", "crewai_crew", crew, {"version": body.version, "previous": previous.version if previous else None})
    return {"crew": crew, "active_version": body.version}


class TestIn(BaseModel):
    config: dict[str, Any]
    run_id: uuid.UUID


@router.post("/crews/{crew}/test", status_code=201)
async def crew_test(crew: str, body: TestIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Test run (Jenkins' Replay): the chosen finished run's inputs, on the unsaved draft. Nothing is saved; the run
    page offers "Save as new version" afterwards."""
    _crew_or_404(crew)
    run, inv, _ = await _run_or_404(request, db, ctx, body.run_id)
    if inv.status not in TERMINAL:
        raise HTTPException(409, "Choose a run that has finished")
    if crew_config.FLOWS.get(run.flow, {}).get("crew") != crew:
        raise HTTPException(422, "That run did not use this crew")
    return await start_run(request, ctx, db, run.flow, run.inputs or {}, trigger="test", rerun_of=inv.id, draft=body.config)


# ---- settings, import and export ----------------------------------------------------------------

class SettingsIn(BaseModel):
    transcripts: bool | None = None
    log_days: int | None = Field(default=None, ge=1, le=365)
    transcript_days: int | None = Field(default=None, ge=1, le=90)


@router.get("/settings")
async def get_settings_(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    s = get_settings()
    return {"history": await console.setting(db, "history", HISTORY_DEFAULTS), "defaults": HISTORY_DEFAULTS, "can_admin": ctx.principal.can(P.CREWAI_ADMIN, "*", "*"),
            "deployment": {"enabled": s.crewai_enabled, "max_concurrent_per_worker": s.crewai_max_concurrent, "tool_timeout_seconds": s.crewai_tool_timeout_seconds,
                           "excerpt_chars": s.crewai_excerpt_chars, "log_lines": s.crewai_log_lines, "max_tool_calls": s.max_tool_calls, "max_duration_seconds": s.max_duration_seconds},
            "unavailable": await console.available(db)}


@router.patch("/settings")
async def put_settings(body: SettingsIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    current = await console.setting(db, "history", HISTORY_DEFAULTS)
    new = {**current, **body.model_dump(exclude_none=True)}
    await console.put_setting(db, "history", new, ctx.user.id)
    await _audit(db, request, ctx, "crewai.settings.update", "crewai_settings", "history", {"before": current, "after": new})
    return {"history": new}


EXPORT_FORMAT = "moks-crewai-console/1"


@router.get("/export")
async def export(request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """One JSON document: each crew's active version (as an overlay on the code's defaults), every schedule, which flows
    are switched off and the run-history settings. No runs, logs, transcripts or owners."""
    crews_out = {}
    for crew in (crew_config.CREW,):
        row = await console.active_config(db, crew)
        crews_out[crew] = {"config": row.config if row else {}, "version": row.version if row else None, "note": row.note if row else ""}
    scheds = (await db.execute(select(CrewSchedule).order_by(CrewSchedule.flow, CrewSchedule.name))).scalars().all()
    await _audit(db, request, ctx, "crewai.config.export", "crewai_config", "export", {"schedules": len(scheds)})
    return {"format": EXPORT_FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(), "crews": crews_out,
            "schedules": [{"flow": s.flow, "name": s.name, "cron": s.cron, "inputs": s.inputs, "enabled": s.enabled} for s in scheds],
            "flows": {k: {"enabled": v["enabled"], "reason": v.get("reason")} for k, v in (await console.flows_state(db)).items()},
            "settings": {"history": await console.setting(db, "history", HISTORY_DEFAULTS)}}


class ImportIn(BaseModel):
    data: dict[str, Any]
    apply: bool = False


@router.post("/import")
async def import_(body: ImportIn, request: Request, ctx: AuthContext = Depends(admin_only), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """A plan first (apply false), then the same plan applied. Import only adds and changes: nothing is deleted, an
    imported crew configuration becomes a new active version, and imported schedules run as the importer."""
    data = body.data
    if data.get("format") != EXPORT_FORMAT:
        raise HTTPException(422, f"format: expected {EXPORT_FORMAT!r}")
    plan: list[dict[str, Any]] = []
    errors: list[str] = []
    for crew, item in (data.get("crews") or {}).items():
        if crew != crew_config.CREW:
            errors.append(f"crews.{crew}: unknown crew")
            continue
        cfg = (item or {}).get("config") or {}
        bad, _ = crew_config.check(cfg)
        if bad:
            errors += [f"crews.{crew}.{e['field']}: {e['message']}" for e in bad]
            continue
        overlay = crew_config.overlay_of(crew_config.merge(cfg))
        current = await console.active_config(db, crew)
        same = crew_config.digest(overlay) == (current.digest if current else crew_config.digest({}))
        plan.append({"kind": "crew", "name": crew, "action": "same" if same else "change", "detail": f"{len(crew_config.diff(overlay))} edited field(s)", "_overlay": overlay})
    for i, s in enumerate(data.get("schedules") or []):
        try:
            if not isinstance(s, dict):
                raise ValueError("must be an object")
            flow, name = s.get("flow") or crew_config.FLOW, str(s.get("name") or "").strip()
            if not name or len(name) > 120:
                raise ValueError("name: 1 to 120 characters")
            expr, clean = await _checked_schedule(db, ctx, flow, str(s.get("cron") or ""), s.get("inputs") or {})
        except (ValueError, HTTPException) as exc:
            errors.append(f"schedules[{i}]: {getattr(exc, 'detail', None) or exc}")
            continue
        existing = (await db.execute(select(CrewSchedule).where(CrewSchedule.flow == flow, CrewSchedule.name == name))).scalar_one_or_none()
        enabled = bool(s.get("enabled", True))
        same = existing is not None and (existing.cron, existing.inputs, existing.enabled) == (expr, clean, enabled)
        plan.append({"kind": "schedule", "name": name, "action": "add" if existing is None else "same" if same else "change", "detail": f"{expr} · {clean.get('namespace')}/{clean.get('workload')}",
                     "_row": {"flow": flow, "name": name, "cron": expr, "inputs": clean, "enabled": enabled}})
    states = await console.flows_state(db)
    for name, f in (data.get("flows") or {}).items():
        if name not in crew_config.FLOWS:
            errors.append(f"flows.{name}: unknown flow")
            continue
        enabled = bool((f or {}).get("enabled", True))
        plan.append({"kind": "flow", "name": name, "action": "same" if states[name]["enabled"] == enabled else "change", "detail": "on" if enabled else "off",
                     "_flow": {"enabled": enabled, "reason": (f or {}).get("reason")}})
    hist = ((data.get("settings") or {}).get("history")) or None
    if hist is not None:
        try:
            parsed = SettingsIn(**{k: hist.get(k) for k in ("transcripts", "log_days", "transcript_days")}).model_dump(exclude_none=True)
        except Exception as exc:
            errors.append(f"settings.history: {exc}")
        else:
            current = await console.setting(db, "history", HISTORY_DEFAULTS)
            plan.append({"kind": "settings", "name": "history", "action": "same" if {**current, **parsed} == current else "change",
                         "detail": ", ".join(f"{k} {v}" for k, v in parsed.items()), "_settings": parsed})
    shown = [{k: v for k, v in p.items() if not k.startswith("_")} for p in plan]
    if errors or not body.apply:
        return {"plan": shown, "errors": errors, "applied": False}
    now = datetime.now(timezone.utc)
    for p in plan:
        if p["action"] == "same":
            continue
        if p["kind"] == "crew":
            await console.save_version(db, p["name"], p["_overlay"], f"Imported {data.get('exported_at') or ''}".strip(), ctx.user.id, activate=True)
        elif p["kind"] == "schedule":
            r = p["_row"]
            row = (await db.execute(select(CrewSchedule).where(CrewSchedule.flow == r["flow"], CrewSchedule.name == r["name"]))).scalar_one_or_none()
            if row is None:
                db.add(CrewSchedule(**r, owner_id=ctx.user.id, created_by=ctx.user.id, updated_by=ctx.user.id, next_run_at=console.next_fire(r["cron"], now)))
            else:
                row.cron, row.inputs, row.enabled, row.owner_id, row.updated_by, row.updated_at = r["cron"], r["inputs"], r["enabled"], ctx.user.id, ctx.user.id, now
                row.version += 1
                row.next_run_at = console.next_fire(r["cron"], now)
        elif p["kind"] == "flow":
            await console.set_flow(db, p["name"], p["_flow"]["enabled"], p["_flow"]["reason"], ctx.user.id)
        elif p["kind"] == "settings":
            await console.put_setting(db, "history", {**(await console.setting(db, "history", HISTORY_DEFAULTS)), **p["_settings"]}, ctx.user.id)
    await db.flush()
    await _audit(db, request, ctx, "crewai.config.import", "crewai_config", "import", {"plan": shown})
    return {"plan": shown, "errors": [], "applied": True}


# ---- the run form's environments ----------------------------------------------------------

@router.get("/environments")
async def run_environments(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> list[str]:
    envs = (await db.execute(select(Environment.name).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    return [e for e in envs if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e)]

