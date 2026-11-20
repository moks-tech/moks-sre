"""Evaluation of anomaly detection and correlation against known answers.

Two kinds of known answer:

  synthetic   generate() builds a small cluster (a web front end, two APIs, a database, a queue worker, two nodes)
              with daily and weekly cycles, correlated features, telemetry gaps and benign rollouts, then injects
              labeled faults: spikes, level shifts, a memory leak, a traffic drop, a database slowdown that
              reaches its callers, and a node under memory pressure. Every fault has an incident id, so
              correlation is scored as well as detection.
  injected    inject() plants the same kinds of fault into a cohort's own validation data during training, sized
              by that cohort's spread. Real history rarely holds labeled incidents; injection measures whether a
              detector would see a fault of a given size in this workload's actual noise.

Measures (all event-based, as an on-call engineer experiences them):

  recall        detected faults / faults; a fault is detected when an alarm starts between its start and its end
                plus a tolerance
  precision     alarms that match a fault / alarms; an alarm is a run of consecutive flagged windows; alarms that
                match a benign change (a rollout) are counted apart, not as false
  f1            harmonic mean of precision and recall
  fpr           flagged windows / windows that overlap no fault and no benign change
  delay         end of the first flagged window minus the fault's start: when a 15-minute tick would have shown it
  latency       milliseconds to score one window with its history, the way inference does
  correlation   pairwise precision and recall of "these two anomalies belong to one incident", the adjusted Rand
                index, and how many groups the anomalies became

Windows are scored the way the inference tick scores them: `window_points` long, one every `stride` points.
Synthetic numbers show that the code finds what it was built to find. They are not a measure of real-world
quality, and the documentation says so wherever they are quoted.
"""
from __future__ import annotations

import math
import random
import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any

from . import detectors
from .features import FEATURES, NODE_FEATURES, NODE_NAMESPACE, CohortFrame, hour_of_week, usable

STEP = 300
WINDOW_POINTS = 12   # one hour
STRIDE = 3           # a tick every 15 minutes
TOLERANCE_POINTS = 6  # 30 minutes after a fault ends still counts as seeing it
DAY0 = 1793836800    # 2026-11-03 00:00 UTC, a Tuesday


@dataclass
class Event:
    """A labeled fault (or a benign change) in one cohort, by grid index."""
    cohort: str
    start: int
    end: int  # last index, inclusive
    kind: str
    features: tuple[str, ...]
    incident: str = ""
    benign: bool = False


@dataclass
class Cluster:
    frames: dict[str, CohortFrame]
    events: list[Event]
    edges: list[tuple[str, str]]        # (caller workload, callee workload), same namespace
    placement: dict[str, set[str]]      # workload -> nodes
    rollouts: dict[str, list[int]]      # workload -> grid indexes of rollouts
    train_points: int
    meta: dict[str, Any] = field(default_factory=dict)


# ---- synthetic data ---------------------------------------------------------------------------------------------


def _profile(ts: int) -> float:
    h = (ts % 86400) / 3600
    daily = 1 + 0.6 * math.sin(2 * math.pi * (h - 8) / 24)
    weekend = 0.7 if hour_of_week(ts) >= 120 else 1.0
    return daily * weekend


def _series(rng: random.Random, ts: list[int], base_rps: float, kind: str) -> dict[str, list[float | None]]:
    v: dict[str, list[float | None]] = {f: [] for f in FEATURES}
    mem = 180 + rng.random() * 80
    for i, t in enumerate(ts):
        load = _profile(t)
        rps = base_rps * load * math.exp(rng.gauss(0, 0.06))
        p50 = 0.04 * (1 + 0.25 * load) * math.exp(rng.gauss(0, 0.05))
        cpu = 0.05 + 0.004 * rps * math.exp(rng.gauss(0, 0.05))
        mem = mem + rng.gauss(0, 0.4)
        if i % 144 == 0:
            mem = 180 + rng.random() * 80  # a GC-like reset twice a day
        ws = (mem + 6 * math.sin(i / 7)) * 2 ** 20
        v["request_rate"].append(rps if kind != "worker" else None)
        v["error_ratio"].append(max(0.0, 0.002 + rng.gauss(0, 0.0008)) if kind != "worker" and rps >= 0.05 else None)
        v["latency_p50"].append(p50 if kind != "worker" else None)
        v["latency_p95"].append(p50 * 2.4 * math.exp(rng.gauss(0, 0.06)) if kind != "worker" else None)
        v["latency_p99"].append(p50 * 4.0 * math.exp(rng.gauss(0, 0.09)) if kind != "worker" else None)
        v["cpu_usage"].append(cpu)
        v["cpu_utilization"].append(cpu / 0.5)
        v["cpu_throttling"].append(max(0.0, cpu / 0.5 - 0.7) * 0.5 + abs(rng.gauss(0, 0.002)))
        v["memory_working_set"].append(ws)
        v["memory_utilization"].append(ws / (512 * 2 ** 20))
        v["restart_rate"].append(0.0)
        v["network_errors"].append(abs(rng.gauss(0, 0.01)))
        v["queue_depth"].append(max(0.0, rng.gauss(20 * load, 3)) if kind == "worker" else None)
        v["db_connections"].append(round(8 + 0.05 * rps + rng.gauss(0, 0.6)) if kind in ("api", "db") else None)
    return v


def _node_series(rng: random.Random, ts: list[int]) -> dict[str, list[float | None]]:
    out: dict[str, list[float | None]] = {f: [] for f in NODE_FEATURES}
    for t in ts:
        load = _profile(t)
        out["node_cpu_utilization"].append(min(1.0, 0.25 + 0.15 * load + rng.gauss(0, 0.01)))
        out["node_memory_utilization"].append(min(1.0, 0.55 + 0.03 * load + rng.gauss(0, 0.005)))
        out["node_pressure"].append(0.0)
    return out


def _quality(frame: CohortFrame) -> CohortFrame:
    n = len(frame.timestamps)
    cov = {f: round(sum(v is not None for v in frame.values[f]) / n, 3) if n else 0.0 for f in frame.features}
    frame.quality = {"points": n, "coverage": cov, "absent": [f for f in frame.features if cov[f] == 0], "undersampled": []}
    return frame


def _bump(frame: CohortFrame, f: str, a: int, b: int, how) -> None:
    vals = frame.values[f]
    for i in range(a, min(b + 1, len(vals))):
        if vals[i] is not None:
            vals[i] = how(vals[i], i - a, b - a + 1)


def generate(seed: int = 7, days: int = 14, test_days: int = 3) -> Cluster:
    """A synthetic cluster: `days` of history, of which the last `test_days` hold the faults."""
    if test_days < 3 or days - test_days < 7:
        raise ValueError("the fault schedule needs at least 3 test days and 7 days of history before them")
    rng = random.Random(seed)
    n = days * 288
    ts = [DAY0 + i * STEP for i in range(n)]
    shapes = {"web": ("web", 40.0), "cart": ("api", 15.0), "catalogue": ("api", 25.0), "postgres": ("db", 30.0), "worker": ("worker", 0.0)}
    frames: dict[str, CohortFrame] = {}
    for wl, (kind, rps) in shapes.items():
        fr = CohortFrame("synthetic", "shop", wl, STEP, list(ts), _series(rng, ts, rps, kind))
        frames[fr.cohort] = fr
    for node in ("node-a", "node-b"):
        fr = CohortFrame("synthetic", NODE_NAMESPACE, node, STEP, list(ts), _node_series(rng, ts), features=NODE_FEATURES)
        frames[fr.cohort] = fr
    edges = [("web", "cart"), ("web", "catalogue"), ("cart", "postgres"), ("catalogue", "postgres")]
    placement = {"web": {"node-a"}, "cart": {"node-a"}, "catalogue": {"node-b"}, "postgres": {"node-b"}, "worker": {"node-a"}}
    c = lambda wl: f"synthetic/shop/{wl}"
    nd = lambda node: f"synthetic/{NODE_NAMESPACE}/{node}"
    train = n - test_days * 288
    events: list[Event] = []
    t0 = train + 36  # three hours into the test period

    def at(hours: float) -> int:
        return t0 + int(hours * 12)

    # Telemetry gaps in training and test: missing, never zero, and never an anomaly.
    for wl, start, length in (("cart", train - 200, 10), ("catalogue", at(30), 8), ("web", at(50), 6)):
        for f in FEATURES:
            vals = frames[c(wl)].values[f]
            for i in range(start, start + length):
                vals[i] = None
    # Benign rollouts: a small step in CPU and memory. Not faults; reported apart when flagged.
    rollouts = {"cart": [at(10)], "catalogue": [at(40)]}
    for wl, idxs in rollouts.items():
        for i in idxs:
            _bump(frames[c(wl)], "cpu_usage", i, n - 1, lambda x, k, m: x * 1.08)
            _bump(frames[c(wl)], "memory_working_set", i, n - 1, lambda x, k, m: x * 1.05)
            events.append(Event(c(wl), i, i + 6, "rollout", ("cpu_usage", "memory_working_set"), benign=True))

    def fault(wl: str, a: int, b: int, kind: str, feats: dict[str, Any], incident: str, cohort: str | None = None) -> None:
        key = cohort or c(wl)
        for f, how in feats.items():
            _bump(frames[key], f, a, b, how)
        events.append(Event(key, a, b, kind, tuple(feats), incident))

    # 1. A latency spike on the front end.
    fault("web", at(2), at(2) + 2, "spike", {"latency_p95": lambda x, k, m: x * 4, "latency_p99": lambda x, k, m: x * 4}, "inc-1")
    # 2. Cart errors after its rollout: a level shift.
    fault("cart", at(10) + 4, at(12), "level_shift", {"error_ratio": lambda x, k, m: x + 0.06}, "inc-2")
    # 3. A memory leak in the worker: gradual, over nine hours.
    fault("worker", at(16), at(25), "drift", {"memory_working_set": lambda x, k, m: x * (1 + 0.7 * k / m),
                                              "memory_utilization": lambda x, k, m: x * (1 + 0.7 * k / m)}, "inc-3")
    # 4. The database slows down; its callers see it ten minutes later, the front end five after that.
    slow = {"latency_p50": lambda x, k, m: x * 5, "latency_p95": lambda x, k, m: x * 6, "latency_p99": lambda x, k, m: x * 6}
    fault("postgres", at(34), at(36), "level_shift", slow, "inc-4")
    fault("cart", at(34) + 2, at(36), "level_shift", {**slow, "error_ratio": lambda x, k, m: x + 0.04}, "inc-4")
    fault("catalogue", at(34) + 2, at(36), "level_shift", slow, "inc-4")
    fault("web", at(34) + 3, at(36), "level_shift", {"latency_p95": lambda x, k, m: x * 3, "latency_p99": lambda x, k, m: x * 3}, "inc-4")
    # 5. node-a runs short of memory: pressure on the node, restarts and slow requests on what runs there.
    fault("", at(44), at(46), "node_pressure", {"node_memory_utilization": lambda x, k, m: min(1.0, x + 0.38), "node_pressure": lambda x, k, m: 1.0},
          "inc-5", cohort=nd("node-a"))
    fault("cart", at(44) + 1, at(46), "restarts", {"restart_rate": lambda x, k, m: 6.0, "latency_p95": lambda x, k, m: x * 3}, "inc-5")
    fault("worker", at(44) + 1, at(46), "restarts", {"restart_rate": lambda x, k, m: 4.0}, "inc-5")
    # 6. Catalogue traffic falls by 70% (a broken upstream route): the direction is lower.
    fault("catalogue", at(56), at(58), "traffic_drop", {"request_rate": lambda x, k, m: x * 0.3}, "inc-6")
    for fr in frames.values():
        _quality(fr)
    return Cluster(frames, events, edges, placement, rollouts, train, {"seed": seed, "days": days, "test_days": test_days})


def inject(frame: CohortFrame, start: int, rng: random.Random, count: int = 6) -> tuple[CohortFrame, list[Event]]:
    """A copy of `frame` with `count` faults planted after index `start`, each sized by the feature's own robust
    spread in the frame: spikes of 8 spreads, level shifts of 6, drifts reaching 10. Only usable features."""
    feats = [f for f in usable(frame) if f != "node_pressure"]
    out = replace(frame, values={f: list(v) for f, v in frame.values.items()})
    events: list[Event] = []
    n = len(frame.timestamps)
    span = n - start
    if not feats or span < WINDOW_POINTS * 4:
        return out, events
    slot = span // count
    for j in range(count):
        f = rng.choice(feats)
        known = [v for v in frame.values[f][:start] if v is not None]
        if len(known) < 8:
            continue
        med = statistics.median(known)
        spread = max(1.4826 * statistics.median(abs(x - med) for x in known), abs(med) * 0.05, detectors.ABS_FLOOR.get(f, 1e-6))
        kind = ("spike", "level_shift", "drift")[j % 3]
        sign = -1 if (f == "request_rate" and rng.random() < 0.5) else 1
        a = start + j * slot + rng.randrange(WINDOW_POINTS, max(WINDOW_POINTS + 1, slot - 3 * WINDOW_POINTS))
        length = {"spike": 3, "level_shift": 18, "drift": 36}[kind]
        b = min(n - 1, a + length - 1)
        size = {"spike": 8.0, "level_shift": 6.0, "drift": 10.0}[kind] * spread * sign
        for i in range(a, b + 1):
            if out.values[f][i] is not None:
                add = size * ((i - a + 1) / length if kind == "drift" else 1.0)
                out.values[f][i] = max(0.0, out.values[f][i] + add) if f != "error_ratio" else min(1.0, max(0.0, out.values[f][i] + add))
        events.append(Event(frame.cohort, a, b, kind, (f,), f"injected-{j}"))
    return out, events


# ---- scoring ----------------------------------------------------------------------------------------------------


def windows(start: int, end: int, window_points: int = WINDOW_POINTS, stride: int = STRIDE) -> list[tuple[int, int]]:
    """[a, b) index windows ending every `stride` points, the way the tick scores."""
    out, b = [], start + window_points
    while b <= end:
        out.append((b - window_points, b))
        b += stride
    return out


def flag_windows(algorithm: str, state: dict, frame: CohortFrame, start: int, *, window_points: int = WINDOW_POINTS, stride: int = STRIDE) -> list[tuple[int, int, dict]]:
    """Every tick window from `start` on: (a, b, result). Pointwise scores are computed once; each window is then
    summarized exactly as inference summarizes it."""
    pts = detectors.pointwise(algorithm, state, frame, start)
    out = []
    for a, b in windows(start, len(frame.timestamps), window_points, stride):
        r = detectors.summarize(algorithm, state, detectors.window_of(pts, a - start, b - start), frame, a)
        out.append((a, b, r))
    return out


def _alarms(flags: list[tuple[int, int, dict]]) -> list[tuple[int, int, int]]:
    """Runs of consecutive flagged windows: (first flagged window's end, last flagged window's end, first flagged point)."""
    out: list[tuple[int, int, int]] = []
    run: list[tuple[int, int, dict]] = []
    for w in [*flags, None]:
        if w is not None and w[2]["unusual"]:
            run.append(w)
            continue
        if run:
            out.append((run[0][1], run[-1][1], run[0][0]))
            run = []
    return out


def score_events(flags: list[tuple[int, int, dict]], events: list[Event], step: int = STEP, tolerance: int = TOLERANCE_POINTS) -> dict[str, Any]:
    """Event-based recall, alarm precision, F1, window false-positive rate and detection delay for one cohort."""
    faults = [e for e in events if not e.benign]
    benign = [e for e in events if e.benign]
    alarms = _alarms(flags)
    # An alarm sees a fault when the tick that raised it (the end of its first flagged window) falls between the
    # fault's start and its end plus the tolerance. An alarm already running before a fault does not see it.
    match = lambda al, e: e.start <= al[0] - 1 <= e.end + tolerance
    detected, delays = 0, []
    for e in faults:
        hits = [al for al in alarms if match(al, e)]
        if hits:
            detected += 1
            delays.append((min(al[0] for al in hits) - e.start) * step)  # a window [a, b) is reported when point b would arrive
    true_alarms = sum(1 for al in alarms if any(match(al, e) for e in faults))
    benign_alarms = sum(1 for al in alarms if not any(match(al, e) for e in faults) and any(match(al, e) for e in benign))
    false_alarms = len(alarms) - true_alarms - benign_alarms
    busy = lambda a, b: any(a <= e.end + tolerance and b - 1 >= e.start for e in events)
    negatives = [w for w in flags if not busy(w[0], w[1])]
    fp_windows = sum(1 for w in negatives if w[2]["unusual"])
    return {"faults": len(faults), "detected": detected, "alarms": len(alarms), "true_alarms": true_alarms, "false_alarms": false_alarms, "benign_alarms": benign_alarms,
            "negative_windows": len(negatives), "fp_windows": fp_windows, "delays": delays}


def combine(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals across cohorts into the reported measures."""
    tot = {k: sum(p[k] for p in parts) for k in ("faults", "detected", "alarms", "true_alarms", "false_alarms", "benign_alarms", "negative_windows", "fp_windows")}
    delays = sorted(d for p in parts for d in p["delays"])
    recall = tot["detected"] / tot["faults"] if tot["faults"] else None
    judged = tot["true_alarms"] + tot["false_alarms"]
    precision = tot["true_alarms"] / judged if judged else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else 0.0 if precision is not None and recall is not None else None
    return {**tot, "recall": _r(recall), "precision": _r(precision), "f1": _r(f1), "fpr": _r(tot["fp_windows"] / tot["negative_windows"] if tot["negative_windows"] else None),
            "median_delay_seconds": statistics.median(delays) if delays else None, "p90_delay_seconds": delays[min(len(delays) - 1, int(0.9 * len(delays)))] if delays else None}


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def latency_ms(algorithm: str, state: dict, frame: CohortFrame, start: int, samples: int = 12, window_points: int = WINDOW_POINTS) -> float | None:
    """Median wall time to score one window with the history the algorithm needs, as inference does."""
    hist = detectors.history_points(algorithm, state)
    spans = windows(start, len(frame.timestamps), window_points, max(1, (len(frame.timestamps) - start) // samples))[:samples]
    times = []
    for a, b in spans:
        sub = frame.slice(frame.timestamps[max(0, a - hist)], frame.timestamps[b - 1] + frame.step)
        began = time.perf_counter()
        detectors.score(algorithm, state, sub, start=a - max(0, a - hist))
        times.append((time.perf_counter() - began) * 1000)
    return round(statistics.median(times), 3) if times else None


def evaluate(algorithm: str, frame: CohortFrame, train_points: int, events: list[Event], *, seed: int = 0, persist: int = detectors.PERSIST) -> dict[str, Any]:
    """Fit on frame[:train_points], score every tick window after it, score the events."""
    train = _quality(frame.slice(frame.timestamps[0], frame.timestamps[train_points]))
    try:
        state = detectors.fit(algorithm, train, seed=seed, persist=persist)
    except ValueError as exc:
        return {"status": "not_trained", "reason": str(exc)}
    flags = flag_windows(algorithm, state, frame, train_points)
    return {"status": "scored", "events": score_events(flags, events), "flags": flags, "state": state,
            "latency_ms": latency_ms(algorithm, state, frame, train_points)}


# ---- correlation accuracy ---------------------------------------------------------------------------------------


def _pairs(labels: dict[Any, str]) -> set[frozenset]:
    keys = sorted(labels, key=str)
    return {frozenset((a, b)) for i, a in enumerate(keys) for b in keys[i + 1:] if labels[a] == labels[b]}


def adjusted_rand(truth: dict[Any, str], pred: dict[Any, str]) -> float | None:
    keys = [k for k in truth if k in pred]
    n = len(keys)
    if n < 2:
        return None
    comb = lambda x: x * (x - 1) / 2
    table: dict[tuple[str, str], int] = {}
    for k in keys:
        table[(truth[k], pred[k])] = table.get((truth[k], pred[k]), 0) + 1
    a = {}
    b = {}
    for (t, p), c in table.items():
        a[t] = a.get(t, 0) + c
        b[p] = b.get(p, 0) + c
    index = sum(comb(c) for c in table.values())
    sa, sb = sum(comb(c) for c in a.values()), sum(comb(c) for c in b.values())
    expected = sa * sb / comb(n)
    top = (sa + sb) / 2
    return round((index - expected) / (top - expected), 4) if top != expected else 1.0


def correlation_scores(truth: dict[Any, str], pred: dict[Any, str]) -> dict[str, Any]:
    """Pairwise precision and recall of "same incident" between anomalies, and the adjusted Rand index."""
    keys = [k for k in truth if k in pred]
    t, p = _pairs({k: truth[k] for k in keys}), _pairs({k: pred[k] for k in keys})
    both = len(t & p)
    precision = both / len(p) if p else (1.0 if not t else 0.0)
    recall = both / len(t) if t else 1.0
    return {"anomalies": len(keys), "groups": len(set(pred[k] for k in keys)), "incidents": len(set(truth[k] for k in keys)),
            "pair_precision": round(precision, 4), "pair_recall": round(recall, 4),
            "pair_f1": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0, "adjusted_rand": adjusted_rand(truth, pred)}


# ---- the whole benchmark ----------------------------------------------------------------------------------------


def run(seed: int = 7, algorithms: tuple[str, ...] | None = None, *, days: int = 14, test_days: int = 3) -> dict[str, Any]:
    """Every algorithm on the synthetic cluster, then correlation over the best one's anomalies and over the true ones."""
    from . import correlate

    cluster = generate(seed, days, test_days)
    algorithms = algorithms or detectors.NAMES
    table, per_alg_flags = {}, {}
    for alg in algorithms:
        parts, lat, skipped = [], [], []
        flags_by_cohort = {}
        began = time.perf_counter()
        for cohort, fr in cluster.frames.items():
            r = evaluate(alg, fr, cluster.train_points, [e for e in cluster.events if e.cohort == cohort], seed=seed)
            if r["status"] != "scored":
                skipped.append({"cohort": cohort, "reason": r["reason"]})
                continue
            parts.append(r["events"])
            flags_by_cohort[cohort] = r["flags"]
            if r["latency_ms"] is not None:
                lat.append(r["latency_ms"])
        table[alg] = {**combine(parts), "inference_ms_median": round(statistics.median(lat), 3) if lat else None, "kind": detectors.ALGORITHMS[alg].kind,
                      "skipped": skipped, "seconds": round(time.perf_counter() - began, 2)}
        per_alg_flags[alg] = flags_by_cohort
    ranked = sorted((a for a in table if table[a]["f1"] is not None), key=lambda a: (-(table[a]["f1"] or 0), table[a]["fpr"] or 1))
    best = ranked[0] if ranked else None
    topo = correlate.Topology(edges={(("shop", a), ("shop", b)) for a, b in cluster.edges},
                              placement={("shop", wl): nodes for wl, nodes in cluster.placement.items()},
                              rollouts={("shop", wl): [cluster.frames[f"synthetic/shop/{wl}"].timestamps[i] for i in idxs] for wl, idxs in cluster.rollouts.items()})
    corr = {}
    if best:
        anomalies, truth = _detected_anomalies(cluster, per_alg_flags[best])
        groups = correlate.group(anomalies, topo)
        corr["detected"] = {"algorithm": best, **correlation_scores(truth, _labels(groups))}
    anomalies, truth = _true_anomalies(cluster)
    corr["oracle"] = correlation_scores(truth, _labels(correlate.group(anomalies, topo)))
    return {"seed": seed, "data": {"cohorts": len(cluster.frames), "days": days, "test_days": test_days, "step_seconds": STEP, "faults": sum(1 for e in cluster.events if not e.benign),
                                   "benign_changes": sum(1 for e in cluster.events if e.benign), "incidents": len({e.incident for e in cluster.events if e.incident})},
            "window": {"points": WINDOW_POINTS, "stride": STRIDE, "tolerance_points": TOLERANCE_POINTS, "persist": detectors.PERSIST},
            "algorithms": table, "ranking": ranked, "correlation": corr,
            "note": "Synthetic data written for these tests: it shows the code finds what it was built to find, not how it performs on real workloads."}


def _labels(groups: list) -> dict[Any, str]:
    return {m.key: g.key for g in groups for m in g.members}


def _anomaly(cohort: str, first_ts: int, last_ts: int, features: list[str], score: float = 1.0):
    from . import correlate

    env, ns, wl = cohort.split("/", 2)
    return correlate.Anomaly(key=(cohort, first_ts), environment=env, namespace=ns, workload=wl, first=first_ts, last=last_ts, features=tuple(features), score=score,
                             level="infrastructure" if ns == NODE_NAMESPACE else "service")


def _detected_anomalies(cluster: Cluster, flags_by_cohort: dict[str, list]) -> tuple[list, dict]:
    anomalies, truth = [], {}
    for cohort, flags in flags_by_cohort.items():
        fr = cluster.frames[cohort]
        events = [e for e in cluster.events if e.cohort == cohort]
        for first_end, last_end, first_point in _alarms(flags):
            first_ts, last_ts = fr.timestamps[first_end - 1] + STEP, fr.timestamps[last_end - 1] + STEP
            res = next(w[2] for w in flags if w[1] == first_end)
            a = _anomaly(cohort, first_ts, last_ts, res.get("flagged_features") or [], res.get("score") or 1.0)
            anomalies.append(a)
            hit = next((e for e in events if not e.benign and e.start <= first_end - 1 <= e.end + TOLERANCE_POINTS), None)
            truth[a.key] = hit.incident if hit else f"noise:{cohort}:{first_ts}"
    return anomalies, truth


def _true_anomalies(cluster: Cluster) -> tuple[list, dict]:
    anomalies, truth = [], {}
    for e in cluster.events:
        if e.benign:
            continue
        fr = cluster.frames[e.cohort]
        a = _anomaly(e.cohort, fr.timestamps[e.start], fr.timestamps[e.end] + STEP, list(e.features))
        anomalies.append(a)
        truth[a.key] = e.incident
    return anomalies, truth


async def run_eval(data: dict[str, Any]) -> dict[str, Any]:
    """The `anomaly` evaluation kind: the benchmark for each seed in the dataset, with gates on the best algorithm's
    F1 and false-positive rate and on correlation. Synthetic, and said so in every gate's evidence."""
    import asyncio

    g = data.get("gates", {})
    results, gates = [], []
    for case in data["cases"]:
        r = await asyncio.to_thread(run, int(case["seed"]), tuple(data["algorithms"]) if data.get("algorithms") else None, days=int(data.get("days", 14)))
        best = r["ranking"][0] if r["ranking"] else None
        m = r["algorithms"].get(best, {}) if best else {}
        corr = (r["correlation"].get("detected") or {})
        results.append({"case": f"seed {case['seed']}", "best": best, "score": m.get("f1") or 0.0, "algorithms": {a: {k: v for k, v in x.items() if k != "skipped"} for a, x in r["algorithms"].items()},
                        "correlation": r["correlation"], "checks": {"f1": (m.get("f1") or 0) >= g.get("min_f1", 0.8), "fpr": (m.get("fpr") or 1) <= g.get("max_fpr", 0.02),
                                                                    "correlation": (corr.get("pair_f1") or 0) >= g.get("min_correlation_f1", 0.8)}})
    n = len(results) or 1
    score = round(sum(x["score"] for x in results) / n, 4)
    for name, key, limit, better in (("best_f1", "f1", g.get("min_f1", 0.8), "min"), ("best_fpr", "fpr", g.get("max_fpr", 0.02), "max"),
                                     ("correlation_pair_f1", "correlation", g.get("min_correlation_f1", 0.8), "min")):
        ok = all(x["checks"][key] for x in results)
        gates.append({"name": name, "passed": ok, "limit": limit, "evidence": "synthetic incidents (moks/ml/benchmark.py), not production data"})
    return {"results": results, "score": score, "gates": gates, "metrics": {"seeds": [c["seed"] for c in data["cases"]], "note": data.get("description", "")}}
