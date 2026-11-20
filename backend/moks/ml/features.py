"""Metric features for anomaly detection, on one fixed grid for training, validation and inference alike.

A cohort is one workload in one namespace of one environment (`env/namespace/workload`), or one node
(`env/_node/<node>`, the infrastructure level). Pods come and go with every rollout, so series are aggregated to
the workload in PromQL (label_replace strips the ReplicaSet and pod suffixes) and pod churn never creates a new
cohort. Every feature is sampled at the same step and aligned to multiples of it.

The catalog (feature schema metrics-anomaly/2) is below, each feature with its unit, level, inputs and what a
missing value means. Traffic features (requests, errors, latency) have two sources: the application's own
metrics (`app`, Prometheus client conventions, names configurable) and Istio's sidecar telemetry (`mesh`,
istio_requests_total, reporter="destination"). Training pins one source per cohort, the one with at least half
coverage, preferring the application's; inference reads only the pinned source, so a model is never scored on a
different measurement of the same thing than it learned.

Sampling intervals. Exporters are scraped at different intervals and some publish rarely. Counters and histograms
use rate() over one window (`rate_window`, default 300 s) for every feature, and gauges are summarized over the
step (avg/max_over_time), so each grid point covers the whole step whatever the native interval is. Before the
chunked reads, one probe per base metric measures its native interval (the median number of samples per series in
the last hour). A counter whose interval is more than a quarter of the rate window is `undersampled`: its rate is
mostly extrapolation, so the feature is reported and not used. A gauge sampled less often than the step simply has
gaps, which stay gaps. A base metric with no series in the probe is `absent` and is not queried at all.

Absent telemetry is not healthy behavior: a feature with no series is `absent`, a gap is `missing`, and both are
counted in data quality instead of being filled with zeros. Rates use PromQL's counter-reset handling; the restart
counter is read raw and a decrease is treated as a reset.

Completeness is tracked per query and per chunk: `success`, `failed` (an error, a timeout, a Prometheus status
other than success), `partial` (warnings, a truncated result, the series cap) or `absent` (not queried, because
the probe found no series). A missing sample in a failed or partial chunk is unknown. It becomes zero only where
the metric's definition says absence means zero, and only inside a chunk that succeeded completely.

Provenance. Every frame says, per feature, which source and raw queries produced it, the exact PromQL, the native
interval, the rate window, the chunk outcomes and the coverage. Models keep it with their training record.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

FEATURE_SCHEMA = "metrics-anomaly/2"
LEGACY_SCHEMAS = ("metrics-anomaly/1",)
SPARSE_RPS = 0.05          # below this, an error ratio says more about chance than about the service
CHUNK_POINTS = 144         # under the connector's 240-point cap, so nothing is downsampled
SERIES_CAP = 25            # the connector's MAX_SERIES
PROBE_SECONDS = 3600       # the probe counts samples over the last hour
RATE_WINDOW = 300
NODE_NAMESPACE = "_node"   # not a valid Kubernetes namespace name, so it can never collide with one
NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
NODE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
METRIC = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


# ---- the catalog ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    unit: str
    level: str              # service | resource | infrastructure
    inputs: tuple[str, ...]  # raw query names it is derived from
    describe: str
    missing: str             # what a missing value means
    family: str | None = None  # "traffic": the inputs have an app and a mesh source
    origin: str = "cadvisor"   # the exporter behind it, for provenance (traffic features say app or mesh instead)
    direction: str = "higher"  # which way is a problem: higher, or both (a fall in traffic is as telling as a rise)


WORKLOAD_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("request_rate", "req/s", "service", ("request_rate",), "requests per second", "no request metric", "traffic", direction="both"),
    FeatureSpec("error_ratio", "ratio", "service", ("request_rate", "errors_rate"), "5xx share of requests",
                f"no request metric, under {SPARSE_RPS} req/s, or the error query was incomplete", "traffic"),
    FeatureSpec("latency_p50", "s", "service", ("latency_p50",), "median request duration", "no duration histogram or no traffic", "traffic"),
    FeatureSpec("latency_p95", "s", "service", ("latency_p95",), "95th percentile request duration", "no duration histogram or no traffic", "traffic"),
    FeatureSpec("latency_p99", "s", "service", ("latency_p99",), "99th percentile request duration", "no duration histogram or no traffic", "traffic"),
    FeatureSpec("cpu_usage", "cores", "resource", ("cpu_usage",), "CPU cores used by the workload's containers", "no cAdvisor series"),
    FeatureSpec("cpu_utilization", "ratio", "resource", ("cpu_utilization",), "CPU used over the CPU limit, for containers that have one",
                "no container has a CPU limit"),
    FeatureSpec("cpu_throttling", "ratio", "resource", ("cpu_throttling",), "throttled share of CFS periods", "no CFS period counters (no CPU limit)"),
    FeatureSpec("memory_working_set", "bytes", "resource", ("memory_working_set",), "working set, the memory the OOM killer counts", "no cAdvisor series"),
    FeatureSpec("memory_utilization", "ratio", "resource", ("memory_utilization",), "working set over the memory limit, for containers that have one",
                "no container has a memory limit"),
    FeatureSpec("restart_rate", "1/h", "resource", ("restarts",), "container restarts per hour, from the raw counter", "no kube-state-metrics", origin="kube-state-metrics"),
    FeatureSpec("network_errors", "1/s", "resource", ("network_errors",), "receive and transmit errors per second", "no cAdvisor network series"),
    FeatureSpec("queue_depth", "messages", "service", ("queue_depth",), "messages waiting (a configured gauge)", "no queue metric configured or exported", origin="application"),
    FeatureSpec("db_connections", "connections", "service", ("db_connections",), "database connections held (a configured gauge)",
                "no connection-pool metric configured or exported", origin="application", direction="both"),
)
NODE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("node_cpu_utilization", "ratio", "infrastructure", ("node_cpu",), "node CPU used over its cores (cAdvisor root cgroup)", "no cAdvisor root series"),
    FeatureSpec("node_memory_utilization", "ratio", "infrastructure", ("node_memory",), "node working set over its memory (cAdvisor root cgroup)", "no cAdvisor root series"),
    FeatureSpec("node_pressure", "flag", "infrastructure", ("node_pressure",), "1 when the node reports memory, disk or PID pressure", "no kube-state-metrics",
                origin="kube-state-metrics"),
)
FEATURES = tuple(s.name for s in WORKLOAD_SPECS)
NODE_FEATURES = tuple(s.name for s in NODE_SPECS)
SPECS = {s.name: s for s in (*WORKLOAD_SPECS, *NODE_SPECS)}
TRAFFIC = ("request_rate", "errors_rate", "latency_p50", "latency_p95", "latency_p99")
SOURCES = ("app", "mesh")


def raw_name(name: str, source: str) -> str:
    """The raw query that serves a traffic input from one source: request_rate (app) or mesh_request_rate (mesh)."""
    return f"mesh_{name}" if source == "mesh" and name in TRAFFIC else name


@dataclass(frozen=True)
class MetricDefinition:
    """What a query's silence means. absent_means_zero: a complete answer with no sample at an instant is a true
    zero, as with rate() over a 5xx selector that matches nothing when there are no errors."""
    absent_means_zero: bool = False
    kind: str = "gauge"  # counter | gauge | histogram: counters and histograms go through rate()


METRIC_DEFINITIONS: dict[str, MetricDefinition] = {
    "request_rate": MetricDefinition(kind="counter"), "mesh_request_rate": MetricDefinition(kind="counter"),
    "errors_rate": MetricDefinition(True, "counter"), "mesh_errors_rate": MetricDefinition(True, "counter"),  # no 5xx series means no 5xx responses
    **{f"{p}latency_{q}": MetricDefinition(kind="histogram") for p in ("", "mesh_") for q in ("p50", "p95", "p99")},
    "cpu_usage": MetricDefinition(kind="counter"), "cpu_utilization": MetricDefinition(kind="counter"), "cpu_throttling": MetricDefinition(kind="counter"),
    "memory_working_set": MetricDefinition(), "memory_utilization": MetricDefinition(),
    "restarts": MetricDefinition(),  # read raw: no kube-state-metrics is not zero restarts
    "network_errors": MetricDefinition(kind="counter"), "queue_depth": MetricDefinition(), "db_connections": MetricDefinition(),
    "node_cpu": MetricDefinition(kind="counter"), "node_memory": MetricDefinition(), "node_pressure": MetricDefinition(),
}

DEFAULT_METRICS = {"requests": "http_requests_total", "duration": "http_request_duration_seconds_bucket", "status_label": "code",
                   "restarts": "kube_pod_container_status_restarts_total", "queue_depth": "", "db_connections": ""}

# One series per workload: the pod name minus its ReplicaSet hash and pod suffix (or a StatefulSet ordinal).
WORKLOAD_REGEX = "^(.+?)(-[a-z0-9]{6,10}-[a-z0-9]{5}|-[0-9]+)$"
_WORKLOAD = 'label_replace({expr}, "workload", "$1", "pod", "' + WORKLOAD_REGEX.replace("{", "{{").replace("}", "}}") + '")'
_MESH = 'label_replace({expr}, "workload", "$1", "destination_workload", "(.+)")'


def _metrics(metrics: dict[str, str] | None) -> dict[str, str]:
    m = {**DEFAULT_METRICS, **{k: v for k, v in (metrics or {}).items() if v is not None}}
    for k in ("requests", "duration", "restarts", "queue_depth", "db_connections"):
        if m[k] and not METRIC.match(m[k]):
            raise ValueError(f"invalid metric name for {k}")
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", m["status_label"]):
        raise ValueError("invalid status label")
    return m


def _window(seconds: int) -> str:
    return f"{int(seconds)}s"


def queries(namespace: str, metrics: dict[str, str] | None = None, *, step: int = 300, rate_window: int = RATE_WINDOW) -> dict[str, str]:
    """PromQL per raw query, pinned to one namespace. Request metric names are configurable because they differ by
    application; the defaults are the Prometheus client conventions, Istio's telemetry and cAdvisor. Optional gauges
    (queue depth, database connections) are queried only when a metric name is configured."""
    if not NAME.match(namespace):
        raise ValueError(f"invalid namespace {namespace!r}")
    m = _metrics(metrics)
    rw, sw = _window(max(rate_window, step)), _window(step)
    sel = f'namespace="{namespace}"'
    cont = f'{sel}, container!="", container!="POD"'
    mesh = f'reporter="destination", destination_workload_namespace="{namespace}"'
    w = lambda e: f"sum by (workload) ({_WORKLOAD.format(expr=e)})"
    mw = lambda e: f"sum by (workload) ({_MESH.format(expr=e)})"
    hist = lambda q, e: f"histogram_quantile({q}, sum by (workload, le) ({_WORKLOAD.format(expr=e)}))"
    mhist = lambda q, e: f"histogram_quantile({q}, sum by (workload, le) ({_MESH.format(expr=e)})) / 1000"
    out = {
        "request_rate": w(f"rate({m['requests']}{{{sel}}}[{rw}])"),
        "errors_rate": w(f"rate({m['requests']}{{{sel}, {m['status_label']}=~\"5..\"}}[{rw}])"),
        **{f"latency_{q}": hist(f"0.{q[1:]}", f"rate({m['duration']}{{{sel}}}[{rw}])") for q in ("p50", "p95", "p99")},
        "mesh_request_rate": mw(f"rate(istio_requests_total{{{mesh}}}[{rw}])"),
        "mesh_errors_rate": mw(f"rate(istio_requests_total{{{mesh}, response_code=~\"5..\"}}[{rw}])"),
        **{f"mesh_latency_{q}": mhist(f"0.{q[1:]}", f"rate(istio_request_duration_milliseconds_bucket{{{mesh}}}[{rw}])") for q in ("p50", "p95", "p99")},
        "cpu_usage": w(f"rate(container_cpu_usage_seconds_total{{{cont}}}[{rw}])"),
        # Only containers with a limit, on both sides: a sidecar's limit must not become the app container's denominator.
        "cpu_utilization": f"{w(f'rate(container_cpu_usage_seconds_total{{{cont}}}[{rw}]) and on (namespace, pod, container) (container_spec_cpu_quota{{{cont}}} > 0)')} / "
                           f"{w(f'max_over_time(container_spec_cpu_quota{{{cont}}}[{sw}]) / max_over_time(container_spec_cpu_period{{{cont}}}[{sw}]) > 0')}",
        "cpu_throttling": f"{w(f'rate(container_cpu_cfs_throttled_periods_total{{{cont}}}[{rw}])')} / clamp_min({w(f'rate(container_cpu_cfs_periods_total{{{cont}}}[{rw}])')}, 1)",
        "memory_working_set": w(f"max_over_time(container_memory_working_set_bytes{{{cont}}}[{sw}])"),
        "memory_utilization": f"{w(f'max_over_time(container_memory_working_set_bytes{{{cont}}}[{sw}]) and on (namespace, pod, container) (container_spec_memory_limit_bytes{{{cont}}} > 0)')} / "
                              f"{w(f'max_over_time(container_spec_memory_limit_bytes{{{cont}}}[{sw}]) > 0')}",
        "restarts": w(f"{m['restarts']}{{{sel}}}"),
        "network_errors": w(f"rate(container_network_receive_errors_total{{{sel}}}[{rw}]) + rate(container_network_transmit_errors_total{{{sel}}}[{rw}])"),
    }
    if m["queue_depth"]:
        out["queue_depth"] = w(f"max_over_time({m['queue_depth']}{{{sel}}}[{sw}])")
    if m["db_connections"]:
        out["db_connections"] = w(f"max_over_time({m['db_connections']}{{{sel}}}[{sw}])")
    return out


def node_queries(*, step: int = 300, rate_window: int = RATE_WINDOW) -> dict[str, str]:
    """Cluster-scoped: one series per node, from the kubelet's cAdvisor root cgroup (id="/") and kube-state-metrics."""
    rw, sw = _window(max(rate_window, step)), _window(step)
    return {
        "node_cpu": f'sum by (node) (rate(container_cpu_usage_seconds_total{{id="/"}}[{rw}])) / max by (node) (machine_cpu_cores)',
        "node_memory": f'max by (node) (max_over_time(container_memory_working_set_bytes{{id="/"}}[{sw}])) / max by (node) (machine_memory_bytes)',
        "node_pressure": f'max by (node) (max_over_time(kube_node_status_condition{{condition=~"MemoryPressure|DiskPressure|PIDPressure", status="true"}}[{sw}]))',
    }


def _bases(namespace: str | None, metrics: dict[str, str] | None) -> dict[str, tuple[str, tuple[str, ...]]]:
    """base metric key -> (selector expression to probe, raw queries that depend on it)."""
    m = _metrics(metrics)
    if namespace is None:
        return {"cadvisor_root": ('container_memory_working_set_bytes{id="/"}', ("node_cpu", "node_memory")),
                "kube_node_status_condition": ('kube_node_status_condition{condition="Ready"}', ("node_pressure",))}
    sel = f'namespace="{namespace}"'
    cont = f'{sel}, container!="", container!="POD"'
    mesh = f'reporter="destination", destination_workload_namespace="{namespace}"'
    out = {
        "requests": (f"{m['requests']}{{{sel}}}", ("request_rate", "errors_rate")),
        "duration": (f"{m['duration']}{{{sel}}}", ("latency_p50", "latency_p95", "latency_p99")),
        "istio_requests_total": (f"istio_requests_total{{{mesh}}}", ("mesh_request_rate", "mesh_errors_rate")),
        "istio_request_duration": (f"istio_request_duration_milliseconds_bucket{{{mesh}}}", ("mesh_latency_p50", "mesh_latency_p95", "mesh_latency_p99")),
        "container_cpu_usage_seconds_total": (f"container_cpu_usage_seconds_total{{{cont}}}", ("cpu_usage", "cpu_utilization")),
        "container_cpu_cfs_periods_total": (f"container_cpu_cfs_periods_total{{{cont}}}", ("cpu_throttling",)),
        "container_memory_working_set_bytes": (f"container_memory_working_set_bytes{{{cont}}}", ("memory_working_set", "memory_utilization")),
        "restarts": (f"{m['restarts']}{{{sel}}}", ("restarts",)),
        "container_network_receive_errors_total": (f"container_network_receive_errors_total{{{sel}}}", ("network_errors",)),
    }
    for k in ("queue_depth", "db_connections"):
        if m[k]:
            out[k] = (f"{m[k]}{{{sel}}}", (k,))
    return out


def probe_query(selector: str) -> str:
    """Median samples per series over the last hour: 120 means one sample every 30 s."""
    return f"quantile(0.5, count_over_time({selector}[{PROBE_SECONDS}s]))"


def is_probe(promql: str) -> bool:
    return promql.startswith("quantile(0.5, count_over_time(")


def catalog_digest(metrics: dict[str, str] | None = None) -> str:
    """Changes whenever a feature's definition or query template changes: a model trained under another digest is
    not scored (stale_schema), because the same name would mean a different measurement."""
    body = {"schema": FEATURE_SCHEMA, "specs": [[s.name, s.unit, s.level, list(s.inputs), s.family, s.direction] for s in (*WORKLOAD_SPECS, *NODE_SPECS)],
            "queries": queries("ns", metrics), "nodes": node_queries(), "definitions": {k: [v.absent_means_zero, v.kind] for k, v in sorted(METRIC_DEFINITIONS.items())},
            "sparse_rps": SPARSE_RPS}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ---- the grid -------------------------------------------------------------------------------------------------


class WindowError(ValueError):
    """A window with no whole step in it: reported as `status`, never scored as 0% coverage."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class Window:
    start: int  # epoch seconds, a multiple of step, included
    end: int    # epoch seconds, a multiple of step, excluded
    step: int
    grid: list[int]
    status: str  # "ok" or "insufficient_window"
    reason: str = ""

    def request(self) -> tuple[datetime, datetime]:
        """The bounds to send to /api/v1/query_range. Prometheus answers start, start + step, ... up to and
        including end, so the request starts on the first grid timestamp and ends one second before `end`:
        exactly this window's grid, and never the first timestamp of the next one."""
        return datetime.fromtimestamp(self.start, timezone.utc), datetime.fromtimestamp(self.end - 1, timezone.utc)


def align_window(start: datetime, end: datetime, step: int) -> Window:
    """The one grid every feature query and frame uses, in training, validation and scoring alike.

    Policy: start and end are floored to a multiple of `step` in UTC epoch seconds, and the window is half-open,
    [start, end). The grid is start, start + step, ... < end, so two runs over overlapping periods sample the same
    instants and adjacent windows share no timestamp. A window shorter than one step, or one that holds no
    whole grid timestamp after flooring, is `insufficient_window` with an empty grid."""
    if step < 2:
        raise ValueError("step must be at least 2 seconds")
    s, e = start.timestamp(), end.timestamp()
    a, b = math.floor(s / step) * step, math.floor(e / step) * step
    if e - s < step or b <= a:
        why = "empty or reversed window" if e <= s else f"window of {e - s:.0f}s holds no whole {step}s step"
        return Window(a, a, step, [], "insufficient_window", why)
    return Window(a, b, step, list(range(a, b, step)), "ok")


def chunks(window: Window) -> list[Window]:
    """The window split on the grid into pieces of at most CHUNK_POINTS timestamps: each grid timestamp falls in
    exactly one chunk, with no overlap at the edges and no gap between them."""
    out = []
    for i in range(0, len(window.grid), CHUNK_POINTS):
        g = window.grid[i:i + CHUNK_POINTS]
        out.append(Window(g[0], g[-1] + window.step, window.step, g, "ok"))
    return out


def _by_entity(series: list[dict], label: str) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {}
    for s in series:
        name = (s.get("metric") or {}).get(label)
        if not name:
            continue
        pts = out.setdefault(name, {})
        for ts, v in s.get("values") or []:
            if v is None:
                continue
            x = float(v)
            if math.isfinite(x):
                pts[int(float(ts))] = x
    return out


def increases(points: dict[int, float], step: int) -> dict[int, float]:
    """Per-step increase of a raw counter. A decrease is a reset (a restarted exporter, a replaced pod), so the
    increase is the new value, never negative. The first point and points after a gap have no increase."""
    out: dict[int, float] = {}
    prev_t, prev_v = None, None
    for t in sorted(points):
        v = points[t]
        if prev_t is not None and t - prev_t == step:
            out[t] = v if v < prev_v else v - prev_v
        prev_t, prev_v = t, v
    return out


@dataclass
class CohortFrame:
    """One workload's (or node's) features on the grid. None means missing at that instant."""
    environment: str
    namespace: str
    workload: str
    step: int
    timestamps: list[int]
    values: dict[str, list[float | None]]
    quality: dict[str, Any] = field(default_factory=dict)
    features: tuple[str, ...] = FEATURES
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def cohort(self) -> str:
        return f"{self.environment}/{self.namespace}/{self.workload}"

    @property
    def level(self) -> str:
        return "infrastructure" if self.namespace == NODE_NAMESPACE else "service"

    def rows(self) -> list[tuple[int, list[float | None]]]:
        return [(t, [self.values[f][i] for f in self.features]) for i, t in enumerate(self.timestamps)]

    def slice(self, a: int, b: int) -> "CohortFrame":
        """[a, b) of this frame, same everything else."""
        idx = [i for i, t in enumerate(self.timestamps) if a <= t < b]
        return replace(self, timestamps=[self.timestamps[i] for i in idx], values={f: [self.values[f][i] for i in idx] for f in self.features})

    def snapshot(self) -> dict[str, Any]:
        """Canonical, JSON-safe contents: what a model was trained on, for its dataset version and reproduction."""
        return {"cohort": self.cohort, "step": self.step, "features": list(self.features), "timestamps": self.timestamps,
                "values": {f: [None if v is None else round(v, 12) for v in self.values[f]] for f in self.features}}


def from_snapshot(doc: dict[str, Any]) -> CohortFrame:
    env, ns, wl = str(doc["cohort"]).split("/", 2)
    feats = tuple(doc["features"])
    return CohortFrame(env, ns, wl, int(doc["step"]), [int(t) for t in doc["timestamps"]], {f: list(doc["values"][f]) for f in feats}, {}, feats)


def snapshot_sha256(doc: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


ChunkStatus = dict[str, list[tuple[Window, str]]]  # query -> each chunk and how it ended: success | failed | partial | absent


@dataclass
class Collection:
    """Everything one collection read: raw series by query, how each chunk ended, what the probe found."""
    environment: str
    namespace: str
    window: Window
    raw: dict[str, list[dict]]
    status: ChunkStatus
    probes: dict[str, dict[str, Any]]
    queries: dict[str, str]
    rate_window: int = RATE_WINDOW
    entity: str = "workload"
    catalog: str = ""

    def entities(self) -> list[str]:
        return sorted({n for series in self.raw.values() for n in _by_entity(series, self.entity)})


def _choose_source(per: dict[str, dict[str, dict[int, float]]], wl: str, ts: list[int]) -> tuple[str, dict[str, float]]:
    """The traffic source a cohort is trained on: the application's own metrics when they cover half the window,
    else the mesh's, else whichever covers more (the application's on a tie)."""
    cov = {src: (sum(1 for t in ts if t in per.get(raw_name("request_rate", src), {}).get(wl, {})) / len(ts) if ts else 0.0) for src in SOURCES}
    if cov["app"] >= 0.5:
        return "app", cov
    if cov["mesh"] >= 0.5:
        return "mesh", cov
    return ("mesh" if cov["mesh"] > cov["app"] else "app"), cov


def _undersampled(probes: dict[str, dict], rate_window: int) -> set[str]:
    """Raw counter/histogram queries whose base metric is sampled too rarely for rate() over the window."""
    out: set[str] = set()
    for p in probes.values():
        iv = p.get("interval_seconds")
        if iv and iv > rate_window / 4:
            out |= {q for q in p.get("queries", ()) if METRIC_DEFINITIONS.get(q, MetricDefinition()).kind in ("counter", "histogram")}
    return out


def assemble(environment: str, namespace: str, step: int, ts: list[int], raw: dict[str, list[dict]], status: ChunkStatus | None = None,
             definitions: dict[str, MetricDefinition] | None = None, *, pins: dict[str, dict[str, str]] | None = None, probes: dict[str, dict] | None = None,
             rate_window: int = RATE_WINDOW, promql: dict[str, str] | None = None, nodes: bool = False, catalog: str | None = None) -> list[CohortFrame]:
    """Raw query results to one frame per workload (or node). Pure, so the arithmetic is tested without Prometheus.
    `status` says how the chunk holding each timestamp ended, per query; None means every query succeeded.
    `pins` fixes a workload's traffic source ({"traffic": "mesh"}); without one, it is chosen by coverage."""
    defs = definitions or METRIC_DEFINITIONS
    probes = probes or {}
    ok_at = {q: {t for ch, outcome in log if outcome == "success" for t in ch.grid} for q, log in (status or {}).items()}
    complete = lambda q, t: status is None or t in ok_at.get(q, ())
    chunk_counts = {q: Counter(outcome for _, outcome in log) for q, log in (status or {}).items()}
    label = "node" if nodes else "workload"
    per = {k: _by_entity(v, label) for k, v in raw.items()}
    under = _undersampled(probes, max(rate_window, step))
    interval_of = {q: p.get("interval_seconds") for p in probes.values() for q in p.get("queries", ())}
    absent_by_probe = {q: p for p in probes.values() if p.get("state") == "absent" for q in p.get("queries", ())}
    specs = NODE_SPECS if nodes else WORKLOAD_SPECS
    catalog = catalog or catalog_digest()
    frames = []
    for wl in sorted({w for d in per.values() for w in d}):
        source, src_cov = ("app", {}) if nodes else ((pins or {}).get(wl, {}).get("traffic"), {})
        if not nodes and source is None:
            source, src_cov = _choose_source(per, wl, ts)
        rq = lambda name: raw_name(name, source) if not nodes else name
        get = lambda name: per.get(rq(name), {}).get(wl, {})
        values: dict[str, list[float | None]] = {s.name: [] for s in specs}
        sparse = unknown_errors = inferred_zero = resets = 0
        if nodes:
            cpu, mem, pressure = get("node_cpu"), get("node_memory"), get("node_pressure")
            for t in ts:
                values["node_cpu_utilization"].append(cpu.get(t))
                values["node_memory_utilization"].append(mem.get(t))
                values["node_pressure"].append(None if t not in pressure else (1.0 if pressure[t] > 0 else 0.0))
        else:
            req, err = get("request_rate"), get("errors_rate")
            restarts = get("restarts")
            inc = increases(restarts, step)
            resets = sum(1 for t in sorted(restarts) if t - step in restarts and restarts[t] < restarts[t - step])
            plain = {n: get(n) for n in ("latency_p50", "latency_p95", "latency_p99", "cpu_usage", "cpu_utilization", "cpu_throttling", "memory_working_set",
                                         "memory_utilization", "network_errors", "queue_depth", "db_connections")}
            for t in ts:
                r = req.get(t)
                values["request_rate"].append(r)
                if r is None or r < SPARSE_RPS:
                    values["error_ratio"].append(None)
                    sparse += r is not None
                elif not complete(rq("errors_rate"), t):
                    values["error_ratio"].append(None)  # the error query failed or came back partial here: unknown, not 0
                    unknown_errors += 1
                elif err.get(t) is not None:
                    values["error_ratio"].append(min(1.0, max(0.0, err[t] / r)))
                elif defs[rq("errors_rate")].absent_means_zero:
                    values["error_ratio"].append(0.0)  # a complete answer with traffic and no 5xx series: no errors
                    inferred_zero += 1
                else:
                    values["error_ratio"].append(None)
                for n, pts in plain.items():
                    values[n].append(pts.get(t))
                values["restart_rate"].append(inc[t] * 3600 / step if t in inc else None)
        n = len(ts)
        coverage = {s.name: round(sum(v is not None for v in values[s.name]) / n, 3) if n else 0.0 for s in specs}
        absent, reasons, unusable, prov = [], {}, [], {}
        for s in specs:
            inputs = [rq(i) for i in s.inputs]
            have = any(per.get(q, {}).get(wl) for q in inputs)
            if not have:
                absent.append(s.name)
                probe = next((absent_by_probe[q] for q in inputs if q in absent_by_probe), None)
                reasons[s.name] = (f"no series for {probe['selector']} in the probe" if probe else s.missing)
            if any(q in under for q in inputs):
                unusable.append(s.name)
                reasons.setdefault(s.name, f"sampled every {max(interval_of.get(q) or 0 for q in inputs):.0f}s, too rarely for rate() over {max(rate_window, step)}s")
            prov[s.name] = {"source": f"prometheus:{source if s.family == 'traffic' else s.origin}",
                            "queries": inputs, "unit": s.unit, "level": s.level, "native_interval_seconds": max((interval_of.get(q) or 0 for q in inputs), default=0) or None,
                            "rate_window_seconds": max(rate_window, step) if any(defs.get(q, MetricDefinition()).kind != "gauge" for q in inputs) else None,
                            "chunks": {q: {k: chunk_counts.get(q, Counter()).get(k, 0) for k in ("success", "failed", "partial", "absent")} for q in inputs if q in chunk_counts},
                            "coverage": coverage[s.name]}
        incomplete = sorted({q for q, log in (status or {}).items() if any(o in ("failed", "partial") for _, o in log)} & {rq(i) for s in specs for i in s.inputs})
        quality = {"points": n, "coverage": coverage, "absent": absent, "absent_reasons": reasons, "undersampled": unusable, "sparse_traffic_points": sparse,
                   "counter_resets": resets, "unknown_error_points": unknown_errors, "inferred_zero_error_points": inferred_zero, "incomplete_queries": incomplete,
                   "chunks": {q: {k: c.get(k, 0) for k in ("success", "failed", "partial", "absent")} for q, c in chunk_counts.items() if q in {rq(i) for s in specs for i in s.inputs}},
                   "sources": {} if nodes else {"traffic": source, "coverage": src_cov}}
        provenance = {"schema": FEATURE_SCHEMA, "catalog": catalog, "step": step, "rate_window_seconds": max(rate_window, step), "features": prov,
                      "promql": {q: (promql or {}).get(q) for q in sorted({rq(i) for s in specs for i in s.inputs}) if (promql or {}).get(q)}}
        frames.append(CohortFrame(environment, NODE_NAMESPACE if nodes else namespace, wl, step, ts, values, quality, tuple(s.name for s in specs), provenance))
    return frames


Query = Callable[[str, datetime, datetime, int], Awaitable[tuple[list[dict], bool]]]


def _on_grid(series: list[dict], grid: set[int]) -> list[dict]:
    """Keeps only samples on this chunk's own grid, so one chunk can never fill or overwrite another's instants."""
    out = []
    for s in series:
        vals = [p for p in s.get("values") or [] if float(p[0]) == int(float(p[0])) and int(float(p[0])) in grid]
        out.append({**s, "values": vals})
    return out


async def probe(query: Query, bases: dict[str, tuple[str, tuple[str, ...]]], at: datetime) -> dict[str, dict[str, Any]]:
    """One instant read per base metric: present (with its native interval), absent (no series at all), or unknown
    (the probe itself failed, in which case the chunks are read anyway and say what they find)."""
    out: dict[str, dict[str, Any]] = {}
    for key, (selector, qs) in bases.items():
        try:
            series, _ = await query(probe_query(selector), at, at, 60)
        except Exception as exc:
            out[key] = {"state": "unknown", "selector": selector, "queries": list(qs), "error": type(exc).__name__}
            continue
        vals = [float(v) for s in series for _, v in (s.get("values") or [])[-1:] if v is not None and math.isfinite(float(v))]
        if not vals:
            out[key] = {"state": "absent", "selector": selector, "queries": list(qs)}
        else:
            per_hour = max(vals[-1], 1.0)
            out[key] = {"state": "present", "selector": selector, "queries": list(qs), "interval_seconds": round(PROBE_SECONDS / per_hour, 1)}
    return out


async def collect_raw(query: Query, environment: str, namespace: str | None, start: datetime, end: datetime, step: int, metrics: dict[str, str] | None = None,
                      *, rate_window: int = RATE_WINDOW, use_probe: bool = True) -> Collection:
    """Probes, then runs every feature query in chunks of the aligned window. `query` returns (series, complete) and
    is the caller's authorized path to Prometheus. namespace None reads the node level. Raises WindowError when the
    window holds no whole step."""
    win = align_window(start, end, step)
    if win.status != "ok":
        raise WindowError(win.status, win.reason)
    nodes = namespace is None
    qs = node_queries(step=step, rate_window=rate_window) if nodes else queries(namespace, metrics, step=step, rate_window=rate_window)
    probes = await probe(query, _bases(namespace, metrics), datetime.fromtimestamp(win.end, timezone.utc)) if use_probe else {}
    skip = {q for p in probes.values() if p["state"] == "absent" for q in p["queries"]}
    raw: dict[str, list[dict]] = {k: [] for k in qs}
    status: ChunkStatus = {k: [] for k in qs}
    for ch in chunks(win):
        a, b = ch.request()
        for name, promql in qs.items():
            if name in skip:
                status[name].append((ch, "absent"))
                continue
            try:
                series, complete = await query(promql, a, b, step)
            except Exception:
                status[name].append((ch, "failed"))
                continue
            status[name].append((ch, "success" if complete and len(series) < SERIES_CAP else "partial"))
            raw[name] += _on_grid(series, set(ch.grid))
    return Collection(environment, NODE_NAMESPACE if nodes else namespace, win, raw, status, probes, qs, rate_window, "node" if nodes else "workload", catalog_digest(metrics))


def frames_of(c: Collection, *, pins: dict[str, dict[str, str]] | None = None, workloads: set[str] | None = None) -> list[CohortFrame]:
    nodes = c.entity == "node"
    out = assemble(c.environment, c.namespace, c.window.step, c.window.grid, c.raw, c.status, pins=pins, probes=c.probes, rate_window=c.rate_window, promql=c.queries, nodes=nodes,
                   catalog=c.catalog or None)
    return [f for f in out if workloads is None or f.workload in workloads]


async def collect(query: Query, environment: str, namespace: str | None, start: datetime, end: datetime, step: int, metrics: dict[str, str] | None = None,
                  workloads: set[str] | None = None, *, pins: dict[str, dict[str, str]] | None = None, rate_window: int = RATE_WINDOW) -> list[CohortFrame]:
    """collect_raw and frames_of in one call."""
    c = await collect_raw(query, environment, namespace, start, end, step, metrics, rate_window=rate_window)
    return frames_of(c, pins=pins, workloads=workloads)


def hour_of_week(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.weekday() * 24 + d.hour


def history_days(frame: CohortFrame) -> float:
    """How much representative history there is: points where at least two features were present."""
    usable = sum(1 for _, row in frame.rows() if sum(v is not None for v in row) >= 2)
    return usable * frame.step / 86400


def usable(frame: CohortFrame, min_coverage: float = 0.5) -> list[str]:
    """Features a model may learn from: at least `min_coverage` known values and not undersampled."""
    bad = set(frame.quality.get("undersampled") or [])
    return [f for f in frame.features if frame.quality.get("coverage", {}).get(f, 0) >= min_coverage and f not in bad]
