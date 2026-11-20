"""Evidence collection for the pipeline's stages, through Investigator._call and the ToolExecutor only.

Every argument that names a place (environment, namespace, pods, repository, window) is bound here from the
investigation and the identified target. Nothing a model or a log line says can widen it: an agent may ask for a
probe by its key from PROBES, and the probe's binder decides the arguments. A source that is not configured, a read
that fails or is refused, and a call the budget does not allow are recorded as gaps with their reason, never guessed.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..crew.tools import controllers_for, owned_by
from ..tools.builtin import REGISTRY
from ..tools.contracts import K8S_NAME_RE
from . import signals as sig
from .pipeline import Budget, StageState, Target

LOG_LINES = 100

# Phrases for queries built from signals (similar incidents, runbook search). Keys only from signals.py.
PHRASES = {  # most telling first: what names a mechanism, then the symptoms, then context
    "log.db_pool_exhausted": "database connection pool exhausted", "k8s.oomkilled": "OOMKilled", "log.oom": "out of memory", "k8s.image_pull": "ImagePullBackOff",
    "k8s.config_error": "CreateContainerConfigError", "log.dns_failure": "DNS resolution failure", "log.connection_refused": "connection refused",
    "metric.db_connections_saturated": "database connections at the pool limit", "log.db_timeout": "database timeouts", "log.upstream_timeout": "upstream timeout",
    "log.config_error": "configuration error", "log.tls_error": "TLS certificate error", "k8s.unschedulable": "pods unschedulable", "node.pressure": "node pressure",
    "metric.traffic_surge": "traffic surge", "metric.cpu_throttling": "CPU throttling", "metric.memory_near_limit": "memory near limit", "k8s.crashloop": "CrashLoopBackOff",
    "metric.latency_increase": "high latency", "metric.error_rate_increase": "elevated error rate", "metric.db_connections_increase": "rising database connections",
    "k8s.probe_failed": "readiness probe failures", "k8s.evicted": "pods evicted", "metric.cpu_increase": "CPU increase", "deploy.rollout": "after a deployment",
}


@dataclass
class Gap:
    stage: str
    source: str
    reason: str

    def out(self) -> dict[str, str]:
        return {"stage": self.stage, "source": self.source, "reason": self.reason}


@dataclass(frozen=True)
class Probe:
    key: str
    tool: str
    label: str
    bind: Callable[["Collector"], dict[str, Any]]  # raises LookupError with the reason it cannot apply


def _pods_or(c: "Collector") -> str:
    rx = c.target.regex()
    if not rx:
        raise LookupError("the target's pods are not known")
    return rx


def _worst_pod(c: "Collector") -> str:
    pods = c.member_pods()
    if not pods:
        raise LookupError("the target has no pods to read")
    return max(pods, key=lambda p: (p.get("restarts") or 0, bool(p.get("problems"))))["name"]


def _deployment(c: "Collector") -> str:
    if c.target.kind != "Deployment" or not c.target.workload:
        raise LookupError("rollout history and deployed commits apply to Deployments only")
    return c.target.workload


def _repo(c: "Collector") -> str:
    if not c.target.repository:
        raise LookupError("no single Git repository is configured for this environment")
    return c.target.repository


def _deps(c: "Collector") -> str:
    deps = sorted(d for d in c.target.dependencies if K8S_NAME_RE.match(d))
    if not deps:
        raise LookupError("no dependency of the target has been identified")
    return "(" + "|".join(re.escape(d) for d in deps[:5]) + ")-.*"


PROBES: dict[str, Probe] = {p.key: p for p in (
    Probe("service_signals", "prom_service_signals", "golden signals (latency, errors, traffic, database connections)",
          lambda c: {"namespace": c.target.namespace, **({"workload": c.target.workload} if c.target.workload else {}), "minutes": c.target.minutes}),
    Probe("pod_metrics", "prom_pod_resources", "CPU, memory and throttling of the target's pods",
          lambda c: {"namespace": c.target.namespace, "pod_regex": _pods_or(c), "minutes": max(5, c.target.minutes)}),
    Probe("alerts", "prom_alerts", "firing alerts", lambda c: {"firing_only": True}),
    Probe("pool_logs", "loki_search", "log lines about connections and pools",
          lambda c: {"namespace": c.target.namespace, "pod": _pods_or(c), "contains": "connection", "limit": LOG_LINES, "minutes": c.target.minutes}),
    Probe("previous_logs", "k8s_pod_logs", "the previous (crashed) container's logs",
          lambda c: {"namespace": c.target.namespace, "pod": _worst_pod(c), "previous": True, "tail_lines": LOG_LINES}),
    Probe("describe_pod", "k8s_describe_pod", "one pod's containers, limits, last state and environment variable names",
          lambda c: {"namespace": c.target.namespace, "pod": _worst_pod(c)}),
    Probe("pod_list", "k8s_list_pods", "the namespace's pods", lambda c: {"namespace": c.target.namespace}),
    Probe("events", "k8s_events", "warning events", lambda c: {"namespace": c.target.namespace, "warnings_only": True}),
    Probe("rollout_history", "k8s_rollout_history", "the Deployment's revisions", lambda c: {"namespace": c.target.namespace, "deployment": _deployment(c)}),
    Probe("deployed_commit", "git_compare_deployment", "the commit the Deployment runs",
          lambda c: {"namespace": c.target.namespace, "deployment": _deployment(c), "repository": _repo(c)}),
    Probe("recent_commits", "git_recent_commits", "commits in the window",
          lambda c: {"repository": _repo(c), "hours": max(1, math.ceil(c.target.minutes / 60)), "limit": 20}),
    Probe("config_audit", "k8s_config_audit", "configuration and missing references", lambda c: {"namespace": c.target.namespace}),
    Probe("services", "k8s_services", "Services and their ready endpoints", lambda c: {"namespace": c.target.namespace}),
    Probe("dependency_logs", "loki_search", "error logs of the target's dependencies in the namespace",
          lambda c: {"namespace": c.target.namespace, "pod": _deps(c), "errors_only": True, "limit": LOG_LINES, "minutes": c.target.minutes}),
    Probe("nodes", "k8s_nodes", "node conditions (needs cluster-wide access)", lambda c: {}),
    Probe("dns", "k8s_dns_health", "CoreDNS health (needs cluster-wide access)", lambda c: {}),
    Probe("anomalies", "ml_anomalies", "anomaly-model observations",
          lambda c: {"namespace": c.target.namespace, **({"service": c.target.workload} if c.target.workload else {}), "minutes": max(15, c.target.minutes)}),
)}
PROBE_TOOLS = {k: (p.tool,) for k, p in PROBES.items()}


@dataclass
class Collector:
    inv: Any
    executor: Any
    ctx: Any
    budget: Budget
    target: Target
    question: str
    request: dict[str, Any]
    window: tuple[datetime, datetime]
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    signals: list[sig.Signal] = field(default_factory=list)
    stage_of: dict[str, str] = field(default_factory=dict)
    by_of: dict[str, str] = field(default_factory=dict)
    read_tools: set[str] = field(default_factory=set)
    refused: list[dict[str, str]] = field(default_factory=list)
    incident: dict[str, Any] = field(default_factory=dict)
    namespace_services: list[str] = field(default_factory=list)

    # ---- calls ----------------------------------------------------------------------------------------

    def gap(self, stage: str, source: str, reason: str) -> None:
        g = Gap(stage, source, sig.redact(reason)[:300])
        if all((x.source, x.reason) != (g.source, g.reason) for x in self.gaps):
            self.gaps.append(g)

    def _record(self, ref: str) -> dict[str, Any] | None:
        return next((e for e in reversed(self.inv.evidence) if e["ref"] == ref), None)

    def _existing(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """A record for the same call made earlier in this investigation (by another stage, or before a resume)."""
        return next((e for e in reversed(self.inv.evidence) if e["tool"] == tool and all(e.get("args", {}).get(k) == v for k, v in args.items())), None)

    def _keep(self, st: StageState, rec: dict[str, Any], by: str) -> dict[str, Any]:
        ref = rec["ref"]
        self.records[ref] = rec
        self.stage_of.setdefault(ref, st.key)
        self.by_of.setdefault(ref, by)
        if ref not in st.evidence:
            st.evidence.append(ref)
        if rec.get("ok"):
            self.read_tools.add(rec["tool"])
        return rec

    async def call(self, st: StageState, tool: str, args: dict[str, Any], purpose: str, *, by: str = "pipeline", extra_share: int = 0) -> dict[str, Any] | None:
        spec = REGISTRY.get(tool)
        if spec is None:
            self.gap(st.key, tool, "no such tool in this MOKS version")
            return None
        missing = [c for c in (spec.connector_type, *spec.also_requires) if not self.ctx.available(c)]
        if missing:
            self.gap(st.key, missing[0], f"no {missing[0]} connector is configured for environment {self.target.environment}")
            return None
        key = tool + json.dumps(args, sort_keys=True)
        if key in self.keys:
            return self._keep(st, self.records[self.keys[key]], by)
        if spec.cluster_scoped and self.executor.would_deny(tool, args):
            self.gap(st.key, tool, f"{tool} reads the whole cluster, which this person's access does not cover")
            return None
        why = self.budget.can_call(st.key, extra_share)
        if why:
            self.gap(st.key, tool, f"not run: {why}")
            st.partial = True
            return None
        self.budget.spend(st.key)
        out = await self.inv._call(self.executor, tool, args, f"unified {st.key}: {purpose}")
        if out is None or not out.evidence_ref:
            rec = self._existing(tool, args)
            if rec is not None:
                self.keys[key] = rec["ref"]
                return self._keep(st, rec, by)
            reason = (out.error if out is not None else None) or "the investigation's tool budget is used up, or the request's scope does not need it"
            self.gap(st.key, tool, f"not run: {reason}")
            st.partial = True
            return None
        st.tool_calls += 1
        rec = self._record(out.evidence_ref)
        if rec is None:
            return None
        self.keys[key] = rec["ref"]
        self._keep(st, rec, by)
        if not rec.get("ok"):
            self.gap(st.key, tool, f"{'refused' if rec.get('denied') else 'failed'}: {rec.get('error') or rec.get('summary')}")
            st.partial = True
        return rec

    def refresh(self) -> None:
        """Signals from every record, narrowed to the target as it is now known."""
        self.signals = [s for rec in self.records.values() for s in sig.extract(rec, self.target if self.target.workload else None, self.window)]

    def present(self, *keys: str) -> list[sig.Signal]:
        return [s for s in self.signals if s.present and (not keys or s.key in keys or any(s.key.startswith(k + ":") for k in keys))]

    def member_pods(self) -> list[dict[str, Any]]:
        for rec in reversed(list(self.records.values())):
            if rec["tool"] == "k8s_list_pods" and rec.get("ok") and isinstance(rec.get("data"), list):
                return [p for p in rec["data"] if isinstance(p, dict) and p.get("name") in self.target.pods]
        return []

    def phrases(self, limit: int = 6) -> list[str]:
        """What the evidence shows, in PHRASES' order (the most telling first)."""
        keys = {s.key for s in self.present()}
        return [p for k, p in PHRASES.items() if k in keys][:limit]

    async def probe(self, st: StageState, key: str, by: str) -> dict[str, Any] | None:
        p = PROBES.get(key)
        if p is None:
            self.refused.append({"probe": key[:60], "by": by, "reason": "not an allowlisted probe"})
            return None
        try:
            args = p.bind(self)
        except LookupError as exc:
            self.gap(st.key, p.tool, f"{p.label}: {exc}")
            return None
        return await self.call(st, p.tool, args, f"probe {key} for {by}", by=by)


# ---- stages ----------------------------------------------------------------------------------------------

async def detect(c: Collector, st: StageState) -> None:
    ns = c.target.namespace
    if c.ctx.available("prometheus"):
        await c.call(st, "prom_alerts", {"firing_only": True}, "firing alerts in scope")
    await c.call(st, "k8s_triage", {"namespace": ns}, "the namespace's health findings")
    c.refresh()
    firing = c.present("alert.firing")
    c.incident = {"trigger": c.request.get("trigger") or "user", "symptom": sig._clip(c.question, 300), "alerts": [s.detail for s in firing][:5],
                  "findings": sorted({s.key for s in c.present() if s.key.startswith("k8s.")})}
    st.data["incident"] = c.incident


CATEGORY_WORDS = {"latency": r"latenc|slow|p9[59]|response time|timeout", "errors": r"error|\b5\d\d\b|fail|exception", "availability": r"\bdown\b|unavailable|crash|outage|not (ready|responding)|restart",
                  "saturation": r"\bcpu\b|memory|\boom|throttl|saturat|disk", "deployment": r"deploy|release|rollout|upgrade|version", "data_store": r"database|postgres|mysql|redis|connection pool|\bdb\b",
                  "network": r"\bdns\b|network|connection refused|\btls\b|certificate", "capacity": r"traffic|load|scal|capacity|pending|unschedulable"}
SIGNAL_CATEGORY = {"metric.latency_increase": "latency", "metric.error_rate_increase": "errors", "log.http_5xx": "errors", "k8s.crashloop": "availability", "k8s.workload_unavailable": "availability",
                   "k8s.not_ready": "availability", "k8s.oomkilled": "saturation", "metric.cpu_throttling": "saturation", "metric.memory_near_limit": "saturation",
                   "k8s.image_pull": "deployment", "deploy.rollout": "deployment", "log.db_pool_exhausted": "data_store", "log.db_timeout": "data_store",
                   "log.dns_failure": "network", "log.connection_refused": "network", "k8s.unschedulable": "capacity", "metric.traffic_surge": "capacity"}


def classify(c: Collector, st: StageState, production: bool) -> dict[str, Any]:
    """Categories and a severity estimate, by stated rules. A heuristic for ordering work, not an incident severity."""
    asked = [k for k, rx in CATEGORY_WORDS.items() if re.search(rx, c.question, re.I)]
    seen = [SIGNAL_CATEGORY[s.key] for s in c.present() if s.key in SIGNAL_CATEGORY]
    categories = list(dict.fromkeys([*seen, *asked])) or ["unknown"]
    why: list[str] = []
    alert_sev = [s.detail for s in c.present("alert.firing") if "(critical" in s.detail]
    user_facing = any(k in categories for k in ("availability", "errors", "latency"))
    if alert_sev:
        severity = "critical"
        why.append("a critical alert is firing")
    elif user_facing and production:
        severity = "high"
        why.append("user-facing symptoms in a production environment")
    elif user_facing or c.present("k8s.crashloop", "k8s.oomkilled"):
        severity = "medium"
        why.append("user-facing symptoms or failing pods" + ("" if production else " outside production"))
    else:
        severity = "low"
        why.append("no user-facing symptom in the evidence so far")
    out = {"categories": categories, "from_question": asked, "from_evidence": list(dict.fromkeys(seen)), "severity": severity, "severity_basis": why,
           "note": "A rule-based estimate to order the investigation, not an incident severity anyone declared."}
    st.data.update(out)
    return out


async def _members(c: Collector, st: StageState) -> None:
    t = c.target
    if t.kind == "Pod":
        rec = await c.call(st, "k8s_list_pods", {"namespace": t.namespace}, "the target pod")
        names = {p.get("name") for p in (rec or {}).get("data") or [] if isinstance(p, dict)} if rec and rec.get("ok") else set()
        t.pods = frozenset({t.workload}) if t.workload in names else frozenset()
        return
    if t.kind == "Deployment":
        rec = await c.call(st, "k8s_rollout_history", {"namespace": t.namespace, "deployment": t.workload}, "the Deployment's ReplicaSets, to find its pods by owner reference")
        owners = frozenset(("ReplicaSet", r["replicaSet"], r.get("uid")) for r in ((rec or {}).get("data") or {}).get("revisions") or [] if r.get("replicaSet")) if rec and rec.get("ok") else frozenset()
    else:
        rec = await c.call(st, "k8s_workloads", {"namespace": t.namespace}, "the workload's controller, to find its pods by owner reference")
        owners, why = controllers_for(t, (rec or {}).get("data") or []) if rec and rec.get("ok") else (frozenset(), "")  # type: ignore[arg-type]
        if why:
            st.note(why)
    t.owners = owners
    if not owners:
        st.note(f"no controller of {t.label()} was found, so its pods are unknown")
        return
    pods = await c.call(st, "k8s_list_pods", {"namespace": t.namespace}, "pods owned by the workload")
    t.pods = frozenset(p["name"] for p in (pods or {}).get("data") or [] if isinstance(p, dict) and owned_by(p, owners)) if pods and pods.get("ok") else frozenset()


def _candidates(c: Collector) -> list[tuple[str, str, str]]:
    """(kind, name, why) workloads the evidence says are unhealthy, worst first."""
    triage = next((r for r in c.records.values() if r["tool"] == "k8s_triage" and r.get("ok") and isinstance(r.get("data"), dict)), None)
    if triage is None:
        return []
    data = triage["data"]
    kinds = {w.get("name"): w.get("kind", "Deployment") for w in data.get("workloads") or [] if isinstance(w, dict)}
    rank = {"high": 0, "medium": 1, "low": 2}
    out: list[tuple[str, str, str]] = []
    for f in sorted(data.get("findings") or [], key=lambda f: rank.get(f.get("severity"), 3)):
        name = None
        owner = str(f.get("owner") or "")
        if owner.startswith("ReplicaSet/"):
            rs = owner.split("/", 1)[1]
            name = next((w for w in kinds if rs.startswith(w + "-")), None)
        elif "/" in owner:
            name = owner.split("/", 1)[1]
        for r in f.get("resources") or []:
            if name is None and str(r).startswith("pod/"):
                name = next((w for w in kinds if str(r)[4:].startswith(w + "-")), None) or sig.workload_of(str(r)[4:])
            elif name is None and "/" in str(r) and str(r).split("/", 1)[0].lower() in ("deployment", "statefulset", "daemonset"):
                name = str(r).split("/", 1)[1]
        if name and all(name != n for _, n, _ in out):
            out.append((kinds.get(name, "Deployment"), name, f"{f.get('kind')} finding: {sig._clip(f.get('title'), 120)}"))
    mentioned = [n for n in kinds if n and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", c.question)]
    return sorted(out, key=lambda x: x[1] not in mentioned) + [(kinds[n], n, "named in the question") for n in mentioned if all(n != o[1] for o in out)]


async def identify(c: Collector, st: StageState) -> None:
    t = c.target
    if t.workload:
        t.how = "requested"
    else:
        found = _candidates(c)
        if found:
            t.kind, t.workload, t.how = found[0][0], found[0][1], found[0][2]
            t.also = [f"{k} {n}" for k, n, _ in found[1:4]]
        else:
            st.note("no single unhealthy workload was identified; the investigation stays at namespace level")
            t.how = "namespace"
    if t.workload:
        await _members(c, st)
        services = await c.call(st, "k8s_services", {"namespace": t.namespace}, "the Services that select the workload's pods, and their endpoints")
        rows = (services or {}).get("data") or [] if services and services.get("ok") else []
        own = [s.get("name") for s in rows if isinstance(s, dict) and set(s.get("matchingPods") or []) & set(t.pods)]
        t.service = own[0] if own else t.workload
        c.namespace_services = [s.get("name") for s in rows if isinstance(s, dict) and s.get("name")][:60]
        st.data["services"] = {"own": own, "in_namespace": c.namespace_services[:30]}
    c.refresh()
    st.data["target"] = t.out()


async def telemetry(c: Collector, st: StageState) -> None:
    t = c.target
    rx = t.regex()
    if c.ctx.available("prometheus"):
        if rx:
            await c.call(st, "prom_pod_resources", {"namespace": t.namespace, "pod_regex": rx, "minutes": max(5, t.minutes)}, "CPU, memory and throttling of the target's pods")
        await c.call(st, "prom_service_signals", {"namespace": t.namespace, **({"workload": t.workload} if t.workload else {}), "minutes": t.minutes}, "golden signals")
    if c.ctx.available("loki") and (rx or not t.workload):
        await c.call(st, "loki_search", {"namespace": t.namespace, **({"pod": rx} if rx else {}), "errors_only": True, "limit": LOG_LINES, "minutes": t.minutes}, "error logs of the target")
    restarted = sorted((p for p in c.member_pods() if (p.get("restarts") or 0) > 0), key=lambda p: -(p.get("restarts") or 0))[:2]
    for p in restarted:
        await c.call(st, "k8s_pod_logs", {"namespace": t.namespace, "pod": p["name"], "previous": True, "tail_lines": LOG_LINES}, "the crashed container's last words")
    if not restarted and not c.ctx.available("loki") and c.member_pods():
        await c.call(st, "k8s_pod_logs", {"namespace": t.namespace, "pod": c.member_pods()[0]["name"], "tail_lines": LOG_LINES}, "current logs (no Loki)")
    await c.call(st, "k8s_events", {"namespace": t.namespace, "warnings_only": True}, "warning events")
    known = set(c.namespace_services)
    hosts = [h for r in c.records.values() if r["tool"] in ("loki_search", "k8s_pod_logs") and r.get("ok") for h in sig.log_services(r)]
    deps = {h for h in hosts if h in known or not known} - {t.service, t.workload}
    t.dependencies = frozenset(d for d in deps if d and K8S_NAME_RE.match(d))
    c.refresh()


async def traces(c: Collector, st: StageState) -> None:
    t = c.target
    backend = next((n for n, spec in REGISTRY.items() if n.startswith("trace_search") and c.ctx.available(spec.connector_type)), None)
    if backend:
        fields = REGISTRY[backend].args.model_fields
        args = {k: v for k, v in {"namespace": t.namespace, "service": t.service, "minutes": t.minutes, "errors_only": True, "limit": 20}.items() if k in fields and v is not None}
        await c.call(st, backend, args, "error traces of the target")
        c.refresh()
        return
    ids: list[str] = []
    for r in c.records.values():
        if r["tool"] in ("loki_search", "k8s_pod_logs") and r.get("ok") and not (r.get("args") or {}).get("trace_id"):
            ids += [i for i in sig.trace_ids(r) if i not in ids]
    if not ids:
        c.gap(st.key, "traces", "no trace backend is connected and the error logs carry no trace ids")
        st.skip("no trace backend is connected and the error logs carry no trace ids")
        return
    if not c.ctx.available("loki"):
        c.gap(st.key, "traces", "trace ids were found but there is no Loki to follow them through")
        st.skip("no Loki to follow trace ids through")
        return
    for tid in ids[:2]:
        await c.call(st, "loki_search", {"namespace": t.namespace, "trace_id": tid, "limit": 50, "minutes": t.minutes}, "one failing request followed across services by its trace id")
    st.note("followed through logs: spans and their timings need a trace backend, and only this namespace's services are visible")
    c.gap(st.key, "traces", "no trace backend is connected: span timings are not available; trace ids were followed through logs instead")
    c.refresh()


async def changes(c: Collector, st: StageState) -> None:
    t = c.target
    if t.kind == "Deployment" and t.workload:
        await c.call(st, "k8s_rollout_history", {"namespace": t.namespace, "deployment": t.workload}, "rollouts in the window")
    await c.call(st, "k8s_config_audit", {"namespace": t.namespace}, "configuration and missing references")
    if not c.ctx.available("git"):
        c.gap(st.key, "git", f"no git connector is configured for environment {t.environment}")
    elif not t.repository:
        c.gap(st.key, "git", "no single repository is configured for this environment, so commits cannot be tied to the workload")
    else:
        await c.call(st, "git_recent_commits", {"repository": t.repository, "hours": max(1, math.ceil(t.minutes / 60)), "limit": 20}, "commits in the window")
        if t.kind == "Deployment" and t.workload:
            await c.call(st, "git_compare_deployment", {"namespace": t.namespace, "deployment": t.workload, "repository": t.repository}, "the commit the Deployment runs")
    c.refresh()


async def anomalies(c: Collector, st: StageState) -> None:
    t = c.target
    await c.call(st, "ml_anomalies", {"namespace": t.namespace, **({"service": t.workload} if t.workload else {}), "minutes": max(15, t.minutes), "limit": 20},
                 "anomaly-model observations for the target")
    c.refresh()


async def history(c: Collector, st: StageState) -> None:
    from ..ml.learning import signature

    t = c.target
    symptoms = [s.key.split(".", 1)[1].replace(":", "_") for s in c.present() if s.key.split(".", 1)[0] in ("k8s", "metric", "log") and re.fullmatch(r"[a-z][a-z0-9_]{1,40}", s.key.split(".", 1)[1].replace(":", "_"))]
    first_error = next((s.detail for s in c.present() if s.key.startswith("log.")), "")
    args = {"symptoms": list(dict.fromkeys(symptoms))[:12], "services": list(dict.fromkeys(x for x in (t.service, t.workload) if x and K8S_NAME_RE.match(x)))[:2],
            "signature": [x for x in signature(first_error) if re.fullmatch(r"(<[a-z]+>|[a-z][a-z0-9_]{1,40})", x)][:40],
            "changes": bool(c.present("deploy.rollout")), "text": sig._clip(c.question, 300), "k": 5}
    await c.call(st, "similar_incidents", args, "approved past incidents like this one: leads, never evidence of the current cause")
    phrases = c.phrases(4)
    if phrases:
        await c.call(st, "kb_search", {"query": f"postmortem incident {' '.join(phrases)} {t.service or ''}"[:300].strip(), "k": 4}, "postmortems about these symptoms")
    c.refresh()


async def semantic(c: Collector, st: StageState) -> None:
    t = c.target
    phrases = c.phrases(6)
    what = ", ".join(phrases) if phrases else sig._clip(c.question, 200)
    rec = await c.call(st, "kb_search", {"query": f"How to troubleshoot {t.service or t.namespace}: {what}"[:450], "k": 5}, "runbooks for these symptoms, by meaning")
    mode = ((rec or {}).get("source") or {}).get("mode") if rec else None
    if rec and rec.get("ok") and mode not in ("hybrid", "vector"):
        st.partial = True
        st.note(f"semantic search ran as {mode or 'unknown'}: no embedding index answered, so it matched words only")
        c.gap(st.key, "embeddings", f"knowledge search ran as {mode or 'unknown'}, not with embeddings")
    st.data["mode"] = mode
    c.refresh()


def wanted_probes(hyps: list[Any], limit: int) -> list[tuple[str, str]]:
    """(probe, hypothesis id) for what the leading hypotheses still lack, best first."""
    out: list[tuple[str, str]] = []
    for h in hyps:
        for g in h.missing:
            if g.observed_absent:
                continue
            for p in g.probes:
                if p in PROBES and all(p != x for x, _ in out):
                    out.append((p, h.id))
    return out[:limit]

