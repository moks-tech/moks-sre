"""The CrewAI console's state (docs/crewai-console.md): run numbers, flows switched on or off, crew configuration
versions, and the scheduler that starts runs from crew_schedules.

The api writes everything here except what a schedule's firing records (next_run_at, last_*), which the worker
writes. A scheduled run is queued exactly like one a person starts: as the schedule's owner (the person who last saved
it), with that person's permissions checked again at every start, so a schedule never reaches further than its owner
can today. Several workers may run the scheduler at once: a compare-and-set on next_run_at lets one of them fire.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, conversations
from ..config import get_settings
from ..db import session_scope
from ..models import CrewConfig, CrewRun, CrewSchedule, CrewSetting, Environment, Investigation, InvestigationEvent, LLMConfig, User
from ..observability import traceparent
from ..rbac import P
from . import config as crew_config
from . import cron

log = logging.getLogger("moks.crewai.console")

STARTING_DEADLINE = timedelta(minutes=15)  # a firing missed by more than this (no worker ran) is skipped, not caught up
PURGE_EVERY_SECONDS = 3600
_purged_at = 0.0


# ---- settings and flows ----------------------------------------------------------------

async def setting(db: AsyncSession, key: str, default: dict[str, Any]) -> dict[str, Any]:
    row = await db.get(CrewSetting, key)
    return {**default, **((row.value if row else None) or {})}


async def put_setting(db: AsyncSession, key: str, value: dict[str, Any], user_id: uuid.UUID | None) -> None:
    await db.execute(text("""INSERT INTO crew_settings (key, value, updated_by, updated_at) VALUES (:k, CAST(:v AS jsonb), :u, now())
                             ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()"""),
                     {"k": key, "v": _json(value), "u": user_id})


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


async def flows_state(db: AsyncSession) -> dict[str, dict[str, Any]]:
    stored = await setting(db, "flows", {})
    return {name: {"enabled": True, "reason": None, **(stored.get(name) or {})} for name in crew_config.FLOWS}


async def flow_state(name: str, db: AsyncSession | None = None) -> dict[str, Any]:
    if db is not None:
        return (await flows_state(db)).get(name) or {"enabled": False, "reason": "unknown flow"}
    async with session_scope() as s:
        return (await flows_state(s)).get(name) or {"enabled": False, "reason": "unknown flow"}


async def set_flow(db: AsyncSession, name: str, enabled: bool, reason: str | None, user_id: uuid.UUID) -> dict[str, Any]:
    stored = await setting(db, "flows", {})
    stored[name] = {"enabled": enabled, "reason": None if enabled else (reason or None), "changed_at": datetime.now(timezone.utc).isoformat()}
    await put_setting(db, "flows", stored, user_id)
    return stored[name]


# ---- crew configuration ----------------------------------------------------------------

async def active_config(db: AsyncSession, crew: str = crew_config.CREW) -> CrewConfig | None:
    return (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew, CrewConfig.active.is_(True)))).scalar_one_or_none()


async def queue_block(db: AsyncSession, flow: str, draft: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a queued run records about its configuration, so a version activated later does not change it."""
    if draft is not None:
        overlay = crew_config.overlay_of(crew_config.merge(draft))
        return {"flow": flow, "config_version": None, "draft": overlay, "config_digest": crew_config.digest(overlay)}
    row = await active_config(db, crew_config.FLOWS[flow]["crew"])
    return {"flow": flow, "config_version": row.version if row else None, "config_digest": row.digest if row else ""}


async def run_config(request: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The configuration a queued run uses and how the log names it. Checked again here: a ceiling may have been
    lowered since the version was saved, and a version that no longer passes is refused rather than half-applied."""
    from . import CrewUnavailable

    overlay: dict[str, Any] | None = None
    if isinstance(request.get("draft"), dict):
        overlay, used = request["draft"], "test draft (not saved)"
    elif request.get("config_version"):
        async with session_scope() as db:
            row = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew_config.CREW, CrewConfig.version == int(request["config_version"])))).scalar_one_or_none()
        if row is None:
            raise CrewUnavailable(f"CrewAI investigation unavailable: crew configuration version {request['config_version']} no longer exists")
        overlay, used = row.config, f"version {row.version}"
    else:
        used = "the code's defaults"
    errors, _ = crew_config.check(overlay or {})
    if errors:
        raise CrewUnavailable("CrewAI investigation unavailable: the crew configuration (" + used + ") does not pass its checks: "
                              + "; ".join(f"{e['field']}: {e['message']}" for e in errors[:3]))
    return crew_config.merge(overlay), used


async def save_version(db: AsyncSession, crew: str, overlay: dict[str, Any], note: str, user_id: uuid.UUID, activate: bool) -> CrewConfig:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('crew_configs:' || :c))"), {"c": crew})
    last = (await db.execute(text("SELECT COALESCE(max(version), 0) FROM crew_configs WHERE crew = :c"), {"c": crew})).scalar_one()
    row = CrewConfig(crew=crew, version=int(last) + 1, config=overlay, digest=crew_config.digest(overlay), base_digest=crew_config.base_digest(),
                     note=note[:2000], active=False, created_by=user_id)
    db.add(row)
    await db.flush()
    if activate:
        await activate_version(db, crew, row.version)
    return row


async def activate_version(db: AsyncSession, crew: str, version: int | None) -> CrewConfig | None:
    """Makes one version active, or none (the code's defaults). Runs already queued keep the version they recorded."""
    await db.execute(update(CrewConfig).where(CrewConfig.crew == crew, CrewConfig.active.is_(True)).values(active=False))
    await db.flush()
    if version is None:
        return None
    row = (await db.execute(select(CrewConfig).where(CrewConfig.crew == crew, CrewConfig.version == version))).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no version {version}")
    row.active = True
    row.activated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


# ---- runs ------------------------------------------------------------------------------

async def record_run(db: AsyncSession, investigation_id: uuid.UUID, *, flow: str, trigger: str, inputs: dict[str, Any], created_by: uuid.UUID | None,
                     block: dict[str, Any], schedule_id: uuid.UUID | None = None, rerun_of: uuid.UUID | None = None) -> CrewRun:
    number = (await db.execute(text("""INSERT INTO crew_run_counters (flow, last) VALUES (:f, 1)
                                       ON CONFLICT (flow) DO UPDATE SET last = crew_run_counters.last + 1 RETURNING last"""), {"f": flow})).scalar_one()
    row = CrewRun(investigation_id=investigation_id, flow=flow, run_number=int(number), trigger=trigger, inputs=inputs, schedule_id=schedule_id, rerun_of=rerun_of,
                  config_version=block.get("config_version"), config_digest=block.get("config_digest") or "", created_by=created_by)
    db.add(row)
    await db.flush()
    return row


def clean_inputs(flow: str, raw: dict[str, Any], *, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """The flow's inputs, checked against its input list. Raises ValueError with the field that is wrong."""
    import re

    spec = crew_config.FLOWS.get(flow)
    if spec is None:
        raise ValueError(f"flow: unknown flow {flow!r}")
    raw = {**(defaults or {}), **{k: v for k, v in (raw or {}).items() if v not in (None, "")}}
    out: dict[str, Any] = {}
    known = {f["name"] for f in spec["inputs"]}
    for k in raw:
        if k not in known:
            raise ValueError(f"{k}: not an input of this flow")
    for f in spec["inputs"]:
        v = raw.get(f["name"], f.get("default"))
        if v in (None, ""):
            if f.get("required"):
                raise ValueError(f"{f['name']}: required")
            continue
        if f["type"] == "integer":
            if isinstance(v, str) and v.strip().isdigit():
                v = int(v)
            if not isinstance(v, int) or isinstance(v, bool) or not f.get("min", v) <= v <= f.get("max", v):
                raise ValueError(f"{f['name']}: a whole number from {f.get('min')} to {f.get('max')}")
        elif f["type"] == "choice":
            if v not in f["choices"]:
                raise ValueError(f"{f['name']}: one of {', '.join(f['choices'])}")
        else:
            if not isinstance(v, str):
                raise ValueError(f"{f['name']}: must be text")
            v = v.strip()
            if f.get("max") and len(v) > f["max"]:
                raise ValueError(f"{f['name']}: at most {f['max']} characters")
            if f.get("pattern") and not re.fullmatch(f["pattern"], v):
                raise ValueError(f"{f['name']}: {v!r} is not a valid Kubernetes name")
            if f["type"] == "environment" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", v):
                raise ValueError(f"{f['name']}: not an environment name")
            if f["name"] == "question" and len(v) < 3:
                raise ValueError("question: at least 3 characters")
        out[f["name"]] = v
    return out


async def flow_defaults(db: AsyncSession, flow: str) -> dict[str, Any]:
    row = await active_config(db, crew_config.FLOWS[flow]["crew"])
    return {k: v for k, v in crew_config.merge(row.config if row else None)["defaults"].items() if v is not None}


# ---- the scheduler -------------------------------------------------------------------------

def next_fire(expr: str, after: datetime) -> datetime | None:
    try:
        return cron.next_after(cron.parse(expr), after)
    except cron.CronError:
        return None


async def available(db: AsyncSession) -> str | None:
    """Why no CrewAI run can start now, or None."""
    from . import capability, worker_version

    cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none()
    state = capability(get_settings(), cfg, await worker_version(db))
    return None if state["available"] else state["reason"]


async def queue_scheduled(db: AsyncSession, sched: CrewSchedule, now: datetime) -> uuid.UUID:
    """Queues one run for a schedule as its owner. Raises PermissionError or ValueError with the reason it cannot."""
    from ..orchestrator.engine import load_principal
    from ..scope import ScopeError, effective_scope

    flow = crew_config.FLOWS.get(sched.flow)
    if flow is None:
        raise ValueError(f"unknown flow {sched.flow}")
    state = (await flows_state(db)).get(sched.flow) or {}
    if not state.get("enabled"):
        raise ValueError("the flow is switched off in the CrewAI console")
    why = await available(db)
    if why:
        raise ValueError(why)
    inputs = clean_inputs(sched.flow, sched.inputs, defaults=await flow_defaults(db, sched.flow))
    env = (await db.execute(select(Environment).where(Environment.name == inputs["environment"]))).scalar_one_or_none()
    if env is None:
        raise ValueError(f"environment {inputs['environment']} does not exist")
    try:
        principal = await load_principal(db, sched.owner_id)
        if not principal.can(P.INVESTIGATION_RUN, env.name, inputs["namespace"]):
            raise ScopeError(f"needs investigation.run in {env.name}/{inputs['namespace']}")
        effective_scope(principal, env, P.INVESTIGATION_RUN).require(inputs["namespace"])
    except ScopeError as exc:
        raise PermissionError(f"the schedule's owner may no longer run it: {exc}") from None
    s = get_settings()
    queued = (await db.execute(select(Investigation.id).where(Investigation.status == "queued").limit(s.investigation_queue_limit))).all()
    if len(queued) >= s.investigation_queue_limit:
        raise ValueError("too many investigations are waiting; the next firing tries again")
    window = min(timedelta(minutes=inputs.get("minutes") or s.default_query_window_minutes), timedelta(minutes=min(env.max_query_window_minutes, s.max_query_window_minutes)))
    question = inputs.get("question") or (f"Why is {inputs['kind']} {inputs['workload']} in namespace {inputs['namespace']} unhealthy?" if inputs.get("workload")
                                          else f"What is wrong in namespace {inputs['namespace']}, and why?")  # the unified flow finds the workload itself
    target = {k: inputs[k] for k in ("workload", "kind") if inputs.get(k)}
    owner = await db.get(User, sched.owner_id)
    inv = Investigation(created_by=sched.owner_id, environment_id=env.id, namespace=inputs["namespace"], question=question, time_from=now - window, time_to=now,
                        status="queued", trigger="schedule")
    db.add(inv)
    await db.flush()
    block = await queue_block(db, sched.flow)
    db.add(InvestigationEvent(investigation_id=inv.id, seq=0, type="queued", payload={
        "window_minutes": int(window.total_seconds() // 60), "requested_minutes": inputs.get("minutes"), "environment": env.name, "environment_reason": "the schedule's",
        "namespace": inputs["namespace"], "namespace_reason": "the schedule's", "traceparent": traceparent(), "trigger": "schedule", "schedule_id": str(sched.id),
        "crewai": {**target, **{k: v for k, v in block.items() if k != "config_digest"}}}))
    await audit.record(db, action="investigation.create", actor_id=sched.owner_id, actor_email=owner.email if owner else None, resource_type="investigation",
                       resource_id=str(inv.id), environment=env.name, namespace=inputs["namespace"], decision="allow",
                       details={"workflow": "crewai", "trigger": "schedule", "schedule_id": str(sched.id), "schedule": sched.name, **target})
    await conversations.start(db, inv)
    await record_run(db, inv.id, flow=sched.flow, trigger="schedule", inputs=inputs, created_by=sched.owner_id, block=block, schedule_id=sched.id)
    return inv.id


async def dispatch_due(now: datetime | None = None) -> list[dict[str, Any]]:
    """Starts every schedule that is due. Called by every worker's housekeeping; returns what it did, for tests and logs."""
    now = now or datetime.now(timezone.utc)
    async with session_scope() as db:
        due = (await db.execute(select(CrewSchedule.id, CrewSchedule.next_run_at).where(CrewSchedule.enabled.is_(True), CrewSchedule.next_run_at <= now)
                                .order_by(CrewSchedule.next_run_at).limit(50))).all()
    out = []
    for sid, at in due:
        try:
            out.append(await _fire(sid, at, now))
        except Exception:
            log.warning("schedule could not be started", extra={"event": "crewai_schedule_failed", "operation": str(sid)}, exc_info=True)
    return [o for o in out if o]


async def _fire(sid: uuid.UUID, due_at: datetime, now: datetime) -> dict[str, Any] | None:
    async with session_scope() as db:
        sched = await db.get(CrewSchedule, sid)
        if sched is None or not sched.enabled or sched.next_run_at != due_at:
            return None
        following = next_fire(sched.cron, now)
        # compare-and-set: only the worker that moves next_run_at on starts the run
        won = (await db.execute(update(CrewSchedule).where(CrewSchedule.id == sid, CrewSchedule.next_run_at == due_at).values(next_run_at=following)
                                .returning(CrewSchedule.id))).first()
        if won is None:
            return None
        if now - due_at > STARTING_DEADLINE:
            error = f"missed the firing at {due_at:%Y-%m-%d %H:%M} UTC (no scheduler ran within {int(STARTING_DEADLINE.total_seconds() // 60)} minutes)"
            await db.execute(update(CrewSchedule).where(CrewSchedule.id == sid).values(last_error=error))
            return {"schedule": str(sid), "outcome": "missed", "error": error}
    try:
        async with session_scope() as db:
            sched = await db.get(CrewSchedule, sid)
            inv_id = await queue_scheduled(db, sched, now)
            await db.execute(update(CrewSchedule).where(CrewSchedule.id == sid).values(last_run_at=now, last_run_id=inv_id, last_error=None))
        log.info("schedule started a run", extra={"event": "crewai_schedule_fired", "operation": str(sid), "investigation_id": str(inv_id)})
        return {"schedule": str(sid), "outcome": "started", "investigation_id": str(inv_id)}
    except (PermissionError, ValueError) as exc:
        async with session_scope() as db:
            await db.execute(update(CrewSchedule).where(CrewSchedule.id == sid).values(last_run_at=now, last_error=str(exc)[:2000]))
        log.info("schedule not started", extra={"event": "crewai_schedule_skipped", "operation": str(sid), "outcome": str(exc)[:200]})
        return {"schedule": str(sid), "outcome": "refused", "error": str(exc)}


async def housekeeping() -> None:
    """The worker's periodic part: due schedules, and retention once an hour."""
    global _purged_at
    try:
        await dispatch_due()
    except Exception:
        log.warning("CrewAI scheduler failed", extra={"event": "crewai_scheduler_failed"}, exc_info=True)
    if time.monotonic() - _purged_at >= PURGE_EVERY_SECONDS:
        _purged_at = time.monotonic()
        try:
            from .history import purge

            await purge()
        except Exception:
            log.warning("CrewAI run history retention failed", extra={"event": "crewai_retention_failed"}, exc_info=True)
