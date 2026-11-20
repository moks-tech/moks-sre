"""Investigations: create, list, read, cancel and stream progress (SSE)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, conversations
from ..auth.deps import get_auth, require
from ..auth.revocation import CHECK_SECONDS as STREAM_RECHECK_SECONDS
from ..auth.sessions import COOKIE, load
from ..auth.ratelimit import limiter
from ..auth.sessions import AuthContext, client_ip
from ..config import get_settings
from ..db import get_db, session_scope
from ..models import Conversation, ConversationMessage, Environment, Evidence, Investigation, InvestigationEvent, User
from ..observability import SSE_STREAMS, SSE_STREAMS_OPEN, traceparent, unrecorded
from ..rbac import P
from ..scope import ScopeError, effective_scope
from .common import env_or_404, max_window

log = logging.getLogger("moks.api.investigations")

router = APIRouter(tags=["investigations"])

_WINDOW = re.compile(r"(?i)\b(?:last|past|previous)\s+(\d{1,4})\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)\b")
TERMINAL = {"completed", "failed", "cancelled"}


def window_from_question(question: str) -> int | None:
    m = _WINDOW.search(question)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)


class CreateIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    # Both optional: MOKS resolves them from the question (see resolve_scope).
    environment: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    namespace: str | None = Field(default=None, pattern=r"^(\*|[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$")
    minutes: int | None = Field(default=None, ge=5, le=10080)
    incident_id: uuid.UUID | None = None
    skill: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")  # run this skill first, if it may run here


def serialize(inv: Investigation, env_name: str, user_email: str | None = None, full: bool = False) -> dict[str, Any]:
    out = {
        "id": str(inv.id), "question": inv.question, "environment": env_name, "namespace": inv.namespace,
        "status": inv.status, "mode": inv.mode, "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "started_at": inv.started_at.isoformat() if inv.started_at else None,
        "finished_at": inv.finished_at.isoformat() if inv.finished_at else None,
        "time_from": inv.time_from.isoformat(), "time_to": inv.time_to.isoformat(),
        "created_by": user_email, "tool_calls": inv.tool_calls, "tokens_in": inv.tokens_in, "tokens_out": inv.tokens_out,
        "cost_usd": str(inv.cost_usd), "error": inv.error,
        "summary": (inv.report or {}).get("summary") if inv.report else None,
        "incident_id": str(inv.incident_id) if inv.incident_id else None, "trigger": inv.trigger, "stage": inv.stage,
        "group_id": str(inv.group_id) if inv.group_id else None, "conversation_id": str(inv.conversation_id) if inv.conversation_id else None,
    }
    if full:
        out["report"] = inv.report
    return out


@router.get("/investigate/config")
async def investigate_config(ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """What an investigation will use: the active chat LLM (or rule-based) and the loaded skills."""
    from .. import skills
    from ..models import LLMConfig

    cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none()
    envs = (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars().all()
    from ..crew import capability, console, worker_version
    from ..crew import config as crew_config

    version = await worker_version(db)
    crew = capability(get_settings(), cfg, version)
    unified = await _unified_capability(db, capability(get_settings(), cfg, version))
    if crew["enabled"]:  # what the CrewAI console decides: each flow switched on or off, and its crew's active version's limits
        states, ready = await console.flows_state(db), crew["available"]
        crew["flows"] = {}
        for name, spec in crew_config.FLOWS.items():
            active = await console.active_config(db, spec["crew"])
            limits = crew_config.merge(active.config if active else None, spec["crew"])["limits"]
            state = states[name]
            crew["flows"][name] = {"label": spec["label"], "description": spec["description"], "enabled": state["enabled"],
                                   "available": ready and state["enabled"], "reason": None if state["enabled"] else f"the {spec['label'].lower()} flow is switched off in the CrewAI console"
                                   + (f": {state['reason']}" if state.get("reason") else ""), "config_version": active.version if active else None,
                                   "agents": len(crew_config.spec(spec["crew"])["agents"]),
                                   "limits": {**limits, "max_tool_calls": min(limits["max_tool_calls"], get_settings().max_tool_calls)},
                                   "inputs": [{k: v for k, v in f.items() if k != "pattern"} for f in spec["inputs"]]}
        workload = crew["flows"][crew_config.FLOW]  # the top-level fields stay those of the workload flow, as before
        if crew["available"] and not workload["enabled"]:
            crew.update(available=False, reason=workload["reason"])
        crew["available_any"] = any(f["available"] for f in crew["flows"].values())
        crew["limits"].update(timeout_seconds=workload["limits"]["timeout_seconds"], max_tool_calls=workload["limits"]["max_tool_calls"],
                              max_iterations_per_agent=workload["limits"]["max_iterations"])
        crew["config_version"] = workload["config_version"]
    return {"llm": {"provider": cfg.provider, "model": cfg.model} if cfg else None,
            "skills": sorted(skills.library().skills), "environments": [e.name for e in envs if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name)],
            "crewai": crew, "unified": unified}


async def _unified_capability(db: AsyncSession, crew: dict[str, Any]) -> dict[str, Any]:
    """Whether the unified investigation (docs/UNIFIED_INVESTIGATION.md) can start, and its stages for the UI."""
    from ..crew import console
    from ..intelligence import FLOW
    from ..intelligence.pipeline import STAGES

    out = {"flow": FLOW, "available": bool(crew["available"]), "reason": crew.get("reason"), "stages": [{"key": s.key, "label": s.label, "description": s.description} for s in STAGES]}
    if crew["available"]:
        state = (await console.flows_state(db)).get(FLOW) or {}
        if not state.get("enabled"):
            out.update(available=False, reason="the unified investigation flow is switched off in the CrewAI console" + (f": {state['reason']}" if state.get("reason") else ""))
    return out


async def resolve_scope(db: AsyncSession, ctx: AuthContext, question: str, environment: str | None, namespace: str | None) -> tuple[Environment, str, str]:
    """Environment: the one named, one mentioned in the question, the default one, or the only one
    the user can investigate. Namespace: the one named, one mentioned in the question among the
    user's concrete patterns, otherwise "*" (the worker narrows it from cluster data) when the user
    may read all namespaces, else their single concrete namespace."""
    words = set(re.findall(r"[a-z0-9][a-z0-9-]{1,62}", question.lower()))
    if environment:
        env = await env_or_404(db, environment)
        why_env = "chosen"
    else:
        envs = [e for e in (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars()
                if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name)]
        if not envs:
            raise HTTPException(403, "You cannot run investigations in any environment")
        named = [e for e in envs if e.name in words or (e.display_name and e.display_name.lower() in question.lower())]
        env = named[0] if named else next((e for e in envs if e.is_default), None) or next((e for e in envs if e.is_production), None) or envs[0]
        why_env = "named in the question" if named else ("default environment" if env.is_default else "only/first environment you can investigate")
    if namespace:
        return env, namespace, why_env
    patterns = ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, env.name)
    concrete = [p for p in patterns if not any(c in p for c in "*?[")]
    hit = [p for p in concrete if p in words]
    if hit:
        return env, hit[0], why_env
    if "*" in patterns:
        return env, "*", why_env
    if len(concrete) == 1:
        return env, concrete[0], why_env
    raise HTTPException(422, f"Mention one of your namespaces ({', '.join(concrete[:10]) or 'none'}) in the question")


class GroupIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    environments: list[str] = Field(default_factory=list, max_length=20)  # empty: every environment you may investigate
    minutes: int | None = Field(default=None, ge=1, le=10080)


@router.post("/investigations/group", status_code=201)
async def create_group(body: GroupIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """One question across environments: one investigation each, so evidence and permissions stay
    per environment. Unauthorized environments are refused, not skipped."""
    envs = [e for e in (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars()
            if ctx.principal.namespace_patterns(P.INVESTIGATION_RUN, e.name)]
    names = {e.name for e in envs}
    if body.environments and not set(body.environments) <= names:
        raise HTTPException(403, "you cannot investigate one of those environments")
    chosen = [e for e in envs if not body.environments or e.name in body.environments]
    if not chosen:
        raise HTTPException(403, "You cannot run investigations in any environment")
    group = uuid.uuid4()
    out = []
    for e in chosen[:10]:
        r = await create(CreateIn(question=body.question, environment=e.name, minutes=body.minutes), request, ctx, db)
        (await db.get(Investigation, uuid.UUID(r["id"]))).group_id = group
        out.append(r)
    return {"group_id": str(group), "investigations": out}


class FollowUpIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


@router.post("/investigations/{inv_id}/follow-up", status_code=201)
async def follow_up(inv_id: uuid.UUID, body: FollowUpIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    parent, env = await _load_visible(request, db, ctx, inv_id)
    minutes = max(5, int((parent.time_to - parent.time_from).total_seconds() // 60))
    if parent.conversation_id is None:  # started by an alert or a detection rule
        await conversations.start(db, parent)
    await conversations.join(db, parent.conversation_id, ctx.user.id)
    r = await start_investigation(CreateIn(question=body.question, environment=env.name, minutes=minutes, incident_id=parent.incident_id),
                                  request, ctx, db, parent.conversation_id, inherited_namespace=parent.namespace)
    (await db.get(Investigation, uuid.UUID(r["id"]))).parent_id = parent.id
    return r


@router.post("/investigations", status_code=201)
async def create(body: CreateIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await start_investigation(body, request, ctx, db)


class CrewIn(BaseModel):
    """The CrewAI flows investigate one workload in one namespace; both are required, nothing is inferred. `flow` picks the workload
    investigation (default) or the incident investigation (docs/crewai-sre-engine.md), whose `change_window` sets how far back it looks."""
    flow: Literal["workload_investigation", "incident_investigation"] = "workload_investigation"  # crew.config.FLOWS
    change_window: Literal["15m", "1h", "6h", "24h"] | None = None  # crew.incident_config.CHANGE_WINDOWS; incident investigation only
    environment: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    workload: str = Field(pattern=r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
    kind: Literal["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod"] = "Deployment"  # crew.config.KINDS
    question: str | None = Field(default=None, min_length=3, max_length=2000)
    minutes: int | None = Field(default=None, ge=5, le=10080)


@router.post("/investigations/crewai", status_code=201)
async def create_crewai(body: CrewIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Opt-in (docs/crewai-investigation.md). While MOKS_CREWAI_ENABLED is false this route answers 404, as if it did not exist.
    The run is the CrewAI console's workload investigation flow (docs/crewai-console.md): numbered, with its log."""
    from ..crew import config as crew_config
    from .crewai import start_run

    if not get_settings().crewai_enabled:
        raise HTTPException(404, "Not Found")
    environment = body.environment
    if environment is None:  # as before: the default environment, or the only one the person may investigate
        environment = (await resolve_scope(db, ctx, body.question or body.workload, None, body.namespace))[0].name
    inputs = {"environment": environment, "namespace": body.namespace, "workload": body.workload, "kind": body.kind, "minutes": body.minutes, "question": body.question}
    if body.flow == crew_config.INCIDENT_FLOW:
        inputs["change_window"] = body.change_window
    elif body.change_window:
        raise HTTPException(422, "change_window: only the incident investigation flow takes a change window")
    return await start_run(request, ctx, db, body.flow, inputs)


class UnifiedIn(BaseModel):
    environment: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
    workload: str | None = Field(default=None, pattern=r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")  # optional: MOKS finds the unhealthy one
    kind: Literal["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod"] = "Deployment"
    question: str | None = Field(default=None, min_length=3, max_length=2000)
    minutes: int | None = Field(default=None, ge=5, le=10080)


@router.post("/investigations/unified", status_code=201)
async def create_unified(body: UnifiedIn, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """The unified investigation pipeline (docs/UNIFIED_INVESTIGATION.md): the CrewAI console's unified_investigation flow, on the
    CrewAI worker. Like /investigations/crewai it answers 404 while MOKS_CREWAI_ENABLED is false."""
    from ..intelligence import FLOW
    from .crewai import start_run

    if not get_settings().crewai_enabled:
        raise HTTPException(404, "Not Found")
    environment = body.environment or (await resolve_scope(db, ctx, body.question or body.workload or body.namespace, None, body.namespace))[0].name
    inputs = {"environment": environment, "namespace": body.namespace, "workload": body.workload, "kind": body.kind, "minutes": body.minutes, "question": body.question}
    return await start_run(request, ctx, db, FLOW, inputs)


async def start_investigation(body: CreateIn, request: Request, ctx: AuthContext, db: AsyncSession, conversation_id: uuid.UUID | None = None,
                              inherited_namespace: str | None = None, crewai: dict[str, str] | None = None) -> dict[str, Any]:
    """conversation_id is never taken from a request body: callers have checked the person may post there.
    inherited_namespace is the conversation's: a default for a follow-up that names no target of its own, never an
    override, so a question about something new is not pinned to the namespace of the last one."""
    s = get_settings()
    limiter.check(f"inv:{ctx.user.id}", s.rate_investigations_per_minute)
    queued = (await db.execute(select(func.count()).select_from(Investigation).where(Investigation.status == "queued"))).scalar_one()
    if queued >= s.investigation_queue_limit:
        raise HTTPException(429, "Too many investigations are waiting; try again shortly", headers={"Retry-After": "30"})
    why_ns = "chosen" if body.namespace else None
    if body.namespace is None and inherited_namespace:
        from ..orchestrator.intent import explicit_targets

        if not explicit_targets(body.question):
            body, why_ns = body.model_copy(update={"namespace": inherited_namespace}), "the conversation's namespace"
    try:
        env, ns, why_env = await resolve_scope(db, ctx, body.question, body.environment, body.namespace)
    except HTTPException as exc:
        if exc.status_code != 422 or not inherited_namespace:
            raise
        # A namespace-scoped person named a target MOKS cannot place: stay where the conversation was, and say so.
        env, ns, why_env = await resolve_scope(db, ctx, body.question, body.environment, inherited_namespace)
        why_ns = "the conversation's namespace (the question's target is in none of your namespaces by name)"
    why_ns = why_ns or ("named in the question" if ns in set(re.findall(r"[a-z0-9][a-z0-9-]{1,62}", body.question.lower())) else
                        "all namespaces you may investigate; MOKS resolves the target" if ns == "*" else "your only namespace")
    body = body.model_copy(update={"environment": env.name, "namespace": ns})
    # "*" (all namespaces) is matched literally: only bindings whose namespace pattern is "*" allow it.
    await require(request, ctx, P.INVESTIGATION_RUN, env.name, body.namespace)
    try:
        scope = effective_scope(ctx.principal, env, P.INVESTIGATION_RUN)
        if body.namespace == "*":
            if not scope.user_patterns:
                raise ScopeError("no namespaces in scope")
        else:
            scope.require(body.namespace)
    except ScopeError as exc:
        from ..auth.deps import deny

        raise await deny(request, ctx, P.INVESTIGATION_RUN, env.name, body.namespace, str(exc))
    minutes = body.minutes or window_from_question(body.question) or s.default_query_window_minutes
    window = min(timedelta(minutes=minutes), max_window(env))
    now = datetime.now(timezone.utc)
    inv = Investigation(created_by=ctx.user.id, environment_id=env.id, namespace=body.namespace, question=body.question, requested_skill=body.skill,
                        time_from=now - window, time_to=now, status="queued")
    if body.incident_id:
        from ..incidents import add_event
        from ..models import Incident

        inc = await db.get(Incident, body.incident_id)
        if inc is None or inc.environment_id != env.id or not ctx.principal.can(P.INCIDENT_WRITE, env.name, inc.namespace):
            raise HTTPException(404, "Incident not found in this environment")
        inv.incident_id = inc.id
    db.add(inv)
    await db.flush()
    if inv.incident_id:
        await add_event(db, inv.incident_id, "investigation", {"text": f"Investigation started: {body.question[:200]}", "investigation_id": str(inv.id)}, ctx.user.id)
    db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued",
                              payload={"window_minutes": int(window.total_seconds() // 60), "requested_minutes": minutes,
                                       "environment": env.name, "environment_reason": why_env, "namespace": ns, "namespace_reason": why_ns, "traceparent": traceparent(),
                                       **({"crewai": crewai} if crewai else {})}))
    await audit.record(db, action="investigation.create", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="investigation", resource_id=str(inv.id), environment=env.name, namespace=body.namespace,
                       decision="allow", details={"question_chars": len(body.question), "window_minutes": int(window.total_seconds() // 60),
                                                  **({"workflow": "crewai", **{k: v for k, v in crewai.items() if k != "draft"}} if crewai else {})},
                       ip=client_ip(request))
    await db.flush()
    if conversation_id:
        inv.conversation_id = conversation_id
        db.add(ConversationMessage(conversation_id=conversation_id, author_id=ctx.user.id, role="user", content=body.question, investigation_id=inv.id))
        await conversations.touch(db, conversation_id)
        await db.flush()
    else:
        await conversations.start(db, inv)
    return serialize(inv, env.name, ctx.user.email)


async def _load_visible(request: Request, db: AsyncSession, ctx: AuthContext, inv_id: uuid.UUID) -> tuple[Investigation, Environment]:
    inv = await db.get(Investigation, inv_id)
    if inv is None:
        raise HTTPException(404, "Investigation not found")
    env = await db.get(Environment, inv.environment_id)
    conv = await db.get(Conversation, inv.conversation_id) if inv.conversation_id else None
    if conv is not None and conv.deleted_at is not None and conv.created_by != ctx.user.id and not ctx.principal.can(P.ADMIN_POLICIES, "*", "*"):
        raise HTTPException(404, "Investigation not found")
    # An all-namespace investigation may hold evidence from any namespace, so only principals
    # with namespace "*" may read it ("*" is matched literally by fnmatch).
    if not ctx.principal.can(P.INVESTIGATION_READ, env.name, inv.namespace):
        raise HTTPException(404, "Investigation not found")  # do not reveal existence
    return inv, env


@router.get("/investigations")
async def list_(request: Request, environment: str | None = None, limit: int = 50, ctx: AuthContext = Depends(get_auth),
                db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    q = (select(Investigation, Environment.name, User.email).join(Environment, Environment.id == Investigation.environment_id)
         .join(User, User.id == Investigation.created_by).outerjoin(Conversation, Conversation.id == Investigation.conversation_id)
         .where(Conversation.deleted_at.is_(None)).order_by(Investigation.created_at.desc()).limit(min(max(limit, 1), 200) * 3))
    if environment:
        q = q.where(Environment.name == environment)
    out = []
    for inv, env_name, email in (await db.execute(q)).all():
        if ctx.principal.can(P.INVESTIGATION_READ, env_name, inv.namespace):
            out.append(serialize(inv, env_name, email))
        if len(out) >= min(max(limit, 1), 200):
            break
    return out


@router.get("/investigations/{inv_id}")
async def get(inv_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    inv, env = await _load_visible(request, db, ctx, inv_id)
    user = await db.get(User, inv.created_by)
    ev = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id).order_by(Evidence.collected_at))).scalars().all()
    out = serialize(inv, env.name, user.email if user else None, full=True)
    out["evidence"] = [{
        "ref": e.ref, "tool": e.tool, "connector_type": e.connector_type, "args": e.args, "scope": e.scope, "ok": e.ok, "kind": e.kind,
        "summary": e.summary, "data": e.data, "source": e.source, "truncated": e.truncated, "error": e.error,
        "duration_ms": e.duration_ms, "collected_at": e.collected_at.isoformat(),
    } for e in sorted(ev, key=lambda e: int(e.ref[1:]))]
    out["grafana_url"] = env.grafana_url
    from ..models import ActionPlan

    plans = (await db.execute(select(ActionPlan).where(ActionPlan.investigation_id == inv.id).order_by(ActionPlan.created_at))).scalars().all()
    out["actions"] = [{"id": str(p.id), "action": p.action, "target": f"{p.target_kind}/{p.target_name}", "status": p.status,
                       "validated": bool((p.result or {}).get("validation", {}).get("ok")), "finished_at": p.finished_at.isoformat() if p.finished_at else None} for p in plans]
    follow = (await db.execute(select(Investigation).where(Investigation.parent_id == inv.id).order_by(Investigation.created_at))).scalars().all()
    out["follow_ups"] = [{"id": str(i.id), "question": i.question, "status": i.status} for i in follow if ctx.principal.can(P.INVESTIGATION_READ, env.name, i.namespace)]
    out["parent_id"] = str(inv.parent_id) if inv.parent_id else None
    queued = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == inv.id, InvestigationEvent.seq == 0))).scalar_one_or_none() or {}
    out["trace_id"] = trace_id_of(queued.get("traceparent"))  # the key that finds this run in Jaeger and its lines in Loki
    return out


def _contract(inv: Investigation, env: Environment, rows: list[Evidence]) -> list[dict[str, Any]]:
    """The shared evidence contract for each stored row. The unified pipeline's own items (with the stage, the agent and the
    signals narrowed to its target) are served as it built them; any other engine's rows are described by the same adapter."""
    from ..intelligence import evidence as contract, signals as sig

    built = {i.get("id"): i for i in ((inv.report or {}).get("unified") or {}).get("evidence_items") or [] if isinstance(i, dict)}
    scope = contract.Scope(investigation_id=str(inv.id), environment=env.name, principal_id=str(inv.created_by), time_from=inv.time_from, time_to=inv.time_to,
                           max_age_seconds=get_settings().evidence_max_age_seconds)
    out = []
    for e in sorted(rows, key=lambda e: int(e.ref[1:])):
        if e.ref in built:
            out.append(built[e.ref])
            continue
        keys = sorted({x.key for x in sig.extract({"ref": e.ref, "tool": e.tool, "args": e.args or {}, "ok": e.ok, "data": e.data}, None, (inv.time_from, inv.time_to))})
        out.append(contract.from_row(e, scope, signals=keys, now=inv.finished_at or datetime.now(timezone.utc)).model_dump(mode="json"))
    return out


@router.get("/investigations/{inv_id}/evidence-items")
async def evidence_items(inv_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Every piece of evidence this investigation holds, in the shared contract (moks.evidence/v1), whichever engine collected it."""
    from ..intelligence.evidence import CONTRACT

    inv, env = await _load_visible(request, db, ctx, inv_id)
    rows = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id))).scalars().all()
    return {"contract": CONTRACT, "investigation_id": str(inv.id), "items": _contract(inv, env, list(rows))}


@router.get("/investigations/{inv_id}/evidence/{ref}")
async def evidence_item(inv_id: uuid.UUID, ref: str, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """One piece of evidence: its contract and the stored, redacted result it describes (the contract's raw_ref)."""
    if not re.fullmatch(r"E\d{1,5}", ref):
        raise HTTPException(404, "Evidence not found")
    inv, env = await _load_visible(request, db, ctx, inv_id)
    row = (await db.execute(select(Evidence).where(Evidence.investigation_id == inv.id, Evidence.ref == ref))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Evidence not found")
    return {"item": _contract(inv, env, [row])[0], "stored": {"ref": row.ref, "tool": row.tool, "connector_type": row.connector_type, "args": row.args, "scope": row.scope,
                                                               "ok": row.ok, "kind": row.kind, "summary": row.summary, "data": row.data, "source": row.source,
                                                               "truncated": row.truncated, "error": row.error, "duration_ms": row.duration_ms,
                                                               "collected_at": row.collected_at.isoformat()}}


@router.get("/intelligence/evidence-schema")
async def evidence_schema(ctx: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    """The JSON Schema of the shared evidence contract."""
    from ..intelligence.evidence import schema

    return schema()


def trace_id_of(value: str | None) -> str | None:
    """The trace id from a W3C traceparent (00-<32 hex>-<16 hex>-<flags>), or None."""
    parts = (value or "").split("-")
    return parts[1] if len(parts) == 4 and re.fullmatch(r"[0-9a-f]{32}", parts[1]) and parts[1] != "0" * 32 else None


@router.post("/investigations/{inv_id}/cancel")
async def cancel(inv_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    inv, env = await _load_visible(request, db, ctx, inv_id)
    if inv.created_by != ctx.user.id:
        await require(request, ctx, P.ADMIN_POLICIES, "*", "*")
    if inv.status in TERMINAL:
        return {"status": inv.status}
    inv.cancel_requested = True
    await audit.record(db, action="investigation.cancel", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="investigation", resource_id=str(inv.id), environment=env.name, namespace=inv.namespace)
    return {"status": "cancelling"}


STREAM_SECONDS = 600  # one connection; the browser reconnects with Last-Event-ID


@router.get("/investigations/{inv_id}/events")
async def events(inv_id: uuid.UUID, request: Request, after: int = -1, ctx: AuthContext = Depends(get_auth),
                 db: AsyncSession = Depends(get_db, scope="function")) -> StreamingResponse:
    """Server-Sent Events. Resumable with Last-Event-ID; bounded to 10 minutes per connection."""
    inv, env = await _load_visible(request, db, ctx, inv_id)
    env_name, namespace = env.name, inv.namespace
    cookie = request.cookies.get(COOKIE)
    await db.commit()  # release the pooled connection; the stream uses short sessions
    last = request.headers.get("last-event-id")
    cursor = int(last) if last and last.lstrip("-").isdigit() else after

    async def poll():
        nonlocal cursor
        deadline = asyncio.get_running_loop().time() + STREAM_SECONDS
        idle = 0
        checked = asyncio.get_running_loop().time()
        yield "retry: 2000\n\n"
        while asyncio.get_running_loop().time() < deadline:
            if await request.is_disconnected():
                ended["outcome"] = "disconnected"
                return
            if asyncio.get_running_loop().time() - checked >= STREAM_RECHECK_SECONDS:
                checked = asyncio.get_running_loop().time()
                async with session_scope() as s:
                    current = await load(s, cookie)  # the session and today's roles, not the ones at connect
                    allowed = current is not None and current.principal.can(P.INVESTIGATION_READ, env_name, namespace)
                if not allowed:
                    ended["outcome"] = "revoked"
                    yield "event: revoked\ndata: {}\n\n"
                    return
            async with session_scope() as s:
                rows = (await s.execute(select(InvestigationEvent).where(
                    InvestigationEvent.investigation_id == inv_id, InvestigationEvent.seq > cursor
                ).order_by(InvestigationEvent.seq).limit(200))).scalars().all()
                status = (await s.get(Investigation, inv_id)).status
            for r in rows:
                cursor = r.seq
                ended["events"] += 1
                data = json.dumps({"type": r.type, "seq": r.seq, "at": r.created_at.isoformat(), **r.payload}, default=str)
                yield f"id: {r.seq}\nevent: {r.type}\ndata: {data}\n\n"
            if not rows:
                if status in TERMINAL:
                    ended["outcome"] = "end"
                    yield "event: end\ndata: {}\n\n"
                    return
                idle += 1
                if idle % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(min(0.5, STREAM_RECHECK_SECONDS))
            else:
                idle = 0
        ended["outcome"] = "timeout"
        yield "event: timeout\ndata: {}\n\n"

    ended = {"outcome": "cancelled", "events": 0}  # cancelled: the server stopped the stream (client gone, shutdown)

    async def stream():
        """The polling records nothing in the trace: the request's span ends with the stream and says how."""
        from opentelemetry import trace

        server, start = trace.get_current_span(), asyncio.get_running_loop().time()
        SSE_STREAMS_OPEN.inc()
        try:
            with unrecorded():
                async for chunk in poll():
                    yield chunk
        finally:
            SSE_STREAMS_OPEN.dec()
            SSE_STREAMS.labels(ended["outcome"]).inc()
            server.set_attribute("moks.stream.outcome", ended["outcome"])
            server.set_attribute("moks.stream.events", ended["events"])
            log.info("event stream closed", extra={"event": "sse_stream_closed", "operation": ended["outcome"], "durationMs": int((asyncio.get_running_loop().time() - start) * 1000),
                                                    "investigation_id": str(inv_id)})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
