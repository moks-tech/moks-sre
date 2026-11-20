"""Historical incident intelligence: what keeps failing, where, what was done, and what worked.

Built from reviewed outcomes only:
  - learning records of approved incidents (a postmortem someone approved), and
  - investigation records a reviewer approved (a confirmed cause), when their incident has no approved record of
    its own, so one problem is not counted twice;
  - reviewed `skill_recommendation` judgments, for skills that helped.
Nothing unreviewed is used, and no investigation output counts until a person confirms it. Access is decided per
record before anything is counted, with the rule similar incidents use: knowledge access and ml.read in the
record's environment and namespace, inside the request's scope. A reader never sees counts that include what they
may not read. Evaluation-realm records never mix with operational ones.

What it returns:
  recurring        records whose symptoms and error signatures match (Jaccard >= 0.5), two or more times
  infrastructure   repeated problems with nodes, volumes, scheduling, eviction, image pulls or ingress
  services         the services most often affected, with how long their incidents took to resolve
  runbooks         runbooks named on approved incidents, documents cited by approved investigations whose
                   resolution was confirmed (only those the reader may read), and skills reviewers found helpful
  what_worked      resolutions recorded on incidents whose cause was verified
History says what happened before. It is not evidence of what is happening now.
"""
from __future__ import annotations

import statistics
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Document, Incident, KnowledgeSource, LearningJudgment, LearningRecord
from ..rag import access
from ..rbac import Principal
from .similar import _jaccard, _readable

NOTE = "History of reviewed incidents you may read. It shows what happened before, not what is happening now."
INFRA_SYMPTOMS = {"evicted", "pending", "image_pull", "ingress_backend_missing"}
INFRA_KINDS = {"node", "persistentvolumeclaim", "persistentvolume", "storageclass", "ingress", "certificate"}
MATCH = 0.5


def _when(r: LearningRecord) -> datetime | None:
    return r.event_from or r.created_at


def _hours(r: LearningRecord) -> float | None:
    end = (r.outcome or {}).get("resolved_at")
    start = r.event_from
    if not end or not start:
        return None
    try:
        return max(0.0, (datetime.fromisoformat(str(end)) - start).total_seconds() / 3600)
    except ValueError:
        return None


def _services(r: LearningRecord) -> set[str]:
    return set((r.features or {}).get("services") or []) | ({r.service} if r.service else set())


def _key(r: LearningRecord) -> set[str]:
    f = r.features or {}
    return {f"s:{x}" for x in f.get("symptoms") or []} | {f"t:{x}" for x in (f.get("signature") or [])[:15]}


def _link(r: LearningRecord) -> dict[str, Any]:
    base = get_settings().base_path.rstrip("/")
    return {"record": str(r.id), "incident": str(r.incident_id) if r.incident_id else None, "url": f"{base}/incidents/{r.incident_id}" if r.incident_id else None,
            "at": _when(r).isoformat() if _when(r) else None, "service": r.service, "namespace": r.namespace}


def cluster(records: list[LearningRecord]) -> list[list[LearningRecord]]:
    """Greedy, in time order: a record joins the first cluster whose founding pattern it matches at MATCH or more."""
    out: list[tuple[set[str], list[LearningRecord]]] = []
    for r in sorted(records, key=lambda r: (_when(r) or datetime.min.replace(tzinfo=timezone.utc), str(r.id))):
        k = _key(r)
        if not k:
            continue
        home = next((c for c in out if _jaccard(c[0], k) >= MATCH), None)
        if home is None:
            out.append((k, [r]))
        else:
            home[1].append(r)
    return [c[1] for c in out]


def _pattern(rs: list[LearningRecord]) -> dict[str, Any]:
    sym = Counter(x for r in rs for x in (r.features or {}).get("symptoms") or [])
    sig = Counter(x for r in rs for x in ((r.features or {}).get("signature") or [])[:15])
    half = len(rs) / 2
    return {"symptoms": sorted(x for x, n in sym.items() if n >= half), "signature": [x for x, n in sig.most_common(8) if n >= half]}


def _causes(rs: list[LearningRecord]) -> list[dict]:
    c = Counter((r.label or {}).get("confirmed_cause", "").strip()[:300] for r in rs)
    return [{"cause": k, "times": n} for k, n in c.most_common(3) if k]


async def summarize(db: AsyncSession, principal: Principal, *, environment: str | None = None, namespace: str | None = None, service: str | None = None,
                    days: int = 180, limit: int = 8, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    namespace = None if namespace in (None, "*") else namespace
    q = select(LearningRecord).where(LearningRecord.label_state == "approved", LearningRecord.realm == access.realm()).order_by(LearningRecord.created_at.desc()).limit(5000)
    if environment:
        q = q.where(LearningRecord.environment == environment)
    rows = [r for r in (await db.execute(q)).scalars().all() if (_when(r) or now) >= since and _readable(principal, r, environment, namespace)]
    incident_ids = {r.incident_id for r in rows if r.kind == "incident" and r.incident_id}
    rows = [r for r in rows if r.kind == "incident" or r.incident_id not in incident_ids]  # one problem, counted once
    scope = [r for r in rows if service is None or service in _services(r)]
    out: dict[str, Any] = {"scope": {"environment": environment, "namespace": namespace, "service": service, "days": days}, "records": len(scope), "note": NOTE,
                           "sources": "approved incidents and reviewer-approved investigations; reviewed skill judgments"}
    if not scope:
        return {**out, "recurring": [], "infrastructure": [], "services": [], "runbooks": [], "what_worked": [], "reason": "no reviewed incident you may read in this scope"}

    recurring = []
    for rs in cluster(scope):
        if len(rs) < 2:
            continue
        hrs = [h for r in rs if (h := _hours(r)) is not None]
        verified = sum(1 for r in rs if (r.label or {}).get("verification") == "verified")
        recurring.append({"pattern": _pattern(rs), "occurrences": len(rs), "services": dict(Counter(s for r in rs for s in _services(r)).most_common(6)),
                          "first_seen": _when(rs[0]).isoformat() if _when(rs[0]) else None, "last_seen": _when(rs[-1]).isoformat() if _when(rs[-1]) else None,
                          "causes": _causes(rs), "verified_share": round(verified / len(rs), 2), "median_hours_to_resolve": round(statistics.median(hrs), 1) if hrs else None,
                          "examples": [_link(r) for r in rs[-3:]]})
    recurring.sort(key=lambda x: x["last_seen"] or "", reverse=True)
    recurring.sort(key=lambda x: -x["occurrences"])  # most frequent first, then most recent

    infra: dict[str, list[LearningRecord]] = {}
    for r in scope:
        f = r.features or {}
        kinds = set(f.get("resource_kinds") or []) & INFRA_KINDS
        syms = set(f.get("symptoms") or []) & INFRA_SYMPTOMS
        for k in sorted(syms) + [f"resource:{x}" for x in sorted(kinds)]:
            infra.setdefault(k, []).append(r)
    infrastructure = [{"problem": k.replace("resource:", "") + (" (resource)" if k.startswith("resource:") else ""), "occurrences": len(rs),
                       "resources": sorted({str(r.resource) for r in rs if r.resource})[:6], "services": sorted({s for r in rs for s in _services(r)})[:6],
                       "last_seen": max((_when(r) for r in rs if _when(r)), default=now).isoformat(), "causes": _causes(rs), "examples": [_link(r) for r in rs[-3:]]}
                      for k, rs in infra.items() if len(rs) >= 2]
    infrastructure.sort(key=lambda x: -x["occurrences"])

    by_service: dict[str, list[LearningRecord]] = {}
    for r in rows:  # services across the whole readable scope, so "most often affected" is relative
        for s in _services(r):
            by_service.setdefault(s, []).append(r)
    services = []
    for s, rs in sorted(by_service.items(), key=lambda kv: -len(kv[1]))[:limit]:
        hrs = [h for r in rs if (h := _hours(r)) is not None]
        services.append({"service": s, "incidents": len(rs), "last_30_days": sum(1 for r in rs if (_when(r) or since) >= now - timedelta(days=30)),
                         "last_seen": max((_when(r) for r in rs if _when(r)), default=now).isoformat(), "median_hours_to_resolve": round(statistics.median(hrs), 1) if hrs else None,
                         "top_symptoms": [x for x, _ in Counter(y for r in rs for y in (r.features or {}).get("symptoms") or []).most_common(3)]})

    out.update(recurring=recurring[:limit], infrastructure=infrastructure[:limit], services=services, runbooks=await _runbooks(db, principal, scope, limit),
               what_worked=_worked(scope, limit))
    return out


async def _runbooks(db: AsyncSession, principal: Principal, rs: list[LearningRecord], limit: int) -> list[dict]:
    out: list[dict] = []
    incidents = {i.id: i for i in (await db.execute(select(Incident).where(Incident.id.in_([r.incident_id for r in rs if r.kind == "incident" and r.incident_id])))).scalars().all()}
    named = Counter((incidents[r.incident_id].runbook or "").strip()[:300] for r in rs if r.incident_id in incidents and (incidents[r.incident_id].runbook or "").strip())
    out += [{"kind": "incident_runbook", "runbook": k, "used": n, "why": "named on approved incidents in this scope"} for k, n in named.most_common(limit)]
    cited: Counter = Counter()
    for r in rs:
        if r.kind != "investigation" or (r.outcome or {}).get("resolution") != "confirmed":
            continue
        ret = r.retrieval or {}
        ids = set(ret.get("cited") or [])
        for p in ret.get("passages") or []:
            if p.get("document_id") and (p.get("id") in ids or p.get("state") == "cited"):
                cited[p["document_id"]] += 1
    if cited:
        ids = []
        for d in cited:
            try:
                ids.append(uuid.UUID(str(d)))
            except ValueError:
                continue
        docs = (await db.execute(select(Document, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == Document.source_id).where(Document.id.in_(ids)))).all()
        for d, src in docs:
            if not access.can_read(principal, d.environment, d.namespace):  # a document the reader may not read is not named, or counted
                continue
            out.append({"kind": "cited_document", "document_id": str(d.id), "title": d.title or d.path, "path": d.path, "source": src.name, "used": cited[str(d.id)],
                        "why": "cited by approved investigations whose resolution was confirmed"})
    ctx = [str(r.investigation_id) for r in rs if r.investigation_id]
    if ctx:
        judged = (await db.execute(select(LearningJudgment).where(LearningJudgment.target_kind == "skill_recommendation", LearningJudgment.state == "reviewed",
                                                                  LearningJudgment.context_id.in_(ctx)))).scalars().all()
        helpful = Counter(j.target_id for j in judged if j.verdict == "helpful")
        unhelpful = Counter(j.target_id for j in judged if j.verdict == "not_helpful")
        out += [{"kind": "skill", "skill": k, "helpful": n, "not_helpful": unhelpful.get(k, 0), "why": "reviewed judgments on investigations in this scope"}
                for k, n in helpful.most_common(limit)]
    out.sort(key=lambda x: -(x.get("used") or x.get("helpful") or 0))
    return out[: limit * 2]


def _worked(rs: list[LearningRecord], limit: int) -> list[dict]:
    """Resolutions recorded where the cause was verified: the closest thing to "this fixed it before"."""
    seen: dict[str, dict] = {}
    for r in sorted(rs, key=lambda r: _when(r) or datetime.min.replace(tzinfo=timezone.utc)):
        label = r.label or {}
        text = (label.get("resolution") or "").strip()
        if not text or label.get("verification") != "verified":
            continue
        k = text[:200].lower()
        cur = seen.setdefault(k, {"resolution": text[:400], "times": 0, "services": set(), "symptoms": set(), "last": None, "example": None})
        cur["times"] += 1
        cur["services"] |= _services(r)
        cur["symptoms"] |= set((r.features or {}).get("symptoms") or [])
        cur["last"], cur["example"] = _when(r), _link(r)
    out = [{"resolution": v["resolution"], "times": v["times"], "services": sorted(v["services"])[:6], "symptoms": sorted(v["symptoms"])[:6],
            "last_used": v["last"].isoformat() if v["last"] else None, "example": v["example"], "cause_verified": True} for v in seen.values()]
    out.sort(key=lambda x: (-x["times"], x["last_used"] or ""))
    return out[:limit]
