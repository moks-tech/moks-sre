"""Incident correlation: anomalies that belong together become one group, so one problem is one investigation.

An anomaly here is one cohort's run of flagged observations (champion models only), with when it was first
flagged and which features moved. Two anomalies are linked when they are close in time (first flagged within
`proximity`, 30 minutes by default) and something structural ties them. Each tie has a weight:

  calls               one calls the other (Istio sidecar telemetry, the request-level relationships a trace
                      would show)                                                                       0.6
  calls_via           they are two calls apart                                                          0.35
  shared_dependency   both call the same service                                                        0.3
  node                one is a node and the other runs on it (pod placement from Kubernetes)            0.6
  same_node           both run on the same node                                                         0.1
  rollout             a rollout of either, or of something they call, in the hour before                0.3
  same_namespace                                                                                        0.15
  plus up to 0.2 for being close in time.

A link needs 0.5, so a shared namespace and good timing alone do not join two anomalies. Linked anomalies form a
group (connected components). Each group then gets candidate explanations, ranked by how many members they
account for: a node problem, a rollout, the earliest anomaly among the services the others depend on, or a shared
dependency that is not itself flagged. Every candidate is labeled unvalidated. A group is a lead for an
investigation; it never becomes a confirmed cause, opens an incident or runs an action.

Context comes through the existing connectors and the executor, as the automation principal: the mesh call graph
(prom_query), pods and their nodes and owners (k8s_list_pods), Deployment revisions (k8s_rollout_history),
warning events (k8s_events), error-log volume (loki_query) and alert history (alert_events). A source that fails
or is not configured is listed in the group's gaps and the rest still works.

Similar historical incidents are not stored in a group: they depend on who is reading, so the API and the crew
tool look them up at read time with the reader's own access.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .features import NODE_NAMESPACE, WORKLOAD_REGEX

log = logging.getLogger("moks.ml.correlate")

ENGINE = "correlate/1"
PROXIMITY = 1800
LINK_THRESHOLD = 0.5
ROLLOUT_LOOKBACK = 3600
WEIGHTS = {"calls": 0.6, "calls_via": 0.35, "shared_dependency": 0.3, "node": 0.6, "same_node": 0.1, "rollout": 0.3, "same_namespace": 0.15, "time": 0.2}
NOTE = ("A correlation group says these anomalies probably belong to one problem. Its candidate explanations are "
        "unvalidated leads for an investigation: confirm them with live evidence before acting on any of them.")

Ref = tuple[str, str]  # (namespace, workload)


@dataclass(frozen=True)
class Anomaly:
    key: Any
    environment: str
    namespace: str
    workload: str
    first: int  # epoch seconds: first flagged
    last: int   # epoch seconds: last flagged window's end
    features: tuple[str, ...]
    score: float
    level: str = "service"
    observations: tuple[str, ...] = ()
    algorithm: str = ""
    model_version: int | None = None

    @property
    def ref(self) -> Ref:
        return (self.namespace, self.workload)

    @property
    def cohort(self) -> str:
        return f"{self.environment}/{self.namespace}/{self.workload}"

    @property
    def is_node(self) -> bool:
        return self.namespace == NODE_NAMESPACE


@dataclass
class Topology:
    edges: set[tuple[Ref, Ref]] = field(default_factory=set)        # (caller, callee)
    placement: dict[Ref, set[str]] = field(default_factory=dict)     # workload -> nodes its pods run on
    rollouts: dict[Ref, list[int]] = field(default_factory=dict)     # workload -> rollout times (epoch seconds)
    owners: dict[Ref, str] = field(default_factory=dict)             # workload -> "Deployment/x"
    alerts: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    logs: dict[Ref, dict] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)            # what each part came from

    def callees(self, r: Ref) -> set[Ref]:
        return {b for a, b in self.edges if a == r}

    def callers(self, r: Ref) -> set[Ref]:
        return {a for a, b in self.edges if b == r}


@dataclass
class Link:
    a: Any
    b: Any
    weight: float
    reasons: list[dict]


@dataclass
class Group:
    key: str
    members: list[Anomaly]
    links: list[Link]
    hypotheses: list[dict]

    @property
    def first(self) -> int:
        return min(m.first for m in self.members)

    @property
    def last(self) -> int:
        return max(m.last for m in self.members)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _name(r: Ref) -> str:
    return r[1] if r[0] != NODE_NAMESPACE else f"node {r[1]}"


def link(a: Anomaly, b: Anomaly, topo: Topology, proximity: int = PROXIMITY) -> Link | None:
    """The tie between two anomalies, or None when they are too far apart in time to be one problem."""
    dt = abs(a.first - b.first)
    if dt > proximity:
        return None
    reasons: list[dict] = []
    ra, rb = a.ref, b.ref
    if ra == rb:
        reasons.append({"type": "same_cohort", "weight": 1.0})
    if a.is_node or b.is_node:
        node, other = (a, b) if a.is_node else (b, a)
        if not other.is_node and node.workload in topo.placement.get(other.ref, set()):
            reasons.append({"type": "node", "node": node.workload, "workload": other.workload, "weight": WEIGHTS["node"]})
    else:
        if (ra, rb) in topo.edges or (rb, ra) in topo.edges:
            caller, callee = (ra, rb) if (ra, rb) in topo.edges else (rb, ra)
            reasons.append({"type": "calls", "from": _name(caller), "to": _name(callee), "weight": WEIGHTS["calls"]})
        else:
            via = sorted((topo.callees(ra) & topo.callers(rb)) | (topo.callees(rb) & topo.callers(ra)))
            if via:
                reasons.append({"type": "calls_via", "via": _name(via[0]), "weight": WEIGHTS["calls_via"]})
        shared = sorted(topo.callees(ra) & topo.callees(rb))
        if shared:
            reasons.append({"type": "shared_dependency", "dependency": _name(shared[0]), "weight": WEIGHTS["shared_dependency"]})
        nodes = sorted(topo.placement.get(ra, set()) & topo.placement.get(rb, set()))
        if nodes:
            reasons.append({"type": "same_node", "node": nodes[0], "weight": WEIGHTS["same_node"]})
        if a.namespace == b.namespace:
            reasons.append({"type": "same_namespace", "namespace": a.namespace, "weight": WEIGHTS["same_namespace"]})
    start = min(a.first, b.first)
    near = lambda x: {x} | topo.callees(x) | topo.callers(x)
    for r in sorted(near(ra) & near(rb)):  # a rollout ties two anomalies only when it touches both of them
        hits = [t for t in topo.rollouts.get(r, []) if start - ROLLOUT_LOOKBACK <= t <= start]
        if hits:
            reasons.append({"type": "rollout", "workload": _name(r), "at": _iso(max(hits)), "weight": WEIGHTS["rollout"]})
            break
    if not reasons:
        return None
    reasons.append({"type": "time", "seconds_apart": dt, "weight": round(WEIGHTS["time"] * (1 - dt / proximity), 3)})
    weight = round(min(1.0, sum(r["weight"] for r in reasons)), 3)
    return Link(a.key, b.key, weight, reasons)


def _hypotheses(members: list[Anomaly], topo: Topology) -> list[dict]:
    """Candidate explanations for one group, most members explained first. All unvalidated."""
    out: list[dict] = []
    services = [m for m in members if not m.is_node]
    first = min(m.first for m in members)
    for node in (m for m in members if m.is_node):
        on = [m.workload for m in services if node.workload in topo.placement.get(m.ref, set())]
        out.append({"kind": "node", "subject": f"node/{node.workload}", "explains": len(on) + 1,
                    "statement": f"Node {node.workload} behaved unusually ({', '.join(node.features) or 'several signals'})"
                                 + (f"; {', '.join(on)} run{'s' if len(on) == 1 else ''} on it." if on else "."),
                    "evidence": [node.cohort, *[f"{m.cohort}" for m in services if m.workload in on]]})
    seen: set[Ref] = set()
    for m in members:
        for r in [m.ref, *sorted(topo.callees(m.ref))]:
            if r in seen:
                continue
            hits = [t for t in topo.rollouts.get(r, []) if first - ROLLOUT_LOOKBACK <= t <= first]
            if hits:
                seen.add(r)
                affected = [x.workload for x in services if x.ref == r or r in topo.callees(x.ref)]
                out.append({"kind": "rollout", "subject": f"{(topo.owners.get(r) or 'Deployment/' + r[1])}", "explains": max(1, len(affected)),
                            "statement": f"{_name(r)} was rolled out at {_iso(max(hits))}, {round((first - max(hits)) / 60)} minutes before the first anomaly"
                                         + (f"; {', '.join(sorted(set(affected)))} {'is' if len(set(affected)) == 1 else 'are'} affected." if affected else "."),
                            "evidence": [x.cohort for x in services if x.ref == r or r in topo.callees(x.ref)]})
    refs = {m.ref for m in services}
    for m in services:
        dependents = [x for x in services if x is not m and _reaches(topo, x.ref, m.ref)]
        if dependents and m.first <= min(x.first for x in dependents) + 300:
            out.append({"kind": "dependency", "subject": f"{m.namespace}/{m.workload}", "explains": len(dependents) + 1,
                        "statement": f"{m.workload} may be where this started: {', '.join(sorted(x.workload for x in dependents))} "
                                     f"call{'s' if len(dependents) == 1 else ''} it, and its anomaly was flagged first ({', '.join(m.features) or 'several signals'}).",
                        "evidence": [m.cohort, *[x.cohort for x in dependents]]})
    shared: dict[Ref, list[str]] = {}
    for m in services:
        for c in topo.callees(m.ref):
            if c not in refs:
                shared.setdefault(c, []).append(m.workload)
    for c, who in sorted(shared.items()):
        if len(who) >= 2:
            out.append({"kind": "shared_dependency", "subject": f"{c[0]}/{c[1]}", "explains": len(who),
                        "statement": f"{', '.join(sorted(who))} all call {c[1]}, which was not flagged (it may not be modeled, or may not be the cause).",
                        "evidence": [x.cohort for x in services if x.workload in who]})
    out.sort(key=lambda h: (-h["explains"], ["node", "rollout", "dependency", "shared_dependency"].index(h["kind"])))
    for i, h in enumerate(out):
        h.update({"rank": i + 1, "status": "candidate", "validated": False})
    return out[:6]


def _reaches(topo: Topology, src: Ref, dst: Ref, depth: int = 3) -> bool:
    frontier, seen = {src}, {src}
    for _ in range(depth):
        frontier = {c for f in frontier for c in topo.callees(f)} - seen
        if dst in frontier:
            return True
        seen |= frontier
    return False


def group(anomalies: list[Anomaly], topo: Topology, *, proximity: int = PROXIMITY, threshold: float = LINK_THRESHOLD) -> list[Group]:
    """Connected components of anomalies whose link weight reaches `threshold`. Pure: the benchmark scores it."""
    parent = {a.key: a.key for a in anomalies}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    links: list[Link] = []
    ordered = sorted(anomalies, key=lambda a: (a.first, str(a.key)))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.first - a.first > proximity:
                break
            ln = link(a, b, topo, proximity)
            if ln and ln.weight >= threshold:
                links.append(ln)
                parent[find(a.key)] = find(b.key)
    comps: dict[Any, list[Anomaly]] = {}
    for a in ordered:
        comps.setdefault(find(a.key), []).append(a)
    groups = []
    for members in comps.values():
        keys = {m.key for m in members}
        body = json.dumps(sorted([m.cohort, m.first] for m in members))
        groups.append(Group(hashlib.sha256(body.encode()).hexdigest()[:16], members, [ln for ln in links if ln.a in keys], _hypotheses(members, topo)))
    groups.sort(key=lambda g: (g.first, g.key))
    return groups


# ---- from stored observations ------------------------------------------------------------------------------------


def episodes(rows: list[Any], interval_seconds: int) -> list[Anomaly]:
    """Champion observations flagged unusual, merged per cohort into runs: two flagged windows of one cohort less than
    two tick intervals apart are one anomaly."""
    by_cohort: dict[str, list[Any]] = {}
    for o in rows:
        by_cohort.setdefault(o.cohort, []).append(o)
    out = []
    for cohort, obs in by_cohort.items():
        obs.sort(key=lambda o: o.window_to)
        run: list[Any] = []
        for o in [*obs, None]:
            if o is not None and (not run or (o.window_to - run[-1].window_to).total_seconds() <= 2 * interval_seconds):
                run.append(o)
                continue
            if run:
                out.append(_anomaly_of(run))
            run = [o] if o is not None else []
    return out


def _anomaly_of(run: list[Any]) -> Anomaly:
    o0 = run[0]
    firsts = [datetime.fromisoformat(str(o.context.get("first_flagged_at"))) for o in run if (o.context or {}).get("first_flagged_at")]
    first = int(min(firsts).timestamp()) if firsts else int(o0.window_to.timestamp())
    feats = list(dict.fromkeys(f for o in run for f in (o.context or {}).get("flagged_features") or [d.get("feature") for d in (o.deviations or [])[:1]]))
    return Anomaly(key=(o0.cohort, first), environment=o0.environment, namespace=o0.namespace, workload=o0.service, first=first, last=int(run[-1].window_to.timestamp()),
                   features=tuple(f for f in feats if f), score=max(float(o.score or 0) for o in run),
                   level="infrastructure" if o0.namespace == NODE_NAMESPACE else "service", observations=tuple(str(o.id) for o in run[-20:]),
                   algorithm=o0.algorithm, model_version=(o0.context or {}).get("model_version"))


# ---- context through the executor --------------------------------------------------------------------------------


def edges_query(namespaces: list[str], lookback_seconds: int) -> str:
    """Who called whom from these namespaces over the lookback: Istio's client-side request counters."""
    sel = "|".join(sorted(namespaces))
    return (f'sum by (source_workload_namespace, source_workload, destination_workload_namespace, destination_workload) '
            f'(rate(istio_requests_total{{reporter="source", source_workload_namespace=~"{sel}"}}[{max(300, int(lookback_seconds))}s])) > 0')


def errors_query(namespace: str, seconds: int) -> str:
    """Error-line volume per workload in Loki over the window (LogQL)."""
    return (f'sum by (workload) (label_replace(count_over_time({{namespace="{namespace}"}} |~ "(?i)(error|exception|fatal|panic)" [{max(300, int(seconds))}s]), '
            f'"workload", "$1", "pod", "{WORKLOAD_REGEX}"))')


def workload_of(pod: str) -> str:
    import re

    m = re.match(WORKLOAD_REGEX, pod)
    return m.group(1) if m else pod


async def topology(db, env, namespaces: list[str], workloads: set[Ref], since: datetime, now: datetime) -> Topology:
    """Context for the anomalies' namespaces, read as the automation principal. Every failure is a gap."""
    from sqlalchemy import select

    from ..alerts import automation_principal
    from ..connectors import registry as connectors
    from ..models import AlertEvent
    from ..observability import ML_CONTEXT_GAPS
    from ..rbac import P
    from ..scope import ScopeError, effective_scope
    from ..tools.contracts import ToolContext
    from ..tools.executor import ToolExecutor

    topo = Topology()
    principal = await automation_principal(db)
    try:
        scope = effective_scope(principal, env, P.INVESTIGATION_RUN)
    except ScopeError:
        topo.gaps.append({"source": "access", "reason": "the automation principal cannot read this environment"})
        return topo
    found, _ = await connectors.for_environment(db, env.id)

    def gap(source: str, reason: str) -> None:
        topo.gaps.append({"source": source, "reason": reason[:300]})
        ML_CONTEXT_GAPS.labels(source).inc()

    async def run(tool: str, args: dict) -> Any:
        ctx = ToolContext(None, env, scope, principal, since, now, found, now - since + timedelta(minutes=1))
        ex = ToolExecutor(None, ctx, timeout_seconds=20, read_permissions=(P.INVESTIGATION_RUN,), audit_success=False)
        out = await ex.run(tool, args)
        if not out.ok:
            raise RuntimeError(out.error_kind or out.error or "failed")
        return out.data, out.truncated

    if "prometheus" in found:
        q = edges_query(namespaces, int((now - since).total_seconds()))
        try:
            data, cut = await run("prom_query", {"promql": q, "mode": "instant"})
            for s in (data or {}).get("series") or []:
                m = s.get("metric") or {}
                a, b = (m.get("source_workload_namespace"), m.get("source_workload")), (m.get("destination_workload_namespace"), m.get("destination_workload"))
                if all(a) and all(b) and b[1] != "unknown" and a != b:
                    topo.edges.add((a, b))  # type: ignore[arg-type]
            topo.sources["edges"] = "istio_requests_total (Envoy sidecar telemetry)"
            if cut or len((data or {}).get("series") or []) >= 25:
                gap("mesh", "the call graph hit the connector's series limit; some calls may be missing")
        except Exception as exc:
            gap("mesh", f"call graph unavailable: {exc}")
    else:
        gap("mesh", "no Prometheus connector: no call graph")
    if "kubernetes" in found:
        for ns in namespaces:
            try:
                pods, _ = await run("k8s_list_pods", {"namespace": ns})
                for p in pods or []:
                    wl = workload_of(str(p.get("name") or ""))
                    if p.get("node"):
                        topo.placement.setdefault((ns, wl), set()).add(str(p["node"]))
                    if p.get("owner"):
                        kind, _, name = str(p["owner"]).partition("/")
                        topo.owners[(ns, wl)] = f"Deployment/{wl}" if kind == "ReplicaSet" else str(p["owner"])
            except Exception as exc:
                gap("kubernetes", f"pods in {ns}: {exc}")
            try:
                evs, _ = await run("k8s_events", {"namespace": ns, "warnings_only": True})
                for e in (evs or [])[:50]:
                    obj = str(e.get("object") or "")
                    topo.events.append({"namespace": ns, "workload": workload_of(obj.partition("/")[2]) if "/" in obj else obj, "reason": e.get("reason"),
                                        "count": e.get("count"), "last": e.get("lastSeen"), "object": obj})
            except Exception as exc:
                gap("kubernetes", f"events in {ns}: {exc}")
        for ns, wl in sorted(workloads)[:10]:
            if ns == NODE_NAMESPACE or not str(topo.owners.get((ns, wl), "Deployment/")).startswith("Deployment/"):
                continue
            try:
                hist, _ = await run("k8s_rollout_history", {"namespace": ns, "deployment": wl})
                times = []
                for r in (hist or {}).get("revisions") or []:
                    created = r.get("created")
                    if created:
                        times.append(int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()))
                topo.rollouts[(ns, wl)] = sorted(times)
            except Exception as exc:
                gap("kubernetes", f"rollouts of {ns}/{wl}: {exc}")
    else:
        gap("kubernetes", "no Kubernetes connector: no placement, owners, rollouts or events")
    if "loki" in found:
        span = max(300, int((now - since).total_seconds()))
        for ns in namespaces:
            if ns == NODE_NAMESPACE:
                continue
            q = errors_query(ns, span)
            try:
                data, _ = await run("loki_query", {"logql": q, "limit": 25})
                for s in (data or {}).get("series") or []:
                    wl = (s.get("metric") or {}).get("workload")
                    vals = s.get("values") or []
                    if wl and vals:
                        topo.logs[(ns, wl)] = {"error_lines": int(float(vals[-1][1])), "window_seconds": span}
                topo.sources["logs"] = "Loki error-line counts (error, exception, fatal, panic)"
            except Exception as exc:
                gap("loki", f"error-log counts in {ns}: {exc}")
    rows = (await db.execute(select(AlertEvent).where(AlertEvent.environment_id == env.id, AlertEvent.received_at >= since).order_by(AlertEvent.received_at.desc()).limit(200))).scalars().all()
    for a in rows:
        labels = a.labels or {}
        ns = labels.get("namespace")
        if ns and ns not in namespaces:
            continue
        wl = labels.get("workload") or labels.get("deployment") or labels.get("statefulset") or (workload_of(labels["pod"]) if labels.get("pod") else None) or labels.get("service")
        topo.alerts.append({"alertname": a.alertname, "status": a.status, "namespace": ns, "workload": wl, "at": (a.starts_at or a.received_at).isoformat(), "source": a.source})
    return topo


# ---- the tick ----------------------------------------------------------------------------------------------------


def _member_out(m: Anomaly) -> dict[str, Any]:
    return {"cohort": m.cohort, "namespace": m.namespace, "workload": m.workload, "level": m.level, "first": _iso(m.first), "last": _iso(m.last), "features": list(m.features),
            "score": m.score, "observations": list(m.observations), "algorithm": m.algorithm, "model_version": m.model_version}


def _context(g: Group, topo: Topology) -> dict[str, Any]:
    refs = {m.ref for m in g.members}
    deps = {b for a, b in topo.edges if a in refs} | {a for a, b in topo.edges if b in refs}
    return {"edges": [{"from": f"{a[0]}/{a[1]}", "to": f"{b[0]}/{b[1]}"} for a, b in sorted(topo.edges) if a in refs or b in refs][:40],
            "placement": {f"{r[0]}/{r[1]}": sorted(topo.placement[r]) for r in refs if r in topo.placement},
            "rollouts": {f"{r[0]}/{r[1]}": [_iso(t) for t in topo.rollouts[r][-5:]] for r in refs | deps if topo.rollouts.get(r)},
            "deployment_frequency": {f"{r[0]}/{r[1]}": {"last_24h": sum(1 for t in topo.rollouts[r] if t >= g.last - 86400), "last_7d": sum(1 for t in topo.rollouts[r] if t >= g.last - 7 * 86400),
                                                        "note": "from the revisions Kubernetes keeps (revisionHistoryLimit): a lower bound"} for r in refs if topo.rollouts.get(r)},
            "alerts": [a for a in topo.alerts if (a["namespace"], a["workload"]) in refs or (a["workload"] is None and a["namespace"] in {r[0] for r in refs})][:20],
            "events": [e for e in topo.events if (e["namespace"], e["workload"]) in refs][:20],
            "logs": {f"{r[0]}/{r[1]}": topo.logs[r] for r in refs if r in topo.logs},
            "gaps": topo.gaps, "sources": topo.sources}


async def run(env_name: str, *, now: datetime | None = None, topo_override: Topology | None = None) -> dict[str, Any]:
    """Groups the environment's recent champion anomalies and stores the groups. Shadow output: nothing else changes."""
    from sqlalchemy import select

    from ..config import get_settings
    from ..db import session_scope
    from ..models import Environment, MLCorrelation, MLObservation
    from ..observability import ML_CORRELATION_SECONDS, ML_CORRELATIONS
    from . import mode

    s = get_settings()
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=s.ml_correlation_lookback_minutes)
    began = time.monotonic()
    async with session_scope() as db:
        state = await mode(db, "anomaly_detection")
        if state == "disabled":
            return {"skipped": "anomaly_detection is disabled"}
        env = (await db.execute(select(Environment).where(Environment.name == env_name))).scalar_one_or_none()
        if env is None:
            return {"skipped": f"no environment {env_name}"}
        rows = (await db.execute(select(MLObservation).where(MLObservation.environment == env_name, MLObservation.status == "scored", MLObservation.unusual.is_(True),
                                                             MLObservation.window_to >= since, MLObservation.window_to <= now))).scalars().all()
        rows = [o for o in rows if (o.context or {}).get("role") == "champion"]
        anomalies = episodes(rows, s.ml_anomaly_interval_minutes * 60)
        if not anomalies:
            closed = await _close(db, env_name, now, s.ml_correlation_close_after_minutes)
            return {"groups": 0, "anomalies": 0, "closed": closed}
        namespaces = sorted({a.namespace for a in anomalies if not a.is_node})[: s.ml_correlation_max_namespaces]
        refs = {a.ref for a in anomalies}
        topo = topo_override if topo_override is not None else await topology(db, env, namespaces, refs, since, now)
    groups = group([a for a in anomalies if a.is_node or a.namespace in namespaces], topo, proximity=s.ml_correlation_window_minutes * 60)
    created = updated = 0
    async with session_scope() as db:
        open_rows = (await db.execute(select(MLCorrelation).where(MLCorrelation.environment == env_name, MLCorrelation.status == "open"))).scalars().all()
        for g in groups:
            cohorts = {m.cohort for m in g.members}
            match = next((r for r in open_rows if cohorts & {m["cohort"] for m in r.members} and r.last_seen.timestamp() + s.ml_correlation_window_minutes * 60 >= g.first), None)
            members = {m["cohort"]: m for m in (match.members if match else [])}
            for m in g.members:
                cur = members.get(m.cohort)
                out = _member_out(m)
                if cur:
                    out["first"] = min(cur["first"], out["first"])
                    out["observations"] = list(dict.fromkeys([*cur.get("observations", []), *out["observations"]]))[-20:]
                members[m.cohort] = out
            body = dict(members=sorted(members.values(), key=lambda x: x["first"]), links=[{"a": list(ln.a), "b": list(ln.b), "weight": ln.weight, "reasons": ln.reasons} for ln in g.links],
                        hypotheses=g.hypotheses, context=_context(g, topo), namespaces=sorted({m["namespace"] for m in members.values()}), engine_version=ENGINE, mode=state,
                        last_seen=datetime.fromtimestamp(g.last, timezone.utc), updated_at=now)
            if match:
                for k, v in body.items():
                    setattr(match, k, v)
                updated += 1
                ML_CORRELATIONS.labels("updated").inc()
            else:
                row = MLCorrelation(id=uuid.uuid4(), environment=env_name, status="open", first_seen=datetime.fromtimestamp(g.first, timezone.utc), **body)
                db.add(row)
                open_rows.append(row)
                created += 1
                ML_CORRELATIONS.labels("created").inc()
        closed = await _close(db, env_name, now, s.ml_correlation_close_after_minutes)
    ML_CORRELATION_SECONDS.observe(time.monotonic() - began)
    return {"anomalies": len(anomalies), "groups": len(groups), "created": created, "updated": updated, "closed": closed, "gaps": topo.gaps, "note": NOTE}


async def _close(db, env_name: str, now: datetime, after_minutes: int) -> int:
    from sqlalchemy import update

    from ..models import MLCorrelation
    from ..observability import ML_CORRELATIONS

    r = await db.execute(update(MLCorrelation).where(MLCorrelation.environment == env_name, MLCorrelation.status == "open",
                                                    MLCorrelation.last_seen < now - timedelta(minutes=after_minutes)).values(status="closed", updated_at=now))
    n = r.rowcount or 0
    if n:
        ML_CORRELATIONS.labels("closed").inc(n)
    return n


def readable(principal, row) -> bool:
    """A group is shown only to someone who may read ML results in every namespace it spans (the environment as a
    whole for node members), so no link or member from another scope leaks through it."""
    from ..rbac import P

    return all(principal.can(P.ML_READ, row.environment, None if ns == NODE_NAMESPACE else ns) for ns in (row.namespaces or ["*"]))


def group_out(row) -> dict[str, Any]:
    return {"id": str(row.id), "environment": row.environment, "namespaces": row.namespaces, "status": row.status, "first_seen": row.first_seen.isoformat(),
            "last_seen": row.last_seen.isoformat(), "members": row.members, "links": row.links, "hypotheses": row.hypotheses, "context": row.context,
            "engine_version": row.engine_version, "mode": row.mode, "feedback": row.feedback, "note": NOTE,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}
