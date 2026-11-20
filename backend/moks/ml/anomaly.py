"""Metric anomaly detection: offline training and comparison, bounded shadow inference, health and reproduction.

Training (`moks ml train`, a Kubernetes CronJob, or an admin request) reads up to ml_anomaly_training_days of
features for each workload in a namespace (or each node), refuses cohorts with less than
ml_anomaly_min_history_days of usable history, and fits every algorithm in detectors.py side by side:

  - chronological split: fitted on the earlier 80%, judged on the rest; an embargo of one rate window between
    them is excluded from judging, so no rate() spans the cut; online detectors read the training period only
    as history, never as anything to fit after the cut
  - approved incidents (reviewed postmortems) inside the training period are masked out of it, so a model does
    not learn a known incident as normal; nothing unreviewed is used, and investigation output never is
  - judged three ways on the validation period: flags per day on it as it was (an upper bound on false alarms if
    the period was healthy), recall and delay on approved incidents in it (when there are any), and recall,
    precision and delay on faults injected into it (benchmark.inject), the same faults for every algorithm
  - the training data is frozen as a content-addressed snapshot; its SHA-256 is the model's dataset version,
    and the state is refitted from the decoded snapshot to prove the fit is deterministic before registering
  - every model is registered as a candidate with its gates. Nothing is promoted automatically, and a champion
    is still shadow output.

Inference (the `ml_anomaly_tick` job) scores the latest window for every champion and shadow challenger in an
environment, with bounded cohorts, concurrency and query windows, reading extra history only for the online
detectors that need it. It writes ml_observations, then regroups the environment's anomalies (correlate.py), and
nothing else: it never opens an incident, changes a severity, or plans or runs an action, and deterministic
detection runs exactly as before. Missing telemetry, maintenance windows, an invalid artifact, a model trained on
another feature schema and too little history are recorded as such instead of being scored.

Scores mean the window is unusual for this cohort, not that something is broken, and never why.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import statistics
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import session_scope
from ..models import Environment, LearningJudgment, LearningRecord, MaintenanceWindow, MLArtifact, MLObservation
from ..observability import ML_DRIFT, ML_FALLBACKS, ML_FLAGS, ML_INFERENCE_SECONDS, ML_MISSING_FEATURES, ML_MODEL_HEALTH, ML_OBSERVATIONS
from . import artifacts, benchmark, detectors, evaluation, features, registry
from .features import FEATURE_SCHEMA, FEATURES, NODE_FEATURES, NODE_NAMESPACE, CohortFrame

log = logging.getLogger("moks.ml.anomaly")

CAPABILITY = "anomaly_detection"
ALGORITHMS = detectors.NAMES
TRAIN_SHARE = 0.8
MIN_VALIDATION_WINDOWS = 12
SNAPSHOT_MEDIA_TYPE = "application/vnd.moks.ml-dataset+json"
NOTE = "Unusual for this workload compared with its own history. Not a fault, not a cause, and not an incident."
_health_at: dict[str, float] = {}


# ---- Prometheus, through the executor ------------------------------------------------------------------


async def prometheus(db: AsyncSession, env: Environment) -> features.Query | None:
    """The automation principal's authorized path to this environment's Prometheus: the same executor, scope
    and connector detection uses. None when there is no Prometheus or no access."""
    from ..alerts import automation_principal
    from ..connectors import registry as connectors
    from ..rbac import P
    from ..scope import ScopeError, effective_scope
    from ..tools.contracts import ToolContext
    from ..tools.executor import ToolExecutor

    principal = await automation_principal(db)
    try:
        scope = effective_scope(principal, env, P.INVESTIGATION_RUN)
    except ScopeError:
        return None
    found, _ = await connectors.for_environment(db, env.id)
    if "prometheus" not in found:
        return None

    async def query(promql: str, a: datetime, b: datetime, step: int) -> tuple[list[dict], bool]:
        if a == b:  # an instant read at b (the sampling probe)
            ctx = ToolContext(None, env, scope, principal, b - timedelta(hours=1), b, found, timedelta(hours=1, minutes=1))
        else:  # the range exactly as asked: the window's start is the first grid timestamp
            ctx = ToolContext(None, env, scope, principal, a, b, found, b - a + timedelta(minutes=1))
        ex = ToolExecutor(None, ctx, timeout_seconds=30, read_permissions=(P.INVESTIGATION_RUN,), audit_success=False)
        args = {"promql": promql, "mode": "instant"} if a == b else {"promql": promql, "mode": "range", "step": step}
        out = await ex.run("prom_query", args)
        if not out.ok:  # an HTTP error, a timeout, or a Prometheus status other than success
            raise RuntimeError(out.error_kind or "query failed")
        data = out.data or {}
        series = data.get("series") or []
        if a == b:  # an instant answer, shaped like a one-point range answer
            series = [{"metric": x.get("metric") or {}, "values": [x["value"]] if x.get("value") else (x.get("values") or [])[-1:]} for x in series]
        # Warnings (a partial response from a remote store, say) and a truncated result are both incomplete.
        return series, not out.truncated and not data.get("warnings")
    return query


def metric_names() -> dict[str, str]:
    s = get_settings()
    return {"requests": s.ml_anomaly_request_metric, "duration": s.ml_anomaly_duration_metric, "queue_depth": s.ml_anomaly_queue_depth_metric,
            "db_connections": s.ml_anomaly_db_connections_metric}


# ---- frames ------------------------------------------------------------------------------------------------


def window(frame: CohortFrame, a: int, b: int) -> CohortFrame:
    return frame.slice(a, b)


def drift_bins(frame: CohortFrame) -> dict[str, dict]:
    out = {}
    for f in frame.features:
        edges, props = evaluation.bins([v for v in frame.values[f] if v is not None])
        if edges:
            out[f] = {"edges": edges, "props": props}
    return out


def snapshot_frame(frame: CohortFrame) -> tuple[dict, bytes, str]:
    """The canonical training data: (document, bytes, sha256). A model's dataset version names these bytes."""
    doc = {**frame.snapshot(), "usable": features.usable(frame), "undersampled": list(frame.quality.get("undersampled") or [])}
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return doc, body, hashlib.sha256(body).hexdigest()


def frame_from_snapshot(doc: dict) -> CohortFrame:
    fr = features.from_snapshot(doc)
    n = len(fr.timestamps)
    fr.quality = {"points": n, "coverage": {f: (1.0 if f in doc.get("usable", []) else 0.0) for f in fr.features}, "undersampled": list(doc.get("undersampled") or [])}
    return fr


def state_sha256(state: Any) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def save_snapshot(db: AsyncSession, body: bytes, sha: str) -> None:
    s = get_settings()
    if len(body) > s.ml_artifact_max_bytes:
        raise artifacts.ArtifactError(f"training snapshot is {len(body)} bytes, above ml_artifact_max_bytes")
    if await db.get(MLArtifact, sha) is None:
        db.add(MLArtifact(sha256=sha, media_type=SNAPSHOT_MEDIA_TYPE, body=body, uri=None, size=len(body)))
        await db.flush()


async def load_snapshot(db: AsyncSession, sha: str) -> dict:
    row = await db.get(MLArtifact, sha)
    if row is None or row.media_type != SNAPSHOT_MEDIA_TYPE or row.body is None:
        raise artifacts.ArtifactError("training snapshot not found")
    body = bytes(row.body)
    if hashlib.sha256(body).hexdigest() != sha:
        raise artifacts.ArtifactError("training snapshot checksum mismatch")
    return json.loads(body)


# ---- scoring helpers -----------------------------------------------------------------------------------------


def anomaly_type(algorithm: str, frame: CohortFrame) -> str:
    if frame.namespace == NODE_NAMESPACE:
        return "infrastructure"
    return "multivariate" if detectors.ALGORITHMS[algorithm].kind == "multivariate" else "service"


def score_window(algorithm: str, state: dict, frame: CohortFrame, start: int | None = None, persist: int | None = None) -> dict[str, Any]:
    """Scores frame[start:] (the last window by default) with frame[:start] as history."""
    return detectors.score(algorithm, state, frame, start=0 if start is None else start, persist=persist)


def _ticks(start: int, end: int, s) -> list[tuple[int, int]]:
    pts = max(1, s.ml_anomaly_window_minutes * 60 // s.ml_anomaly_step_seconds)
    stride = max(1, s.ml_anomaly_interval_minutes * 60 // s.ml_anomaly_step_seconds)
    return benchmark.windows(start, end, pts, stride)


async def _incidents(db: AsyncSession, frame: CohortFrame, a: datetime, b: datetime) -> list[dict]:
    """Approved incidents of this workload (or node) that started in [a, b): the only labels anomaly work uses."""
    q = select(LearningRecord).where(LearningRecord.kind == "incident", LearningRecord.label_state == "approved", LearningRecord.realm == "operational",
                                     LearningRecord.environment == frame.environment, LearningRecord.event_from >= a, LearningRecord.event_from < b)
    if frame.namespace != NODE_NAMESPACE:
        q = q.where(LearningRecord.namespace == frame.namespace)
    rows = (await db.execute(q)).scalars().all()
    mine = lambda r: frame.workload in ((r.features or {}).get("services") or []) or r.service == frame.workload or str(r.resource or "").lower().endswith(f"/{frame.workload}")
    return [{"cohort": frame.cohort, "start": r.event_from, "end": r.event_to} for r in rows if mine(r)]


def _mask(frame: CohortFrame, incidents: list[dict]) -> int:
    """Approved incidents inside the training period become gaps (30 minutes before to 2 hours after when no end is
    recorded), so a model never learns a known incident as normal. Returns the points masked."""
    masked = 0
    for inc in incidents:
        a = int(inc["start"].timestamp()) - 1800
        b = int((inc["end"] or inc["start"] + timedelta(hours=2)).timestamp())
        for i, t in enumerate(frame.timestamps):
            if a <= t <= b:
                for f in frame.features:
                    if frame.values[f][i] is not None:
                        frame.values[f][i] = None
                        masked += 1
    return masked


# ---- training -----------------------------------------------------------------------------------------------


async def train(env_name: str, namespace: str | None, *, query: features.Query | None = None, now: datetime | None = None, created_by: uuid.UUID | None = None,
                job_id: str | None = None, metrics: dict[str, str] | None = None, algorithms: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Trains every algorithm for every workload in `namespace`, or every node when namespace is None or "_node"."""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=s.ml_anomaly_training_days)
    nodes = namespace in (None, NODE_NAMESPACE)
    async with session_scope() as db:
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one_or_none()
        if env is None:
            raise ValueError(f"no environment {env_name}")
        query = query or await prometheus(db, env)
    label = NODE_NAMESPACE if nodes else namespace
    if query is None:
        return {"environment": env_name, "namespace": label, "status": "no_prometheus", "cohorts": []}
    try:
        coll = await features.collect_raw(query, env_name, None if nodes else namespace, start, now, s.ml_anomaly_step_seconds, metrics if metrics is not None else metric_names(),
                                          rate_window=s.ml_anomaly_rate_window_seconds)
    except features.WindowError as exc:
        return {"environment": env_name, "namespace": label, "status": exc.status, "reason": exc.reason, "cohorts": []}
    frames = features.frames_of(coll)[: s.ml_anomaly_max_cohorts]
    results = [await _train_cohort(fr, s, created_by, job_id, algorithms or ALGORITHMS) for fr in frames]
    return {"environment": env_name, "namespace": label, "status": "ok", "window": [start.isoformat(), now.isoformat()], "probes": coll.probes, "cohorts": results,
            "note": "Candidates only. Promotion is a separate, gated, audited decision."}


async def _train_cohort(fr: CohortFrame, s, created_by, job_id, algorithms: tuple[str, ...]) -> dict[str, Any]:
    hist = features.history_days(fr)
    usable = features.usable(fr)
    base = {"cohort": fr.cohort, "level": fr.level, "history_days": round(hist, 2), "quality": fr.quality}
    if hist < s.ml_anomaly_min_history_days:
        return {**base, "status": "insufficient_history", "reason": f"{hist:.1f} days of usable history; {s.ml_anomaly_min_history_days} needed"}
    if len(usable) < 2 and fr.level != "infrastructure":
        return {**base, "status": "insufficient_features", "reason": "fewer than two features have half coverage; absent telemetry is not modeled as healthy"}
    if not usable:
        return {**base, "status": "insufficient_features", "reason": "no feature has half coverage"}
    n = len(fr.timestamps)
    cut_i = int(n * TRAIN_SHARE)
    embargo = max(1, -(-max(s.ml_anomaly_rate_window_seconds, s.ml_anomaly_step_seconds) // s.ml_anomaly_step_seconds))
    cut = fr.timestamps[cut_i]
    a, b = datetime.fromtimestamp(cut, timezone.utc), datetime.fromtimestamp(fr.timestamps[-1] + fr.step, timezone.utc)
    async with session_scope() as db:
        train_incidents = await _incidents(db, fr, datetime.fromtimestamp(fr.timestamps[0], timezone.utc) - timedelta(hours=2), a)
        incidents = await _incidents(db, fr, a, b)
        champ = await registry.champion(db, CAPABILITY, fr.cohort)
    full = replace(fr, values={f: list(v) for f, v in fr.values.items()})
    train_fr = full.slice(fr.timestamps[0], cut)
    train_fr.quality = fr.quality
    masked = _mask(train_fr, train_incidents)
    for f in fr.features:  # the masked training values are what history means after the cut, too
        full.values[f][:cut_i] = train_fr.values[f]
    doc, body, sha = snapshot_frame(train_fr)
    fit_fr = frame_from_snapshot(json.loads(body))  # fitted from the decoded snapshot: exactly what a reproduction reads
    eval_start = cut_i + embargo
    valid_days = max((n - eval_start) * fr.step / 86400, 1e-9)
    rng = random.Random(int(hashlib.sha256(fr.cohort.encode()).hexdigest()[:8], 16))
    injected, events = benchmark.inject(full, eval_start, rng)
    window_points = max(1, s.ml_anomaly_window_minutes * 60 // s.ml_anomaly_step_seconds)
    stride = max(1, s.ml_anomaly_interval_minutes * 60 // s.ml_anomaly_step_seconds)
    tol = timedelta(seconds=benchmark.TOLERANCE_POINTS * fr.step)
    drift = drift_bins(train_fr)
    out = {**base, "status": "trained", "cutoff": a.isoformat(), "embargo_points": embargo, "masked_points": masked, "dataset_sha256": sha, "models": []}
    try:
        async with session_scope() as db:
            await save_snapshot(db, body, sha)
    except artifacts.ArtifactError as exc:  # without a frozen snapshot a model could not be reproduced, so none is registered
        return {**base, "status": "snapshot_too_large", "reason": f"{exc}; shorten ml.anomaly.trainingDays or raise MOKS_ML_ARTIFACT_MAX_BYTES"}

    def judge(alg: str, state: dict) -> dict[str, Any]:
        flags = benchmark.flag_windows(alg, state, full, eval_start, window_points=window_points, stride=stride)
        obs = [{"cohort": fr.cohort, "window_to": datetime.fromtimestamp(full.timestamps[w[1] - 1] + fr.step, timezone.utc), "unusual": bool(w[2]["unusual"]), "score": w[2]["score"]}
               for w in flags]
        inj = benchmark.combine([benchmark.score_events(benchmark.flag_windows(alg, state, injected, eval_start, window_points=window_points, stride=stride), events, fr.step)])
        alarms = len(benchmark._alarms(flags))
        return {"validation": {"windows": len(obs), "flagged_windows": sum(o["unusual"] for o in obs), "alarms": alarms, "days": round(valid_days, 3),
                               "flags_per_day": round(alarms / valid_days, 3),
                               "note": "alarms (runs of consecutive flagged ticks) per day on unreviewed history: an upper bound on false positives only if that period was healthy"},
                "detection": evaluation.detection(obs, incidents, tol), "synthetic": {k: inj[k] for k in ("faults", "detected", "recall", "precision", "f1", "median_delay_seconds", "alarms")},
                "latency_ms": benchmark.latency_ms(alg, state, full, eval_start, window_points=window_points)}

    for alg in algorithms:
        try:
            state = detectors.fit(alg, fit_fr, seed=0, window_points=window_points, persist=s.ml_anomaly_persist_points)
        except ValueError as exc:
            out["models"].append({"algorithm": alg, "status": "not_trained", "reason": str(exc)})
            continue
        digest = state_sha256(state)
        again = state_sha256(detectors.fit(alg, frame_from_snapshot(json.loads(body)), seed=0, window_points=window_points, persist=s.ml_anomaly_persist_points))
        metrics = judge(alg, state)
        if champ is not None and champ.feature_schema == FEATURE_SCHEMA:  # the same windows and faults, so the comparison is like for like
            try:
                cdoc = await _load(champ)
                cm = judge(champ.algorithm, cdoc["state"])
                metrics["champion_comparison"] = {"champion": str(champ.id), "champion_algorithm": champ.algorithm, "champion_version": champ.version,
                                                  "champion_flags_per_day": cm["validation"]["flags_per_day"], "champion_detection": cm["detection"], "champion_synthetic": cm["synthetic"]}
            except artifacts.ArtifactError as exc:
                metrics["champion_comparison"] = {"champion": str(champ.id), "error": str(exc)}
        syn = metrics["synthetic"]
        det = metrics["detection"]
        metrics["gates"] = [
            {"name": "history", "passed": hist >= s.ml_anomaly_min_history_days, "value": round(hist, 2)},
            {"name": "validation_windows", "passed": metrics["validation"]["windows"] >= MIN_VALIDATION_WINDOWS, "value": metrics["validation"]["windows"]},
            {"name": "flags_per_day", "passed": metrics["validation"]["flags_per_day"] <= s.ml_anomaly_max_flags_per_day, "value": metrics["validation"]["flags_per_day"],
             "limit": s.ml_anomaly_max_flags_per_day},
            {"name": "feature_coverage", "passed": len(state.get("features") or []) >= (1 if fr.level == "infrastructure" else 2), "value": sorted(state.get("features") or [])},
            {"name": "synthetic_recall", "passed": syn["faults"] > 0 and (syn["recall"] or 0) >= s.ml_anomaly_min_synthetic_recall, "value": syn["recall"],
             "limit": s.ml_anomaly_min_synthetic_recall, "evidence": f"{syn['faults']} faults injected into this workload's validation data"},
            {"name": "detection_delay", "passed": syn["median_delay_seconds"] is not None and syn["median_delay_seconds"] <= s.ml_anomaly_max_detection_delay_minutes * 60,
             "value": syn["median_delay_seconds"], "limit": s.ml_anomaly_max_detection_delay_minutes * 60},
            {"name": "reproducible", "passed": digest == again, "value": digest[:12], "evidence": "refitted from the stored snapshot"},
        ]
        if det.get("incidents", 0) >= 3:  # real labels only gate once there are enough of them to mean something
            metrics["gates"].append({"name": "incident_recall", "passed": (det.get("recall") or 0) >= 0.5, "value": det.get("recall"), "evidence": f"{det['incidents']} approved incidents"})
        pins = {"traffic": (fr.quality.get("sources") or {}).get("traffic")} if fr.level != "infrastructure" else {}
        params = {"drift": drift, "pins": pins}
        body_a, sha_a = artifacts.encode(algorithm=alg, cohort=fr.cohort, feature_schema=FEATURE_SCHEMA, features=list(fr.features), step=fr.step, state=state, params=params)
        frm = datetime.fromtimestamp(fr.timestamps[0], timezone.utc)
        async with session_scope() as db:
            m = await registry.register(
                db, capability=CAPABILITY, cohort=fr.cohort, algorithm=alg, feature_schema=FEATURE_SCHEMA, body=body_a, sha=sha_a,
                params={"window_minutes": s.ml_anomaly_window_minutes, "step_seconds": fr.step, "rate_window_seconds": max(s.ml_anomaly_rate_window_seconds, fr.step),
                        "persist": s.ml_anomaly_persist_points, "pins": pins, "catalog": (fr.provenance or {}).get("catalog") or features.catalog_digest(),
                        "history_points": detectors.history_points(alg, state), "kind": detectors.ALGORITHMS[alg].kind, "level": fr.level},
                metrics=metrics,
                training={"from": frm.isoformat(), "cutoff": a.isoformat(), "to": b.isoformat(), "history_days": round(hist, 2), "train_points": len(train_fr.timestamps),
                          "validation_points": n - eval_start, "embargo_points": embargo, "masked_points": masked, "masked_incidents": len(train_incidents),
                          "snapshot_sha256": sha, "state_sha256": digest, "seed": 0, "quality": fr.quality, "provenance": fr.provenance},
                created_by=created_by, job_id=job_id, dataset_version=f"prometheus#{sha[:16]}@{frm.date()}..{a.date()}")
            out["models"].append({"algorithm": alg, "status": "candidate", "id": str(m.id), "version": m.version, "gates_passed": registry.gates_passed(m)[0], "metrics": metrics})
    ranked = sorted((x for x in out["models"] if x["status"] == "candidate"),
                    key=lambda x: (not x["gates_passed"], -((x["metrics"]["synthetic"].get("f1")) or 0), x["metrics"]["validation"]["flags_per_day"]))
    out["recommended"] = {"algorithm": ranked[0]["algorithm"], "id": ranked[0]["id"], "gates_passed": ranked[0]["gates_passed"],
                          "why": "passes its gates with the best F1 on the injected faults, then the fewest flags per day"} if ranked else None
    return out


async def _load(m) -> dict:
    async with session_scope() as db:
        feats = NODE_FEATURES if m.cohort.split("/", 2)[1] == NODE_NAMESPACE else FEATURES
        return await artifacts.load(db, m.artifact_sha256, algorithm=m.algorithm, feature_schema=FEATURE_SCHEMA, features=list(feats), step=get_settings().ml_anomaly_step_seconds)


async def reproduce(model_id: uuid.UUID) -> dict[str, Any]:
    """Refits a model from its stored training snapshot and compares the state with the one it was registered with."""
    from ..models import MLModel

    async with session_scope() as db:
        m = await db.get(MLModel, model_id)
        if m is None:
            raise ValueError("model not found")
        tr = m.training or {}
        if not tr.get("snapshot_sha256"):
            return {"model": str(m.id), "reproducible": False, "reason": "trained before snapshots were kept"}
        doc = await load_snapshot(db, tr["snapshot_sha256"])
        p = m.params or {}
    state = detectors.fit(m.algorithm, frame_from_snapshot(doc), seed=int(tr.get("seed") or 0),
                          window_points=max(1, int(p.get("window_minutes", 60)) * 60 // int(p.get("step_seconds", 300))), persist=int(p.get("persist") or detectors.PERSIST))
    got = state_sha256(state)
    return {"model": str(m.id), "algorithm": m.algorithm, "version": m.version, "snapshot_sha256": tr["snapshot_sha256"], "expected_state_sha256": tr.get("state_sha256"),
            "state_sha256": got, "reproducible": got == tr.get("state_sha256")}


# ---- inference ----------------------------------------------------------------------------------------------


def _split_cohort(cohort: str) -> tuple[str, str, str]:
    env, ns, wl = cohort.split("/", 2)
    return env, ns, wl


def _fingerprint(model_id: uuid.UUID, window_to: datetime) -> str:
    return hashlib.sha256(f"{model_id}:{int(window_to.timestamp())}".encode()).hexdigest()


async def _maintenance(db: AsyncSession, env_id: uuid.UUID, namespace: str, now: datetime) -> dict | None:
    rows = (await db.execute(select(MaintenanceWindow).where(MaintenanceWindow.environment_id == env_id, MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now))).scalars().all()
    hit = next((w for w in rows if w.namespace in ("*", namespace) and w.rule_id in ("*", "ml", CAPABILITY)), None)
    return {"reason": hit.reason[:200], "ends_at": hit.ends_at.isoformat()} if hit else None


async def judged(db: AsyncSession, model_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """Reviewed operator verdicts on each model's observations: what validation status rests on."""
    if not model_ids:
        return {}
    rows = (await db.execute(select(MLObservation.model_id, LearningJudgment.verdict, func.count())
                             .join(LearningJudgment, LearningJudgment.target_id == cast(MLObservation.id, String))
                             .where(LearningJudgment.target_kind == "anomaly_observation", LearningJudgment.state == "reviewed", MLObservation.model_id.in_(model_ids))
                             .group_by(MLObservation.model_id, LearningJudgment.verdict))).all()
    out: dict[uuid.UUID, dict[str, int]] = {}
    for mid, verdict, n in rows:
        out.setdefault(mid, {})[verdict] = n
    return out


def validation_status(m, verdicts: dict[str, int]) -> dict[str, Any]:
    """What is known about this model's flags: gates at training, then reviewed verdicts in shadow."""
    tp, fp = verdicts.get("true_positive", 0), verdicts.get("false_positive", 0) + verdicts.get("expected", 0)
    n = tp + fp
    precision = round(tp / n, 3) if n else None
    if n < 5:
        state = "unvalidated"
    elif n < 20:
        state = "partially_validated"
    else:
        state = "validated" if (precision or 0) >= 0.7 else "failed_validation"
    syn = ((m.metrics or {}).get("synthetic") or {})
    return {"status": state, "mode": "shadow", "gates_passed": registry.gates_passed(m)[0], "reviewed_flags": n, "judged_precision": precision,
            "synthetic_recall": syn.get("recall"), "role": "champion" if m.status == "champion" else "challenger",
            "note": "shadow output: never an incident, a severity or an action, whatever its status"}


def confidence(result: dict, quality: dict, validation: dict, agreement: int) -> dict[str, Any]:
    """low | medium | high, with the reasons. A ranking of how much weight the flag deserves as a lead, not a probability."""
    reasons = []
    if not result.get("unusual"):
        return {"level": "none", "reasons": ["not flagged"]}
    margin = (result.get("score") or 0) / (result.get("threshold") or 1)
    flagged = result.get("flagged_features") or []
    cov = min((quality.get("coverage", {}).get(f, 0) for f in flagged), default=0) if flagged else 1.0
    level = "medium" if margin >= 1.5 and cov >= 0.8 else "low"
    reasons.append(f"{margin:.1f}x its threshold")
    reasons.append(f"flagged features {round(cov * 100)}% covered" if flagged else "multivariate score")
    if agreement:
        reasons.append(f"{agreement} other model(s) flagged the same window")
    drift = max((quality.get("drift_psi") or {}).values(), default=0)
    incomplete = set(quality.get("incomplete_queries") or [])
    if level == "medium" and margin >= 2 and agreement >= 1 and validation["status"] == "validated":
        level = "high"
    if drift > 0.25:
        level = "low"
        reasons.append(f"input drift (PSI {drift:.2f}) since training")
    if incomplete:
        level = "low"
        reasons.append("some queries were incomplete")
    if validation["status"] in ("unvalidated", "failed_validation"):
        reasons.append(f"model {validation['status'].replace('_', ' ')}")
        if validation["status"] == "failed_validation":
            level = "low"
    return {"level": level, "reasons": reasons}


async def tick(env_name: str, *, query: features.Query | None = None, now: datetime | None = None, correlate_after: bool = True) -> dict[str, Any]:
    """Scores every active model of one environment once, then regroups its anomalies. Shadow output only."""
    from . import mode

    s = get_settings()
    now = now or datetime.now(timezone.utc)
    async with session_scope() as db:
        state = await mode(db, CAPABILITY)
        if state == "disabled":
            return {"skipped": "anomaly_detection is disabled"}
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one_or_none()
        if env is None:
            return {"skipped": f"no environment {env_name}"}
        models = (await registry.active(db, CAPABILITY, prefix=f"{env_name}/"))[: s.ml_anomaly_max_cohorts * len(ALGORITHMS)]
        query = query or await prometheus(db, env)
        env_id = env.id
        verdicts = await judged(db, [m.id for m in models])
    if not models:
        return {"skipped": "no champion or challenger for this environment", "observations": 0}
    if query is None:
        ML_FALLBACKS.labels(CAPABILITY, "no_prometheus").inc()
        return {"skipped": "no Prometheus access for this environment"}
    by_ns: dict[str, list] = {}
    for m in models:
        by_ns.setdefault(_split_cohort(m.cohort)[1], []).append(m)
    gate = asyncio.Semaphore(s.ml_anomaly_concurrency)
    start = now - timedelta(minutes=s.ml_anomaly_window_minutes)
    win = features.align_window(start, now, s.ml_anomaly_step_seconds)
    if win.status != "ok":
        return {"skipped": win.reason, "status": win.status, "observations": 0}

    async def one_namespace(ns: str, ms: list) -> list[dict]:
        async with gate:
            current = [m for m in ms if m.feature_schema == FEATURE_SCHEMA]
            hist = max([int((m.params or {}).get("history_points") or 0) for m in current] + [0])
            lookback = min(hist * s.ml_anomaly_step_seconds, s.ml_anomaly_lookback_hours * 3600)
            coll = None
            try:
                if current:
                    coll = await features.collect_raw(query, env_name, None if ns == NODE_NAMESPACE else ns, start - timedelta(seconds=lookback), now, s.ml_anomaly_step_seconds,
                                                      metric_names(), rate_window=s.ml_anomaly_rate_window_seconds)
            except Exception as exc:
                log.warning("anomaly features unavailable", extra={"event": "ml_features_failed", "namespace": ns, "error_type": type(exc).__name__})
            async with session_scope() as db:
                maint = await _maintenance(db, env_id, ns, now)
            return [await _observe(m, coll, maint, state, start, now, win, verdicts.get(m.id, {})) for m in ms]

    rows = [r for batch in await asyncio.gather(*(one_namespace(ns, ms) for ns, ms in by_ns.items())) for r in batch]
    flagged: dict[str, int] = {}
    for r in rows:
        if r["unusual"]:
            flagged[r["cohort"]] = flagged.get(r["cohort"], 0) + 1
    for r in rows:  # agreement: the other models of this cohort that flagged the same window
        if r["unusual"]:
            agree = flagged[r["cohort"]] - 1
            r["context"]["agreement"] = agree
            r["context"]["confidence"] = confidence({"unusual": True, "score": r["score"], "threshold": r["threshold"], "flagged_features": r["context"].get("flagged_features")},
                                                    r["data_quality"], r["context"]["validation"], agree)
    async with session_scope() as db:
        for r in rows:
            await db.execute(insert(MLObservation).values(**r).on_conflict_do_nothing(index_elements=["fingerprint"]))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        ML_OBSERVATIONS.labels(CAPABILITY, r["status"]).inc()
        if r["unusual"]:
            ML_FLAGS.labels(r["algorithm"], r["context"].get("level", "service"), r["context"].get("role", "champion")).inc()
    out: dict[str, Any] = {"observations": len(rows), "by_status": counts, "mode": state, "note": NOTE}
    if time.monotonic() - _health_at.get(env_name, -1e9) >= 3600:  # champions' health, at most hourly, for moks_ml_model_degraded
        _health_at[env_name] = time.monotonic()
        try:
            async with session_scope() as db:
                out["review"] = [h["model"]["id"] for m in models if m.status == "champion" and not (h := await health(db, m))["healthy"]]
        except Exception as exc:
            log.warning("model health failed", extra={"event": "ml_health_failed", "error_type": type(exc).__name__})
    if correlate_after:
        from . import correlate

        try:
            out["correlation"] = await correlate.run(env_name, now=now)
        except Exception as exc:  # grouping is a view over the observations; it never takes them down with it
            log.warning("correlation failed", extra={"event": "ml_correlation_failed", "error_type": type(exc).__name__})
            out["correlation"] = {"error": type(exc).__name__}
    return out


def _compact_provenance(frame: CohortFrame) -> dict[str, Any]:
    p = frame.provenance or {}
    return {"schema": p.get("schema"), "catalog": p.get("catalog"), "rate_window_seconds": p.get("rate_window_seconds"),
            "features": {f: {k: v.get(k) for k in ("source", "native_interval_seconds", "coverage")} for f, v in (p.get("features") or {}).items()}}


async def _observe(m, coll: features.Collection | None, maint: dict | None, state: str, start: datetime, now: datetime, win: features.Window, verdicts: dict) -> dict[str, Any]:
    env, ns, wl = _split_cohort(m.cohort)
    p = m.params or {}
    validation = validation_status(m, verdicts)
    row = {"id": uuid.uuid4(), "capability": CAPABILITY, "model_id": m.id, "algorithm": m.algorithm, "environment": env, "namespace": ns, "service": wl, "cohort": m.cohort,
           "window_from": start, "window_to": now, "mode": state, "fingerprint": _fingerprint(m.id, now),
           "context": {"role": "champion" if m.status == "champion" else "challenger", "model_version": m.version, "maintenance": maint, "note": NOTE,
                       "level": p.get("level") or ("infrastructure" if ns == NODE_NAMESPACE else "service"), "validation": validation, "schema": m.feature_schema},
           "score": None, "threshold": None, "unusual": False, "deviations": [], "data_quality": {}}
    if m.feature_schema != FEATURE_SCHEMA:
        ML_FALLBACKS.labels(CAPABILITY, "stale_schema").inc()
        return {**row, "status": "stale_schema", "data_quality": {"error": f"trained on {m.feature_schema}; this build scores {FEATURE_SCHEMA}. Retrain the cohort."}}
    frames = features.frames_of(coll, pins={wl: p.get("pins") or {}}, workloads={wl}) if coll is not None else []
    frame = frames[0] if frames else None
    if frame is not None and p.get("catalog") and (frame.provenance or {}).get("catalog") != p.get("catalog"):
        ML_FALLBACKS.labels(CAPABILITY, "stale_schema").inc()
        return {**row, "status": "stale_schema", "data_quality": {"error": "the feature catalog changed since training (metric names or query templates). Retrain the cohort."}}
    wstart = next((i for i, t in enumerate(frame.timestamps) if t >= win.start), len(frame.timestamps)) if frame else 0
    wq: dict[str, Any] = {}
    if frame is not None:  # data quality is judged on the scored window, not on the history before it
        n = len(frame.timestamps) - wstart
        wq = {**frame.quality, "points": n, "coverage": {f: round(sum(v is not None for v in frame.values[f][wstart:]) / n, 3) if n else 0.0 for f in frame.features}}
    if frame is None or not any(wq["coverage"].get(f, 0) >= 0.2 for f in frame.features):
        ML_FALLBACKS.labels(CAPABILITY, "no_telemetry").inc()
        return {**row, "status": "no_telemetry", "data_quality": wq if frame else {"absent": list(NODE_FEATURES if ns == NODE_NAMESPACE else FEATURES)}}
    for f in frame.features:
        if f in wq.get("absent", []) or wq["coverage"].get(f, 0) < 0.5:
            ML_MISSING_FEATURES.labels(f).inc()
    if (m.training or {}).get("history_days", 0) < get_settings().ml_anomaly_min_history_days:
        return {**row, "status": "insufficient_history", "data_quality": wq}
    began = time.monotonic()
    try:
        doc = await _load(m)
    except artifacts.ArtifactError as exc:
        ML_FALLBACKS.labels(CAPABILITY, "model_invalid").inc()
        return {**row, "status": "model_invalid", "data_quality": {**wq, "error": str(exc)[:300]}}
    r = score_window(m.algorithm, doc["state"], frame, start=wstart, persist=p.get("persist"))
    ML_INFERENCE_SECONDS.labels(CAPABILITY, m.algorithm).observe(time.monotonic() - began)
    drift = {}
    for f, b in ((doc.get("params") or {}).get("drift") or {}).items():
        psi = evaluation.psi(b["props"], b["edges"], [v for v in frame.values.get(f, [])[wstart:] if v is not None])
        if psi is not None:
            drift[f] = psi
            ML_DRIFT.labels(row["cohort"], f).set(psi)
    quality = {**wq, "drift_psi": drift, "latency_ms": round((time.monotonic() - began) * 1000, 2), "provenance": _compact_provenance(frame),
               "history_points": wstart}
    row["context"].update({"anomaly_type": anomaly_type(m.algorithm, frame), "first_flagged_at": r.get("first_flagged_at"), "flagged_features": r.get("flagged_features") or [],
                           "explanation": r.get("explanation") or "features whose deviation from this workload's baseline stayed past their threshold"})
    if maint:  # scored for the record, never flagged: maintenance is expected to look unusual
        return {**row, "status": "maintenance", "score": r["score"], "threshold": r["threshold"], "unusual": False, "deviations": r["deviations"], "data_quality": quality}
    row["context"]["confidence"] = confidence(r, quality, validation, 0)
    return {**row, "status": "scored", "score": r["score"], "threshold": r["threshold"], "unusual": bool(r["unusual"]), "deviations": r["deviations"], "data_quality": quality}


# ---- health ---------------------------------------------------------------------------------------------------


async def health(db: AsyncSession, m, days: int = 7) -> dict[str, Any]:
    """How a champion or challenger is doing in shadow: flag rate against its gate, input drift, judged precision,
    fallbacks and latency. Recommends a review; never rolls back on its own."""
    s = get_settings()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(select(MLObservation).where(MLObservation.model_id == m.id, MLObservation.observed_at >= since))).scalars().all()
    scored = [o for o in rows if o.status == "scored"]
    statuses: dict[str, int] = {}
    for o in rows:
        statuses[o.status] = statuses.get(o.status, 0) + 1
    flags_per_day = None
    if scored:  # alarms, as in training: runs of consecutive flagged ticks, since overlapping windows flag one change several times
        scored.sort(key=lambda o: o.window_to)
        span_days = max((scored[-1].window_to - scored[0].window_to).total_seconds() / 86400, s.ml_anomaly_interval_minutes / 1440)
        alarms = sum(1 for i, o in enumerate(scored) if o.unusual and (i == 0 or not scored[i - 1].unusual))
        flags_per_day = round(alarms / span_days, 3)
    psi: dict[str, list[float]] = {}
    for o in scored:
        for f, v in ((o.data_quality or {}).get("drift_psi") or {}).items():
            psi.setdefault(f, []).append(v)
    drift = {f: round(statistics.median(v), 4) for f, v in psi.items()}
    verdicts = (await judged(db, [m.id])).get(m.id, {})
    validation = validation_status(m, verdicts)
    lat = [float((o.data_quality or {}).get("latency_ms")) for o in scored if (o.data_quality or {}).get("latency_ms") is not None]
    recs = []
    if m.feature_schema != FEATURE_SCHEMA:
        recs.append({"reason": "stale_schema", "action": "retrain", "detail": f"trained on {m.feature_schema}"})
    if flags_per_day is not None and flags_per_day > s.ml_anomaly_max_flags_per_day:
        recs.append({"reason": "flag_rate", "action": "review", "detail": f"{flags_per_day} flags per day against a gate of {s.ml_anomaly_max_flags_per_day}"})
    drifted = sorted(f for f, v in drift.items() if v > 0.25)
    if drifted:
        recs.append({"reason": "drift", "action": "retrain", "detail": f"median PSI above 0.25 for {', '.join(drifted)}"})
    if validation["status"] == "failed_validation":
        recs.append({"reason": "judged_precision", "action": "roll back or retire", "detail": f"judged precision {validation['judged_precision']} on {validation['reviewed_flags']} reviewed flags"})
    if rows and statuses.get("no_telemetry", 0) > len(rows) / 2:
        recs.append({"reason": "telemetry", "action": "check the sources", "detail": "more than half its windows had no telemetry"})
    for reason in ("flag_rate", "drift", "judged_precision", "stale_schema", "telemetry"):
        ML_MODEL_HEALTH.labels(m.algorithm, reason).set(1 if any(r["reason"] == reason for r in recs) and m.status == "champion" else 0)
    return {"model": registry.model_out(m), "days": days, "observations": len(rows), "statuses": statuses, "flags_per_day": flags_per_day,
            "flag_gate": s.ml_anomaly_max_flags_per_day, "drift_psi": drift, "validation": validation,
            "latency_ms_median": round(statistics.median(lat), 2) if lat else None, "recommendations": recs, "healthy": not recs,
            "note": "Recommendations only. Rolling back is an explicit, audited decision."}


def observation_out(o: MLObservation) -> dict[str, Any]:
    return {"id": str(o.id), "model_id": str(o.model_id) if o.model_id else None, "algorithm": o.algorithm, "environment": o.environment, "namespace": o.namespace, "service": o.service,
            "cohort": o.cohort, "window_from": o.window_from.isoformat(), "window_to": o.window_to.isoformat(), "observed_at": o.observed_at.isoformat() if o.observed_at else None,
            "status": o.status, "mode": o.mode, "score": o.score, "threshold": o.threshold, "unusual": o.unusual, "deviations": o.deviations, "data_quality": o.data_quality,
            "context": o.context, "feedback": o.feedback}
