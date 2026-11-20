"""ML signals for one investigation target: its anomalies and the correlation groups it belongs to.

This is what an investigation (and the CrewAI crew, through the `ml_signals` tool) is shown. Every anomaly carries
what an engineer needs to weigh it: its type (service, infrastructure or multivariate), the affected resource, when
it was first flagged, and for each feature the baseline, the observed value and the deviation; the model, its
version and role; a confidence (low, medium, high, with reasons) and the model's validation status. Rows are
filtered by the reader's ml.read bindings; a group is shown only when every namespace it spans is readable.

The evidence these become is of kind `model_output` (orchestrator/evidence.py): it can never stand as an observed
fact or raise a cause above low confidence on its own. ML predictions are leads, not root causes.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MLCorrelation, MLObservation
from ..rbac import P, Principal
from . import correlate, mode, registry
from .features import NODE_NAMESPACE

NOTE = ("ML signals are statistical leads from shadow-mode models: unusual compared with this workload's own history. "
        "They are not observations of the system and not causes. Confirm anything they suggest with live evidence.")


def anomaly_of(o: MLObservation) -> dict[str, Any]:
    ctx = o.context or {}
    flagged = set(ctx.get("flagged_features") or [])
    feats = [{"feature": d.get("feature"), "baseline": d.get("typical"), "observed": d.get("observed"), "deviation": d.get("z"), "direction": d.get("direction"),
              "unit": d.get("unit"), "threshold": d.get("threshold"), "pattern": d.get("pattern"), "flagged": d.get("feature") in flagged or bool(d.get("flagged"))}
             for d in (o.deviations or [])[:6]]
    q = o.data_quality or {}
    return {"observation_id": str(o.id), "anomaly_type": ctx.get("anomaly_type") or ("infrastructure" if o.namespace == NODE_NAMESPACE else "service"),
            "resource": f"node/{o.service}" if o.namespace == NODE_NAMESPACE else f"{o.namespace}/{o.service}", "cohort": o.cohort,
            "detected_at": ctx.get("first_flagged_at") or o.window_to.isoformat(), "window": [o.window_from.isoformat(), o.window_to.isoformat()],
            "features": feats, "score": o.score, "threshold": o.threshold,
            "model": {"id": str(o.model_id) if o.model_id else None, "algorithm": o.algorithm, "version": ctx.get("model_version"), "role": ctx.get("role"), "mode": o.mode},
            "confidence": ctx.get("confidence") or {"level": "low", "reasons": ["not assessed"]}, "validation": ctx.get("validation") or {"status": "unvalidated"},
            "agreement": ctx.get("agreement", 0),
            "data_quality": {"coverage": {f: (q.get("coverage") or {}).get(f) for f in flagged}, "incomplete_queries": q.get("incomplete_queries") or [],
                             "drift_psi": q.get("drift_psi") or {}}}


def group_brief(row: MLCorrelation) -> dict[str, Any]:
    return {"group_id": str(row.id), "status": row.status, "first_seen": row.first_seen.isoformat(), "last_seen": row.last_seen.isoformat(),
            "members": [{"resource": f"node/{m['workload']}" if m["namespace"] == NODE_NAMESPACE else f"{m['namespace']}/{m['workload']}", "first": m["first"],
                         "features": m["features"]} for m in row.members][:12],
            "links": [{"between": [a[0] for a in (ln["a"], ln["b"])], "weight": ln["weight"], "why": [r["type"] for r in ln["reasons"] if r["type"] != "time"]} for ln in row.links][:12],
            "candidate_explanations": [{"rank": h["rank"], "kind": h["kind"], "statement": h["statement"], "status": "candidate, unvalidated"} for h in row.hypotheses],
            "context_gaps": (row.context or {}).get("gaps") or []}


async def for_target(db: AsyncSession, principal: Principal, environment: str, namespace: str, workload: str | None, end: datetime, minutes: int) -> dict[str, Any]:
    """The target's champion anomalies in the window, the open or recent groups that include it, and the active models."""
    state = await mode(db, "anomaly_detection")
    if state == "disabled":
        return {"status": "disabled", "anomalies": [], "groups": [], "note": NOTE}
    since = end - timedelta(minutes=minutes)
    prefix = f"{environment}/{namespace}/"
    models = [m for m in await registry.active(db, "anomaly_detection", prefix=prefix) if workload is None or m.cohort == prefix + workload]
    q = select(MLObservation).where(MLObservation.environment == environment, MLObservation.namespace == namespace, MLObservation.status == "scored",
                                    MLObservation.unusual.is_(True), MLObservation.window_to >= since, MLObservation.window_to <= end + timedelta(minutes=5))
    if workload:
        q = q.where(MLObservation.service == workload)
    rows = [o for o in (await db.execute(q.order_by(MLObservation.window_to.desc()).limit(200))).scalars().all()
            if principal.can(P.ML_READ, o.environment, o.namespace) and (o.context or {}).get("role") == "champion"]
    seen: set[str] = set()
    anomalies = []
    for o in rows:  # the latest window per cohort and first-flag time: one anomaly, not one per tick
        key = f"{o.cohort}@{(o.context or {}).get('first_flagged_at')}"
        if key not in seen:
            seen.add(key)
            anomalies.append(anomaly_of(o))
    cohort = prefix + (workload or "")
    groups = (await db.execute(select(MLCorrelation).where(MLCorrelation.environment == environment, MLCorrelation.last_seen >= since).order_by(MLCorrelation.last_seen.desc())
                               .limit(50))).scalars().all()
    mine = [g for g in groups if correlate.readable(principal, g) and any(m["cohort"] == cohort or (workload is None and m["namespace"] == namespace) for m in g.members)]
    status = "ok" if anomalies or mine else ("no_models" if not models else "nothing_unusual")
    return {"status": status, "mode": state, "window": [since.isoformat(), end.isoformat()], "models": [{"algorithm": m.algorithm, "version": m.version, "role": m.status,
                                                                                                            "gates_passed": registry.gates_passed(m)[0]} for m in models][:10],
            "anomalies": anomalies[:10], "groups": [group_brief(g) for g in mine[:3]], "note": NOTE}
