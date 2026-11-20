"""Read-only tools the unified investigation pipeline adds (docs/UNIFIED_INVESTIGATION.md). Like every tool they run
only through the ToolExecutor, so RBAC, scope, redaction, truncation, evidence and audit are the usual ones.

- ml_anomalies: the anomaly model's stored observations for a namespace or service, as citable evidence. It reads
  ml_observations and nothing else, needs ml.read in that namespace, and returns each observation's validation state
  (shadow or enabled) so nobody mistakes an unusual window for a cause.
- prom_service_signals: a service's golden signals (request rate, error ratio, p95 latency, database connections)
  in one call, from templated, namespace-pinned queries. These are the anomaly model's own feature queries
  (ml/features.py), so the pipeline and the model read the same series. No raw PromQL reaches it.
"""
from __future__ import annotations

from typing import Any

from pydantic import Field
from sqlalchemy import select

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..connectors.prometheus import auto_step
from ..db import session_scope
from ..rbac import P
from .contracts import K8S_NAME, ToolArgs, ToolContext, ToolResult, ToolSpec

NOTE = "Unusual for this workload compared with its own history. Not a fault, not a cause, and not an incident."
_WORKLOAD = 'label_replace({expr}, "workload", "$1", "pod", "^(.+?)(-[a-z0-9]{{6,10}}-[a-z0-9]{{5}}|-[0-9]+)$")'


class AnomalyArgs(ToolArgs):
    namespace: str = Field(pattern=K8S_NAME, max_length=63)
    service: str | None = Field(default=None, pattern=K8S_NAME, max_length=200, description="one workload; empty for every workload in the namespace")
    minutes: int | None = Field(default=None, ge=5, le=10080)
    unusual_only: bool = False
    limit: int = Field(default=20, ge=1, le=50)


async def ml_anomalies(ctx: ToolContext, a: AnomalyArgs) -> ToolResult:
    from ..ml import mode
    from ..models import MLModel, MLObservation

    start, end = ctx.window(a.minutes)
    async with session_scope() as db:
        state = await mode(db, "anomaly_detection")
        if state == "disabled":
            raise ConnectorError("config", "ML anomaly detection is disabled (capability anomaly_detection), so there are no observations to read")
        q = (select(MLObservation).where(MLObservation.environment == ctx.environment.name, MLObservation.namespace == a.namespace,
                                         MLObservation.window_to >= start, MLObservation.window_from <= end)
             .order_by(MLObservation.window_to.desc()).limit(a.limit))
        if a.service:
            q = q.where(MLObservation.service == a.service)
        if a.unusual_only:
            q = q.where(MLObservation.unusual.is_(True))
        rows = (await db.execute(q)).scalars().all()
        versions = {m.id: m for m in (await db.execute(select(MLModel).where(MLModel.id.in_([r.model_id for r in rows if r.model_id])))).scalars().all()} if rows else {}
    out: list[dict[str, Any]] = []
    for o in rows:
        m = versions.get(o.model_id)
        out.append({"id": str(o.id), "environment": o.environment, "namespace": o.namespace, "service": o.service, "window_from": o.window_from.isoformat(),
                    "window_to": o.window_to.isoformat(), "observed_at": o.observed_at.isoformat() if o.observed_at else None, "status": o.status, "mode": o.mode,
                    "algorithm": o.algorithm, "model_version": m.version if m else (o.context or {}).get("model_version"), "model_status": m.status if m else None,
                    "score": o.score, "threshold": o.threshold, "unusual": bool(o.unusual),
                    "deviations": [{"feature": d.get("feature"), "z": d.get("z"), "observed": d.get("observed"), "baseline": d.get("typical"), "direction": d.get("direction")}
                                   for d in (o.deviations or [])[:6] if isinstance(d, dict)],
                    "validation": {"mode": o.mode, "feedback": (o.feedback or {}).get("verdict"), "trusted": o.mode == "enabled"}, "data_quality": {k: v for k, v in (o.data_quality or {}).items() if k in ("coverage", "absent", "drift_psi")}})
    unusual = sum(1 for o in out if o["unusual"])
    summary = (f"{len(out)} anomaly observation(s) for {a.service or 'namespace ' + a.namespace} in the window, {unusual} unusual ({state} mode). " if out
               else f"No anomaly observation for {a.service or 'namespace ' + a.namespace} in the window ({state} mode). ") + NOTE
    return ToolResult(summary, out, {"api": "SELECT ml_observations", "mode": state, "note": NOTE, "scope": {"environment": ctx.environment.name, "namespace": a.namespace}})


class ServiceSignalsArgs(ToolArgs):
    namespace: str = Field(pattern=K8S_NAME, max_length=63)
    workload: str | None = Field(default=None, pattern=K8S_NAME, max_length=200, description="keep only this workload's series")
    minutes: int | None = Field(default=None, ge=5, le=10080)


def service_queries(namespace: str) -> dict[str, str]:
    from ..ml import features

    s = get_settings()
    q = features.queries(namespace, {"requests": s.unified_request_metric, "duration": s.unified_duration_metric, "status_label": s.unified_status_label})
    sel = f'namespace="{namespace}"'
    w = lambda e: f"sum by (workload) ({_WORKLOAD.format(expr=e)})"
    return {"request_rate": q["request_rate"], "errors_rate": q["errors_rate"], "latency_p95": q["latency_p95"],
            "db_connections": w(f"{s.unified_db_connections_metric}{{{sel}}}"), "db_connections_max": w(f"{s.unified_db_connections_max_metric}{{{sel}}}")}


def _ratio(errors: list[dict], requests: list[dict]) -> list[dict]:
    """errors / requests per workload and timestamp. No 5xx series means no 5xx responses (ml/features.py)."""
    by_wl = {(s.get("metric") or {}).get("workload"): {t: v for t, v in s.get("values") or []} for s in errors}
    out = []
    for s in requests:
        wl = (s.get("metric") or {}).get("workload")
        errs = by_wl.get(wl, {})
        values = [[t, (errs.get(t) or 0.0) / v if v else None] for t, v in s.get("values") or [] if v is not None]
        out.append({"metric": {"workload": wl}, "values": values})
    return out


async def prom_service_signals(ctx: ToolContext, a: ServiceSignalsArgs) -> ToolResult:
    prom = ctx.connector("prometheus")
    start, end = ctx.window(a.minutes)
    step = auto_step(start, end)
    queries = service_queries(a.namespace)
    raw: dict[str, Any] = {}
    for key, q in queries.items():
        try:
            r = await prom.range(q, start, end, step)
            series = [x for x in r.get("series") or [] if not a.workload or (x.get("metric") or {}).get("workload") == a.workload]
            raw[key] = {"series": series}
        except ConnectorError as exc:
            raw[key] = {"series": [], "error": exc.message}
    signals = {"request_rate": raw["request_rate"], "error_ratio": {"series": _ratio(raw["errors_rate"]["series"], raw["request_rate"]["series"])},
               "latency_p95": raw["latency_p95"], "db_connections": raw["db_connections"], "db_connections_max": raw["db_connections_max"]}
    missing = sorted(k for k, v in signals.items() if not v["series"])
    errors = {k: v["error"] for k, v in raw.items() if v.get("error")}
    summary = (f"golden signals for {a.workload or 'namespace ' + a.namespace}: {len(signals) - len(missing)} of {len(signals)} with data"
               + (f"; no series for {', '.join(missing)}" if missing else ""))
    return ToolResult(summary, {"signals": signals, "step": step, "missing": missing, "errors": errors, "workload": a.workload},
                      {"queries": list(queries.values()), "partial": bool(errors)})


TOOLS: list[ToolSpec] = [
    ToolSpec("ml_anomalies", "Anomaly-model observations for a namespace or workload in the window: score, threshold, the features that deviated with observed and "
             "baseline values, model version and whether the model is in shadow mode. Unusual is not a cause.", "knowledge", AnomalyArgs, ml_anomalies,
             extra_permission=P.ML_READ),
    ToolSpec("prom_service_signals", "A workload's golden signals in one read: request rate, error ratio, p95 latency and database connections against the pool limit "
             "(namespace-pinned templates; the exact PromQL is returned).", "prometheus", ServiceSignalsArgs, prom_service_signals),
]
