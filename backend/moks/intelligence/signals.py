"""What a stored result shows, as named signals: deterministic reads of the evidence, never of model text.

A signal is `present` when the evidence shows the condition and absent when the evidence was read and shows it is
not there (a Deployment with no rollout in the window, memory far below its limit). Absence is an observation too:
it is what contradicts a hypothesis. A source that was not read gives no signal at all, which scoring counts as
missing evidence, not as absence.

Signals carry the time the evidence puts on them (a rollout's creation, a metric's change point, the first log line
that matched), which is what temporal correlation compares. Thresholds are stated here and in the docs; they decide
what is worth naming, not what is true.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..redaction import redact
from .evidence import source_type, workload_of

TRACE_ID = re.compile(r"(?i)\b(?:trace[_-]?id|traceid|trace)[\"'=: ]+([0-9a-f]{32})\b")

# Operational topics shared by Git messages, runbooks, past incidents and the hypothesis library.
TOPICS: dict[str, re.Pattern] = {k: re.compile(v, re.I) for k, v in {
    "connection_pool": r"connection[- ]pool|pool (is )?exhaust|too many (clients|connections)|max_connections|connection slots|hikari|pgbouncer|pool[_ ]size|db pool|QueuePool",
    "memory": r"out of memory|\boom|memory limit|memory leak|\bheap\b",
    "cpu": r"cpu throttl|cpu limit|cpu saturat|\bcfs\b|cpu (usage|spike)",
    "dns": r"\bdns\b|nxdomain|name resolution|coredns",
    "image": r"imagepull|image pull|registry|manifest unknown",
    "config": r"configmap|secret ref|missing (env|config)|environment variable|config(uration)? error|createcontainerconfigerror",
    "probe": r"readiness probe|liveness probe|probe fail",
    "deployment": r"rollback|rollout|regression|bad (release|deploy)|canary",
    "dependency": r"upstream|downstream|dependency|connection refused|timeout calling",
    "scheduling": r"unschedulable|insufficient (cpu|memory)|node affinity|taint",
    "node": r"node pressure|diskpressure|memorypressure|evict",
    "traffic": r"traffic (spike|surge)|load (spike|test)|request rate",
}.items()}

# Log line patterns, named for the condition they show.
LOG_PATTERNS: dict[str, re.Pattern] = {k: re.compile(v, re.I) for k, v in {
    "log.db_pool_exhausted": r"connection pool (is )?(exhausted|full)|pool exhausted|timeout (waiting for|acquiring) (a )?connection|could not (obtain|acquire|get) (a )?(db |database )?connection|"
                             r"too many (clients|connections)|remaining connection slots|hikaripool.*(timeout|not available)|QueuePool limit|max_connections",
    "log.db_timeout": r"statement timeout|query (timed out|timeout)|(database|db|sql|postgres).{0,40}(timed? ?out|timeout)|deadlock detected|canceling statement",
    "log.oom": r"out of memory|outofmemoryerror|java heap space|killed process|cannot allocate memory|memory limit exceeded",
    "log.dns_failure": r"no such host|nxdomain|could not resolve|temporary failure in name resolution|name or service not known|server misbehaving",
    "log.connection_refused": r"connection refused|econnrefused|connect: connection reset|no route to host",
    "log.upstream_timeout": r"context deadline exceeded|upstream (request )?timeout|gateway timeout|\b504\b|i/o timeout|read timeout",
    "log.tls_error": r"x509|certificate (has )?expired|tls handshake|certificate verify failed",
    "log.config_error": r"(is not set|must be set|missing required|required (env|environment|config)|invalid configuration|config(uration)? (error|invalid)|no such file or directory)",
    "log.exception": r"traceback \(most recent call last\)|\bexception\b|\bpanic\b|\bfatal\b|unhandled",
    "log.http_5xx": r"(status|code)[\"'=: ]+5\d\d\b|\bhttp/\d\.\d\" 5\d\d\b",
}.items()}

LATENCY_FLOOR_SECONDS = 0.1   # a p95 below this is fast whatever it rose from
ERROR_RATIO_FLOOR = 0.02
INCREASE = 1.5                # late vs early mean
SURGE = 2.0
MEMORY_NEAR_LIMIT = 0.9
MEMORY_COMFORTABLE = 0.7
THROTTLING = 0.25
THROTTLING_LOW = 0.05
DB_SATURATED = 0.9


@dataclass(frozen=True)
class Signal:
    key: str
    ref: str
    source_type: str
    present: bool = True
    at: datetime | None = None
    value: float | None = None
    detail: str = ""
    resource: str | None = None

    def out(self) -> dict[str, Any]:
        return {"key": self.key, "ref": self.ref, "source_type": self.source_type, "present": self.present, "at": self.at.isoformat() if self.at else None,
                "value": self.value, "detail": self.detail, "resource": self.resource}


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _clip(text: Any, n: int = 200) -> str:
    return redact(re.sub(r"\s+", " ", str(text or ""))).strip()[:n]


@dataclass(frozen=True)
class Change:
    early: float
    late: float
    ratio: float | None
    onset: datetime | None
    peak: float


def change(values: list[list[Any]]) -> Change | None:
    """Early (first 40%) against late (last 30%) of a range series, and the first point after the early part that
    left the early band (mean + max(3 sd, 50%)). None with fewer than 6 usable points."""
    pts = [(t, float(v)) for t, v in values if v is not None]
    if len(pts) < 6:
        return None
    n = len(pts)
    early = [v for _, v in pts[: max(2, int(n * 0.4))]]
    late = [v for _, v in pts[n - max(2, int(n * 0.3)):]]
    e_mean, l_mean = statistics.fmean(early), statistics.fmean(late)
    band = e_mean + max(3 * (statistics.pstdev(early) if len(early) > 1 else 0), abs(e_mean) * 0.5, 1e-9)
    onset = next((_ts(t) for t, v in pts[len(early):] if v > band), None)
    ratio = (l_mean / e_mean) if e_mean > 0 else (None if l_mean == 0 else float("inf"))
    return Change(e_mean, l_mean, ratio, onset, max(v for _, v in pts))


def _series(result: Any) -> list[dict]:
    return [s for s in (result or {}).get("series") or [] if isinstance(s, dict) and s.get("values")] if isinstance(result, dict) else []


def _mine(target: Any, pod: str | None) -> bool:
    pods = getattr(target, "pods", None)
    if target is None or pod is None:
        return True
    if pods:
        return pod in pods
    return workload_of(pod) == getattr(target, "workload", None)


def _fmt(v: float) -> str:
    return f"{v:.3g}"


# ---- per tool -----------------------------------------------------------------------------------------

def _pod_problems(ref: str, st: str, pods: Iterable[dict], target: Any) -> list[Signal]:
    out: list[Signal] = []
    seen_pods = 0
    restarted = 0
    for p in pods:
        if not isinstance(p, dict) or not _mine(target, p.get("name")):
            continue
        seen_pods += 1
        res = f"pod/{p.get('name')}"
        if (p.get("restarts") or 0) > 0:
            restarted += 1
            out.append(Signal("k8s.restarts", ref, st, value=float(p["restarts"]), detail=f"{p.get('name')} restarted {p['restarts']} time(s)", resource=res))
        for pr in p.get("problems") or []:
            reason = str(pr.get("reason") or "")
            at = _ts(pr.get("finishedAt"))
            if reason == "CrashLoopBackOff":
                out.append(Signal("k8s.crashloop", ref, st, detail=f"{p.get('name')} is in CrashLoopBackOff", resource=res))
            elif reason == "OOMKilled":
                out.append(Signal("k8s.oomkilled", ref, st, at=at, detail=f"{p.get('name')} container {pr.get('container')} was OOMKilled (exit {pr.get('exitCode')})", resource=res))
            elif reason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
                out.append(Signal("k8s.image_pull", ref, st, detail=f"{p.get('name')} cannot pull its image ({reason})", resource=res))
            elif reason in ("CreateContainerConfigError", "CreateContainerError"):
                out.append(Signal("k8s.config_error", ref, st, detail=f"{p.get('name')}: {reason} {_clip(pr.get('message'), 120)}", resource=res))
            elif pr.get("state") == "unschedulable":
                out.append(Signal("k8s.unschedulable", ref, st, detail=f"{p.get('name')} cannot be scheduled: {_clip(pr.get('message'), 120)}", resource=res))
            elif reason == "Evicted":
                out.append(Signal("k8s.evicted", ref, st, detail=f"{p.get('name')} was evicted", resource=res))
        if p.get("ready") and "/" in str(p["ready"]) and str(p["ready"]).split("/")[0] != str(p["ready"]).split("/")[1] and p.get("phase") == "Running":
            out.append(Signal("k8s.not_ready", ref, st, detail=f"{p.get('name')} is Running but not Ready ({p['ready']})", resource=res))
    if seen_pods and not restarted:
        out.append(Signal("k8s.restarts", ref, st, present=False, detail=f"none of {seen_pods} pod(s) restarted"))
    if seen_pods and not any(s.key == "k8s.crashloop" for s in out):
        out.append(Signal("k8s.crashloop", ref, st, present=False, detail=f"no pod of {seen_pods} is crash-looping"))
    if seen_pods and not any(s.key == "k8s.oomkilled" for s in out):
        out.append(Signal("k8s.oomkilled", ref, st, present=False, detail=f"no OOMKilled container in {seen_pods} pod(s)"))
    return out


FINDING_SIGNALS = {"crashloop": "k8s.crashloop", "oomkilled": "k8s.oomkilled", "oomkilled_earlier": "k8s.oomkilled_earlier", "image_pull": "k8s.image_pull",
                   "config_error": "k8s.config_error", "unschedulable": "k8s.unschedulable", "not_ready": "k8s.not_ready", "recent_restarts": "k8s.restarts",
                   "service_selector_mismatch": "k8s.service_selector_mismatch", "service_no_ready_endpoints": "k8s.service_no_ready_endpoints",
                   "service_target_port": "k8s.service_target_port", "workload_unavailable": "k8s.workload_unavailable", "evicted": "k8s.evicted", "pending": "k8s.pending",
                   "ingress_backend_missing": "k8s.ingress_backend_missing", "event_FailedScheduling": "k8s.unschedulable", "event_Unhealthy": "k8s.probe_failed",
                   "event_BackOff": "k8s.crashloop", "event_FailedMount": "k8s.mount_failed", "event_Evicted": "k8s.evicted", "event_OOMKilling": "k8s.oomkilled"}
EVENT_SIGNALS = {"Unhealthy": "k8s.probe_failed", "BackOff": "k8s.crashloop", "FailedScheduling": "k8s.unschedulable", "Evicted": "k8s.evicted", "OOMKilling": "k8s.oomkilled",
                 "FailedMount": "k8s.mount_failed", "Failed": "k8s.container_failed", "ScalingReplicaSet": "deploy.scaled", "FailedCreate": "k8s.create_failed"}


def _finding_mine(target: Any, f: dict) -> bool:
    if target is None:
        return True
    names = {f"pod/{p}" for p in getattr(target, "pods", ()) or ()} | {f"{getattr(target, 'kind', 'Deployment')}/{target.workload}", f"{str(getattr(target, 'kind', 'deployment')).lower()}/{target.workload}"}
    if str(f.get("owner") or f.get("controller") or "") in names:
        return True
    return any(str(r) in names or (str(r).startswith("pod/") and _mine(target, str(r)[4:])) for r in f.get("resources") or [])


def _triage(ref: str, st: str, data: dict, target: Any) -> list[Signal]:
    out = [Signal(FINDING_SIGNALS[f["kind"]], ref, st, detail=_clip(f.get("title")), resource=(f.get("resources") or [None])[0])
           for f in data.get("findings") or [] if isinstance(f, dict) and f.get("kind") in FINDING_SIGNALS and _finding_mine(target, f)]
    if not out and target is not None:
        out.append(Signal("k8s.unhealthy", ref, st, present=False, detail=f"triage found nothing wrong with {target.workload}"))
    return out


def _events(ref: str, st: str, data: list, target: Any) -> list[Signal]:
    out = []
    for e in data:
        if not isinstance(e, dict) or e.get("reason") not in EVENT_SIGNALS:
            continue
        obj = str(e.get("object") or "")
        if target is not None and obj.startswith("Pod/") and not _mine(target, obj[4:]):
            continue
        key = EVENT_SIGNALS[e["reason"]]
        if key == "deploy.scaled":
            continue
        out.append(Signal(key, ref, st, at=_ts(e.get("firstSeen") or e.get("lastSeen")), value=float(e.get("count") or 1),
                          detail=f"{e.get('count') or 1}x {e['reason']} on {obj}: {_clip(e.get('message'), 140)}", resource=obj.lower() or None))
    return out


def _services(ref: str, st: str, data: list, target: Any) -> list[Signal]:
    """A Service the target calls (or the target's own) with nothing ready behind it."""
    deps = set(getattr(target, "dependencies", ()) or ()) | {getattr(target, "service", None)} if target is not None else None
    out = []
    for s in data:
        if not isinstance(s, dict) or not s.get("selector") or (deps is not None and s.get("name") not in deps):
            continue
        if not (s.get("endpoints") or {}).get("ready"):
            out.append(Signal("k8s.service_no_ready_endpoints", ref, st, detail=f"Service {s.get('name')} has no ready endpoints ({s.get('matchingPodCount', 0)} matching pod(s))",
                              resource=f"service/{s.get('name')}"))
    return out


def _rollouts(ref: str, st: str, data: dict, window: tuple[datetime, datetime]) -> list[Signal]:
    revs = sorted([r for r in data.get("revisions") or [] if isinstance(r, dict)], key=lambda r: -(r.get("revision") or 0))
    out = []
    for i, r in enumerate(revs):
        at = _ts(r.get("created"))
        if at is None or not window[0] <= at <= window[1]:
            continue
        prev = revs[i + 1] if i + 1 < len(revs) else None
        imgs, before = r.get("images") or [], (prev or {}).get("images") or []
        out.append(Signal("deploy.rollout", ref, st, at=at, value=float(r.get("revision") or 0), detail=f"revision {r.get('revision')} rolled out ({', '.join(imgs)[:160]})",
                          resource=f"replicaset/{r.get('replicaSet')}"))
        if prev and imgs != before:
            out.append(Signal("deploy.image_change", ref, st, at=at, detail=f"image changed from {', '.join(before)[:80]} to {', '.join(imgs)[:80]}"))
    if not out and revs:
        out.append(Signal("deploy.rollout", ref, st, present=False, detail=f"no rollout inside the window (latest revision {revs[0].get('revision')} at {revs[0].get('created')})"))
    return out


def _topics(text: str) -> list[str]:
    return [k for k, rx in TOPICS.items() if rx.search(text or "")]


def _commits(ref: str, st: str, data: list, window: tuple[datetime, datetime]) -> list[Signal]:
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        at = _ts(c.get("date") or c.get("committed_at") or c.get("authored_at"))
        msg = str(c.get("message") or "").splitlines()[0] if c.get("message") else ""
        sha = str(c.get("sha") or "")[:10]
        out.append(Signal("git.commit", ref, st, at=at, detail=f"{sha} {_clip(msg, 140)}"))
        for t in _topics(f"{msg} {' '.join(map(str, c.get('files') or []))}"):
            out.append(Signal(f"git.topic:{t}", ref, st, at=at, detail=f"{sha} {_clip(msg, 140)}"))
    if not out:
        out.append(Signal("git.commit", ref, st, present=False, detail="no commit in the window"))
    return out


def _audit(ref: str, st: str, data: dict, target: Any) -> list[Signal]:
    out = []
    for w in data.get("workloads") or []:
        name = str(w.get("workload") or "")
        if target is not None and name.split("/", 1)[-1] != target.workload:
            continue
        for i in w.get("issues") or []:
            kind = {"missing_memory_limit": "config.no_memory_limit", "missing_readiness_probe": "config.no_readiness_probe"}.get(i.get("kind"))
            if kind:
                out.append(Signal(kind, ref, st, detail=_clip(i.get("detail")), resource=name.lower()))
    for m in data.get("missing_references") or []:
        out.append(Signal("config.missing_ref", ref, st, detail=_clip(m if isinstance(m, str) else f"{m.get('kind')} {m.get('name')} is referenced but missing")))
    return out


def _resources(ref: str, st: str, data: dict) -> list[Signal]:
    res = data.get("results") or {}
    out: list[Signal] = []
    limits = {(s.get("metric") or {}).get("pod"): max((v for _, v in s["values"] if v is not None), default=None) for s in _series(res.get("memory_limit_bytes"))}
    worst = None
    for s in _series(res.get("memory_working_set_bytes")):
        pod = (s.get("metric") or {}).get("pod")
        lim = limits.get(pod)
        peak = max((v for _, v in s["values"] if v is not None), default=None)
        if lim and peak is not None and lim > 0:
            ratio = peak / lim
            at = next((_ts(t) for t, v in s["values"] if v is not None and v / lim >= MEMORY_NEAR_LIMIT), None)
            if worst is None or ratio > worst[0]:
                worst = (ratio, pod, at)
    if worst:
        if worst[0] >= MEMORY_NEAR_LIMIT:
            out.append(Signal("metric.memory_near_limit", ref, st, at=worst[2], value=round(worst[0], 3), detail=f"{worst[1]} peaked at {worst[0]:.0%} of its memory limit", resource=f"pod/{worst[1]}"))
        elif worst[0] < MEMORY_COMFORTABLE:
            out.append(Signal("metric.memory_near_limit", ref, st, present=False, value=round(worst[0], 3), detail=f"memory peaked at {worst[0]:.0%} of the limit"))
    thr = [(max((v for _, v in s["values"] if v is not None), default=0), s) for s in _series(res.get("cpu_throttled_ratio"))]
    if thr:
        top, s = max(thr, key=lambda x: x[0])
        pod = (s.get("metric") or {}).get("pod")
        if top >= THROTTLING:
            at = next((_ts(t) for t, v in s["values"] if v is not None and v >= THROTTLING), None)
            out.append(Signal("metric.cpu_throttling", ref, st, at=at, value=round(top, 3), detail=f"{pod} was CPU-throttled in up to {top:.0%} of periods", resource=f"pod/{pod}"))
        elif top < THROTTLING_LOW:
            out.append(Signal("metric.cpu_throttling", ref, st, present=False, value=round(top, 3), detail=f"CPU throttling stayed below {THROTTLING_LOW:.0%}"))
    changes = [(change(s["values"]), s) for s in _series(res.get("cpu_cores"))]
    changes = [(c, s) for c, s in changes if c is not None and c.ratio is not None]
    if changes:
        c, s = max(changes, key=lambda x: x[0].ratio or 0)
        pod = (s.get("metric") or {}).get("pod")
        if (c.ratio or 0) >= INCREASE:
            out.append(Signal("metric.cpu_increase", ref, st, at=c.onset, value=round(c.ratio, 2) if c.ratio != float("inf") else None,
                              detail=f"{pod} CPU rose from {_fmt(c.early)} to {_fmt(c.late)} cores", resource=f"pod/{pod}"))
        elif (c.ratio or 0) <= 1.1:
            out.append(Signal("metric.cpu_increase", ref, st, present=False, detail=f"CPU steady ({_fmt(c.early)} to {_fmt(c.late)} cores)"))
    return out


def _service_signals(ref: str, st: str, data: dict) -> list[Signal]:
    """prom_golden_signals: one series per signal (tools/intelligence.py)."""
    out: list[Signal] = []
    sig = data.get("signals") or {}

    def first(name: str) -> tuple[Change | None, dict | None]:
        series = _series(sig.get(name))
        best = max(((change(s["values"]), s) for s in series), key=lambda x: (x[0].late if x[0] else -1), default=(None, None))
        return best

    lat, _ = first("latency_p95")
    if lat is not None:
        if (lat.ratio or 0) >= INCREASE and lat.late >= LATENCY_FLOOR_SECONDS:
            out.append(Signal("metric.latency_increase", ref, st, at=lat.onset, value=round(lat.late, 4), detail=f"p95 latency rose from {_fmt(lat.early)}s to {_fmt(lat.late)}s"))
        elif (lat.ratio or 0) <= 1.15:
            out.append(Signal("metric.latency_increase", ref, st, present=False, detail=f"p95 latency steady at about {_fmt(lat.late)}s"))
    err, _ = first("error_ratio")
    if err is not None:
        if err.late >= ERROR_RATIO_FLOOR and (err.early == 0 or (err.ratio or 0) >= SURGE):
            out.append(Signal("metric.error_rate_increase", ref, st, at=err.onset, value=round(err.late, 4), detail=f"error ratio rose from {err.early:.2%} to {err.late:.2%}"))
        elif err.late < ERROR_RATIO_FLOOR / 2:
            out.append(Signal("metric.error_rate_increase", ref, st, present=False, detail=f"error ratio stayed at {err.late:.2%}"))
    rps, _ = first("request_rate")
    if rps is not None and rps.ratio is not None:
        if rps.ratio >= SURGE:
            out.append(Signal("metric.traffic_surge", ref, st, at=rps.onset, value=round(rps.ratio, 2) if rps.ratio != float("inf") else None,
                              detail=f"request rate rose from {_fmt(rps.early)} to {_fmt(rps.late)}/s"))
        elif rps.ratio <= 0.5:
            out.append(Signal("metric.traffic_drop", ref, st, value=round(rps.ratio, 2), detail=f"request rate fell from {_fmt(rps.early)} to {_fmt(rps.late)}/s"))
        else:
            out.append(Signal("metric.traffic_surge", ref, st, present=False, detail=f"request rate steady ({_fmt(rps.early)} to {_fmt(rps.late)}/s)"))
    db, _ = first("db_connections")
    if db is not None:
        cap = max((v for s in _series(sig.get("db_connections_max")) for _, v in s["values"] if v is not None), default=None)
        if cap and db.peak / cap >= DB_SATURATED:
            out.append(Signal("metric.db_connections_saturated", ref, st, at=db.onset, value=round(db.peak / cap, 3), detail=f"database connections reached {db.peak:.0f} of {cap:.0f}"))
        if (db.ratio or 0) >= INCREASE:
            out.append(Signal("metric.db_connections_increase", ref, st, at=db.onset, value=round(db.late, 1), detail=f"database connections rose from {db.early:.0f} to {db.late:.0f}"))
        elif (db.ratio or 0) <= 1.15 and not (cap and db.peak / cap >= DB_SATURATED):
            out.append(Signal("metric.db_connections_increase", ref, st, present=False, detail=f"database connections steady at about {db.late:.0f}"))
            out.append(Signal("metric.db_connections_saturated", ref, st, present=False, detail="database connections below the pool's limit"))
    return out


def _lines(data: Any) -> list[tuple[datetime | None, str, dict]]:
    if isinstance(data, str):
        return [(None, line, {}) for line in data.splitlines() if line.strip()]
    if isinstance(data, dict) and isinstance(data.get("lines"), list):
        return [(_ts(r.get("ts")), str(r.get("line") or ""), r.get("labels") or {}) for r in data["lines"] if isinstance(r, dict)]
    return []


def _logs(ref: str, st: str, data: Any, args: dict, target: Any) -> list[Signal]:
    lines = [(t, line, labels) for t, line, labels in _lines(data) if _mine(target, labels.get("pod") or args.get("pod"))]
    out: list[Signal] = []
    for key, rx in LOG_PATTERNS.items():
        hits = [(t, line, labels) for t, line, labels in lines if rx.search(line)]
        if hits:
            times = [t for t, _, _ in hits if t is not None]
            t0, line, labels = min(hits, key=lambda h: h[0] or datetime.max.replace(tzinfo=timezone.utc))
            pod = labels.get("pod") or args.get("pod")
            out.append(Signal(key, ref, st, at=min(times) if times else None, value=float(len(hits)), detail=f"{len(hits)} line(s), first: {_clip(line, 160)}",
                              resource=f"pod/{pod}" if pod else None))
    if not lines and args.get("errors_only"):
        out.append(Signal("log.errors", ref, st, present=False, detail="no error lines in the window"))
    elif lines and args.get("errors_only"):
        out.append(Signal("log.errors", ref, st, value=float(len(lines)), at=min((t for t, _, _ in lines if t), default=None), detail=f"{len(lines)} error line(s)"))
    return out


def _trace_correlation(ref: str, st: str, data: Any, args: dict, target: Any = None) -> list[Signal]:
    lines = sorted(_lines(data), key=lambda x: x[0] or datetime.max.replace(tzinfo=timezone.utc))
    if not lines:
        return []
    services = list(dict.fromkeys(workload_of(str(lb.get("pod") or lb.get("app") or "?")) for _, _, lb in lines))
    erring = next(((t, line, lb) for t, line, lb in lines if LOG_PATTERNS["log.exception"].search(line) or re.search(r"(?i)\berror\b", line)), None)
    out = [Signal("trace.cross_service", ref, st, value=float(len(services)), detail=f"trace {str(args.get('trace_id'))[:12]}… seen in {', '.join(services[:5])}")] if len(services) > 1 else []
    if erring:
        svc = workload_of(str(erring[2].get("pod") or erring[2].get("app") or "?"))
        out.append(Signal("trace.error", ref, st, at=erring[0], detail=f"first error on trace {str(args.get('trace_id'))[:12]}… in {svc}: {_clip(erring[1], 120)}", resource=svc))
        if target is not None and svc not in (getattr(target, "workload", None), getattr(target, "service", None)):
            out.append(Signal("trace.dependency_error", ref, st, at=erring[0], detail=f"the failing request's first error is in {svc}, not {target.workload}", resource=svc))
    return out


def _traces(ref: str, st: str, data: Any) -> list[Signal]:
    """A trace backend's spans (any tool named trace_*): {service, operation, duration_ms, error} per span."""
    spans = [s for t in (data if isinstance(data, list) else (data or {}).get("traces") or []) for s in ((t.get("spans") if isinstance(t, dict) else None) or [])]
    out = []
    for s in spans:
        if s.get("error"):
            out.append(Signal("trace.error", ref, st, at=_ts(s.get("start")), detail=f"error span {s.get('service')}/{s.get('operation')}", resource=s.get("service")))
    slow = sorted((s for s in spans if isinstance(s.get("duration_ms"), (int, float))), key=lambda s: -s["duration_ms"])[:1]
    for s in slow:
        out.append(Signal("trace.slow_span", ref, st, at=_ts(s.get("start")), value=float(s["duration_ms"]), detail=f"slowest span {s.get('service')}/{s.get('operation')} {s['duration_ms']:.0f} ms",
                          resource=s.get("service")))
    return out


def _anomalies(ref: str, st: str, data: list, target: Any) -> list[Signal]:
    out = []
    for o in data:
        if not isinstance(o, dict) or o.get("status") != "scored":
            continue
        svc = o.get("service")
        if target is not None and svc and svc != target.workload and svc not in getattr(target, "dependencies", ()):
            continue
        at = _ts(o.get("window_to"))
        if not o.get("unusual"):
            out.append(Signal("ml.anomaly", ref, st, present=False, at=at, detail=f"{svc}: window scored as usual ({o.get('mode')} model v{o.get('model_version')})", resource=svc))
            continue
        out.append(Signal("ml.anomaly", ref, st, at=at, value=o.get("score"), detail=f"{svc}: unusual window, score {o.get('score')} over threshold {o.get('threshold')} ({o.get('mode')})", resource=svc))
        for d in (o.get("deviations") or [])[:4]:
            feat, z = d.get("feature"), d.get("z") if d.get("z") is not None else d.get("deviation")
            if feat:
                direction = "up" if (z or 0) >= 0 else "down"
                out.append(Signal(f"ml.anomaly:{feat}", ref, st, at=at, value=z if isinstance(z, (int, float)) else None,
                                  detail=f"{svc}: {feat} {direction} (observed {d.get('observed')}, baseline {d.get('baseline')})", resource=svc))
    return out


def _knowledge(ref: str, st: str, data: list) -> list[Signal]:
    out = []
    for h in data:
        if not isinstance(h, dict):
            continue
        text = f"{h.get('title') or ''} {h.get('heading') or ''} {str(h.get('text') or '')[:2000]}"
        cite = (h.get("citation") or {}).get("path") or h.get("title") or "passage"
        for t in _topics(text):
            out.append(Signal(f"kb.topic:{t}", ref, st, value=float(h.get("score") or 0), detail=f"{_clip(cite, 120)}"))
    return out


def _similar(ref: str, st: str, data: list) -> list[Signal]:
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        cause = str((r.get("resolution") or {}).get("cause") or "")
        score = float(r.get("score") or 0)
        title = r.get("title") or cause or r.get("incident_id") or "an approved incident"
        out.append(Signal("history.similar", ref, st, value=score, detail=f"{_clip(title, 100)} (similarity {score:.2f})"))
        for t in _topics(f"{cause} {r.get('title') or ''}"):
            out.append(Signal(f"history.cause:{t}", ref, st, value=score, detail=f"{_clip(cause, 160)} (similarity {score:.2f})"))
    return out


def _alerts(ref: str, st: str, data: list, target: Any) -> list[Signal]:
    out = []
    for rule in data:
        for a in rule.get("active") or [] if isinstance(rule, dict) else []:
            if a.get("state") != "firing":
                continue
            labels = a.get("labels") or {}
            if target is not None and labels.get("namespace") not in (None, target.namespace):
                continue
            name = labels.get("alertname") or rule.get("name")
            out.append(Signal("alert.firing", ref, st, at=_ts(a.get("activeAt")), detail=f"{name} ({labels.get('severity', 'no severity')}) firing", resource=labels.get("pod") or labels.get("service")))
    return out


def _nodes(ref: str, st: str, data: list) -> list[Signal]:
    bad = [n for n in data if isinstance(n, dict) and (n.get("pressure") or not n.get("ready"))]
    if not bad:
        return [Signal("node.pressure", ref, st, present=False, detail=f"no pressure on {len(data)} node(s)")]
    return [Signal("node.pressure", ref, st, detail=f"{n.get('name')}: {', '.join(n.get('pressure') or []) or 'not ready'}", resource=f"node/{n.get('name')}") for n in bad]


def _dns(ref: str, st: str, data: dict) -> list[Signal]:
    pods = data.get("pods") or []
    ready = [p for p in pods if str(p.get("ready", "0/1")).split("/")[0] == str(p.get("ready", "0/1")).split("/")[-1] and p.get("phase") == "Running"]
    return [Signal("dns.unhealthy", ref, st, present=len(ready) < len(pods) or not pods, detail=f"CoreDNS {len(ready)}/{len(pods)} pods ready")]


def extract(record: dict[str, Any], target: Any = None, window: tuple[datetime, datetime] | None = None) -> list[Signal]:
    """Signals from one Investigator.evidence record. Failed and refused reads give none."""
    if not record.get("ok"):
        return []
    tool, args, data, ref = record["tool"], record.get("args") or {}, record.get("data"), record["ref"]
    st = source_type(tool, args)
    window = window or (datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc))
    try:
        if tool == "k8s_triage" and isinstance(data, dict):
            return _triage(ref, st, data, target)
        if tool == "k8s_list_pods" and isinstance(data, list):
            return _pod_problems(ref, st, data, target)
        if tool == "k8s_describe_pod" and isinstance(data, dict):
            return _pod_problems(ref, st, [data.get("summary") or {}], target) + _events(ref, st, data.get("events") or [], target)
        if tool == "k8s_events" and isinstance(data, list):
            return _events(ref, st, data, target)
        if tool == "k8s_services" and isinstance(data, list):
            return _services(ref, st, data, target)
        if tool == "k8s_rollout_history" and isinstance(data, dict):
            return _rollouts(ref, st, data, window)
        if tool == "git_recent_commits" and isinstance(data, list):
            return _commits(ref, st, data, window)
        if tool == "git_compare_deployment" and isinstance(data, dict) and data.get("resolved") and isinstance(data.get("deployed_commit"), dict):
            return [Signal("deploy.commit_resolved", ref, st, detail=f"running commit {str(data['deployed_commit'].get('sha'))[:12]} ({data.get('source')})")]
        if tool == "k8s_config_audit" and isinstance(data, dict):
            return _audit(ref, st, data, target)
        if tool == "prom_pod_resources" and isinstance(data, dict):
            return _resources(ref, st, data)
        if tool == "prom_golden_signals" and isinstance(data, dict):
            return _service_signals(ref, st, data)
        if tool == "loki_search" and args.get("trace_id"):
            return _trace_correlation(ref, st, data, args, target)
        if tool in ("loki_search", "loki_logs", "loki_query", "k8s_pod_logs"):
            return _logs(ref, st, data, args, target)
        if tool.startswith("trace_"):
            return _traces(ref, st, data)
        if tool == "ml_observations" and isinstance(data, list):
            return _anomalies(ref, st, data, target)
        if tool == "kb_search" and isinstance(data, list):
            return _knowledge(ref, st, data)
        if tool == "similar_incidents" and isinstance(data, list):
            return _similar(ref, st, data)
        if tool == "prom_alerts" and isinstance(data, list):
            return _alerts(ref, st, data, target)
        if tool == "k8s_nodes" and isinstance(data, list):
            return _nodes(ref, st, data)
        if tool == "k8s_dns_health" and isinstance(data, dict):
            return _dns(ref, st, data)
    except (TypeError, ValueError, KeyError, AttributeError, ZeroDivisionError):
        return []  # an unexpected shape yields nothing rather than a guess
    return []


def trace_ids(record: dict[str, Any], limit: int = 2) -> list[str]:
    """Trace ids on the error lines of a log read, newest first: what the trace stage follows."""
    found: list[str] = []
    for _, line, _ in reversed(_lines(record.get("data"))):
        for m in TRACE_ID.findall(line):
            if m.lower() not in found:
                found.append(m.lower())
        if len(found) >= limit:
            break
    return found[:limit]


def log_services(record: dict[str, Any]) -> list[str]:
    """Hosts a workload's logs say it could not reach (connection refused, DNS): dependency candidates."""
    out: list[str] = []
    for _, line, _ in _lines(record.get("data")):
        for m in re.finditer(r"(?i)(?:dial tcp|connect(?:ing)? to|lookup|host)\s+([a-z0-9][a-z0-9-]{0,62})(?:\.[a-z0-9-]+)*(?::\d+)?", line):
            name = m.group(1).lower()
            if name not in out and name not in ("localhost", "the", "a"):
                out.append(name)
    return out[:5]
