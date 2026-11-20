"""Anomaly detectors: seven algorithms behind one interface, compared on the same data before any is trusted.

  robust_zscore           univariate  median and MAD of the training period; the statistical baseline
  rolling_zscore          univariate  mean and standard deviation of the trailing 3 hours
  ewma                    univariate  exponentially weighted mean and variance, updated online, robust to outliers
  seasonal_baseline       univariate  median and MAD per hour of week, falling back to hour of day (baseline.py)
  seasonal_decomposition  univariate  trailing 24-hour trend plus an hour-of-day profile; scores the residual
  isolation_forest        multivariate  random isolation depth over the usable features (iforest.py)
  local_outlier_factor    multivariate  local density against a reference sample of training rows

Every state is plain JSON (artifacts.py refuses anything else) and is fitted on training data only. Online
detectors (rolling, EWMA, decomposition) read only points before the one they score, so a score never depends on
the future. That is checked by a test that changes later values and expects the same earlier scores.

Direction. Each feature says which way is a problem (features.FeatureSpec.direction): latency, errors, memory
and restarts only when higher; traffic both ways. A univariate detector flags only in a problem direction and
still reports the other deviations. A multivariate flag whose clearly moved features (|z| >= 2) all moved in a
harmless direction is not raised.

Robust baselines. Online baselines (the EWMA mean and the decomposition's trailing trend) take a new value at most
`clip` spreads from what they expected, so an anomaly does not become the baseline it is measured against.

Thresholds are calibrated per feature on held-out training data: the detector is fitted on the first 75% of the
training period, the last 25% is scored with it, and the threshold is the 99.5th percentile of sustained deviations
there (never below 3). The state is then refitted on the whole training period with those thresholds. Calibrating
on the data a model was fitted on would make flexible models (an hour-of-week profile nearly memorizes its training
days) look far more certain than they are. A feature is flagged in a window only when its deviation stays past the threshold for
`persist` consecutive points (default 2), so one odd sample is not an anomaly. A zero is a value; a missing point
is skipped and never treated as zero.

What a detector reports, per feature: the observed value, the baseline it expected, the deviation in robust
standard units, the direction, a pattern (spike, sustained, rising) and when it was first flagged. Multivariate
detectors add the features furthest from the training median. None of this says why.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import baseline, iforest
from .features import SPECS, CohortFrame, hour_of_week, usable

PERSIST = 2
Z_FLOOR = 3.0
Z_CEIL = 50.0
CAL_QUANTILE = 0.995
CAL_SHARE = 0.75          # fit on this much of the training period, calibrate thresholds on the rest
ROLLING_POINTS = 36       # 3 hours at the default 300 s step
EWMA_ALPHA = 0.1
EWMA_CLIP = 4.0           # an update moves the mean by at most this many spreads, so an anomaly is learned slowly
TREND_POINTS = 288        # 24 hours at the default step
LOF_REFERENCE = 256
LOF_K = 20

# The smallest spread worth measuring in, per feature: below it a change is noise whatever the history says.
ABS_FLOOR = {"request_rate": 0.05, "error_ratio": 0.01, "latency_p50": 0.005, "latency_p95": 0.005, "latency_p99": 0.005, "cpu_usage": 0.01,
             "cpu_utilization": 0.02, "cpu_throttling": 0.02, "memory_working_set": 1 << 20, "memory_utilization": 0.02, "restart_rate": 1.0,
             "network_errors": 0.1, "queue_depth": 1.0, "db_connections": 1.0, "node_cpu_utilization": 0.02, "node_memory_utilization": 0.02, "node_pressure": 0.25}

Point = tuple[float, float, float]  # (z, observed, expected)


def _floor(feature: str, level: float, spread: float) -> float:
    return max(spread, abs(level) * 0.05, ABS_FLOOR.get(feature, 1e-6))


def _mad(xs: list[float]) -> tuple[float, float]:
    med = statistics.median(xs)
    return med, statistics.median(abs(x - med) for x in xs)


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


# ---- the shared window rule ------------------------------------------------------------------------------------


def direction(feature: str) -> str:
    spec = SPECS.get(feature)
    return spec.direction if spec else "both"


def _ok_sign(run: list, way: str) -> bool:
    up, down = all(z > 0 for z in run), all(z < 0 for z in run)
    return up if way == "higher" else down if way == "lower" else (up or down)


def sustained(zs: list[float | None], persist: int, way: str = "both") -> tuple[float, int] | None:
    """The strongest deviation held for `persist` consecutive known points, with one sign that `way` allows: (signed
    value, index of the run's last point). The value is the smallest |z| inside the best run, so a run is as strong as
    its weakest point."""
    best: tuple[float, int] | None = None
    for i in range(persist - 1, len(zs)):
        run = zs[i - persist + 1:i + 1]
        if any(z is None for z in run) or not _ok_sign(run, way):
            continue
        v = min(abs(z) for z in run) * (1 if run[0] > 0 else -1)  # type: ignore[operator]
        if best is None or abs(v) > abs(best[0]):
            best = (v, i)
    return best


def first_flag(zs: list[float | None], threshold: float, persist: int, way: str = "both") -> int | None:
    """Index of the point that completes the first run of `persist` points past the threshold, with one sign `way` allows."""
    for i in range(persist - 1, len(zs)):
        run = zs[i - persist + 1:i + 1]
        if all(z is not None and abs(z) >= threshold for z in run) and _ok_sign(run, way):
            return i
    return None


def _pattern(zs: list[float | None], threshold: float, start: int) -> str:
    run = []
    for z in zs[start:]:
        if z is None or abs(z) < threshold:
            break
        run.append(abs(z))
    if start + len(run) < len(zs):
        return "spike"      # it came back inside the window
    if len(run) >= 3 and run[-1] > 1.5 * run[0]:
        return "rising"
    return "sustained"


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def summarize_univariate(state: dict, per: dict[str, list[Point | None]], ts: list[int], persist: int = PERSIST) -> dict[str, Any]:
    """One window's result from per-point (z, observed, expected) per feature."""
    devs, best = [], None
    for f, pts in per.items():
        zs = [p[0] if p else None for p in pts]
        known = [p for p in pts if p]
        if not known:
            continue
        thr = float(state["thresholds"].get(f, Z_FLOOR))
        way = direction(f)
        s = sustained(zs, persist, way)
        at = first_flag(zs, thr, persist, way)
        flagged = at is not None
        if flagged:
            start = at - persist + 1
            run = [p for p in pts[start:] if p and abs(p[0]) >= thr] or known
        else:
            run = known
        z = statistics.median(p[0] for p in run)
        spec = SPECS.get(f)
        d = {"feature": f, "z": round(z, 2), "sustained_z": round(s[0], 2) if s else 0.0, "alert_direction": way, "observed": round(statistics.median(p[1] for p in run), 6),
             "typical": round(statistics.median(p[2] for p in run), 6), "direction": "higher" if z > 0 else "lower", "points": len(known), "threshold": round(thr, 3),
             "flagged": flagged, "level": spec.level if spec else None, "unit": spec.unit if spec else None}
        if flagged:
            d["first_flagged_at"] = _iso(ts[at])
            d["pattern"] = _pattern(zs, thr, at - persist + 1)
        devs.append(d)
        ratio = abs(s[0]) / thr if s else 0.0
        if best is None or ratio > best[0]:
            best = (ratio, abs(s[0]) if s else 0.0, thr)
    devs.sort(key=lambda d: (not d["flagged"], -abs(d["sustained_z"]) / d["threshold"]))
    flagged = [d for d in devs if d["flagged"]]
    return {"score": round(best[1], 3) if best else None, "threshold": round(best[2], 3) if best else None, "unusual": bool(flagged), "deviations": devs[:8],
            "flagged_features": [d["feature"] for d in flagged], "first_flagged_at": min((d["first_flagged_at"] for d in flagged), default=None)}


def _explain(state: dict, frame: CohortFrame, idx: list[int]) -> list[dict]:
    """For a multivariate detector: the features furthest from the training median, scaled by the training IQR."""
    pre, feats = state["pre"], state["features"]
    out = []
    for j, f in enumerate(feats):
        xs = [frame.values[f][i] for i in idx if frame.values[f][i] is not None]
        if xs:
            z = (statistics.median(xs) - pre["median"][j]) / pre["scale"][j]
            spec = SPECS.get(f)
            out.append({"feature": f, "z": round(z, 2), "observed": round(statistics.median(xs), 6), "typical": round(pre["median"][j], 6),
                        "direction": "higher" if z > 0 else "lower", "points": len(xs), "level": spec.level if spec else None, "unit": spec.unit if spec else None})
    out.sort(key=lambda d: -abs(d["z"]))
    return out[:6]


def summarize_multivariate(state: dict, scores: list[float | None], frame: CohortFrame, start: int, persist: int = PERSIST) -> dict[str, Any]:
    thr = float(state["threshold"])
    known = [s for s in scores if s is not None]
    if not known:
        return {"score": None, "threshold": round(thr, 4), "unusual": False, "deviations": [], "flagged_features": [], "first_flagged_at": None}
    margin = [None if s is None else s - thr + 1e-12 for s in scores]  # positive where past the threshold
    at = next((i for i in range(persist - 1, len(margin)) if all(m is not None and m > 0 for m in margin[i - persist + 1:i + 1])), None)
    runs = [min(scores[i - persist + 1:i + 1]) for i in range(persist - 1, len(scores)) if all(s is not None for s in scores[i - persist + 1:i + 1])]  # type: ignore[type-var]
    top = max(runs) if runs else max(known)
    idx = [start + i for i in range(len(scores)) if scores[i] is not None and (at is None or scores[i] >= thr)]
    devs = _explain(state, frame, idx)
    moved = [d for d in devs[:3] if abs(d["z"]) >= 2]
    harmless = bool(moved) and all(direction(d["feature"]) == "higher" and d["z"] < 0 for d in moved)
    if at is not None and harmless:
        at = None  # everything that clearly moved moved the harmless way (lower latency, less memory)
    return {"score": round(top, 4), "threshold": round(thr, 4), "unusual": at is not None, "deviations": devs, "suppressed": "harmless direction" if harmless else None,
            "flagged_features": [d["feature"] for d in devs if abs(d["z"]) >= 3] if at is not None else [],
            "first_flagged_at": _iso(frame.timestamps[start + at]) if at is not None else None,
            "explanation": "features furthest from the training median, scaled by the training interquartile range"}


# ---- univariate detectors ---------------------------------------------------------------------------------------


def _calibrate_multi(scores: list[float | None], window: int, persist: int) -> float | None:
    """The CAL_QUANTILE of each held-out window's strongest sustained score (the weakest point of its best run)."""
    out = []
    for a in range(0, max(1, len(scores) - window + 1), max(1, window // 2)):
        w = scores[a:a + window]
        runs = [min(w[i - persist + 1:i + 1]) for i in range(persist - 1, len(w)) if all(x is not None for x in w[i - persist + 1:i + 1])]  # type: ignore[type-var]
        if runs:
            out.append(max(runs))
    return _quantile(out, CAL_QUANTILE) if out else None


def _calibrate(per: dict[str, list[Point | None]], window: int, persist: int) -> dict[str, float]:
    """Per feature: the CAL_QUANTILE of the sustained |z| over every training window, clamped to [Z_FLOOR, Z_CEIL]."""
    out = {}
    for f, pts in per.items():
        zs = [p[0] if p else None for p in pts]
        scores = []
        for a in range(0, max(1, len(zs) - window + 1), max(1, window // 2)):
            s = sustained(zs[a:a + window], persist, direction(f))
            if s is not None:
                scores.append(abs(s[0]))
        out[f] = round(min(Z_CEIL, max(Z_FLOOR, _quantile(scores, CAL_QUANTILE) if scores else Z_FLOOR)), 4)
    return out


def _robust_fit(frame: CohortFrame, feats: list[str]) -> dict:
    out = {}
    for f in feats:
        xs = [v for v in frame.values[f] if v is not None]
        if len(xs) >= 8:
            med, mad = _mad(xs)
            out[f] = [med, _floor(f, med, 1.4826 * mad)]
    return {"features": out}


def _robust_points(state: dict, frame: CohortFrame, start: int) -> dict[str, list[Point | None]]:
    out = {}
    for f, (med, spread) in state["features"].items():
        out[f] = [None if (x := frame.values[f][i]) is None else ((x - med) / spread, x, med) for i in range(start, len(frame.timestamps))]
    return out


def _rolling_points(state: dict, frame: CohortFrame, start: int) -> dict[str, list[Point | None]]:
    w = int(state["window"])
    out = {}
    for f, floor in state["features"].items():
        vals = frame.values[f]
        pts: list[Point | None] = []
        for i in range(start, len(vals)):
            x = vals[i]
            hist = [v for v in vals[max(0, i - w):i] if v is not None]
            if x is None or len(hist) < max(4, w // 2):
                pts.append(None)
                continue
            mean = sum(hist) / len(hist)
            sd = statistics.pstdev(hist)
            pts.append(((x - mean) / max(sd, floor), x, mean))
        out[f] = pts
    return out


def _rolling_fit(frame: CohortFrame, feats: list[str]) -> dict:
    floors = {}
    for f in feats:
        xs = [v for v in frame.values[f] if v is not None]
        if len(xs) < 2 * ROLLING_POINTS:
            continue
        med = statistics.median(xs)
        sds = []
        vals = frame.values[f]
        for i in range(ROLLING_POINTS, len(vals), ROLLING_POINTS // 2):
            h = [v for v in vals[i - ROLLING_POINTS:i] if v is not None]
            if len(h) >= ROLLING_POINTS // 2:
                sds.append(statistics.pstdev(h))
        floors[f] = _floor(f, med, 0.5 * statistics.median(sds) if sds else 0.0)
    return {"window": ROLLING_POINTS, "features": floors}


def _ewma_fit(frame: CohortFrame, feats: list[str]) -> dict:
    out = {}
    for f in feats:
        xs = [v for v in frame.values[f] if v is not None]
        if len(xs) < 16:
            continue
        med, mad = _mad(xs)
        out[f] = {"mean": med, "var": (1.4826 * mad) ** 2, "floor": _floor(f, med, 0.5 * 1.4826 * mad)}
    return {"alpha": EWMA_ALPHA, "clip": EWMA_CLIP, "features": out}


def _ewma_points(state: dict, frame: CohortFrame, start: int) -> dict[str, list[Point | None]]:
    a, clip = float(state["alpha"]), float(state["clip"])
    out = {}
    for f, s in state["features"].items():
        m, v, floor = float(s["mean"]), float(s["var"]), float(s["floor"])
        pts: list[Point | None] = []
        for i, x in enumerate(frame.values[f]):
            if x is None:
                if i >= start:
                    pts.append(None)
                continue
            spread = max(math.sqrt(max(v, 0.0)), floor)
            if i >= start:
                pts.append(((x - m) / spread, x, m))
            diff = max(-clip * spread, min(clip * spread, x - m))  # an outlier moves the baseline by at most clip spreads
            incr = a * diff
            m += incr
            v = (1 - a) * (v + diff * incr)
        out[f] = pts
    return out


SEASONAL_STRENGTH = 0.2  # share of a feature's variance its hour-of-day profile must explain to be used


def seasonal_strength(frame: CohortFrame, f: str) -> float:
    """1 - var(within-hour) / var(total), on hour of day: near 0 for a feature with no daily cycle."""
    by_hour: dict[int, list[float]] = {}
    for t, x in zip(frame.timestamps, frame.values[f]):
        if x is not None:
            by_hour.setdefault((t % 86400) // 3600, []).append(x)
    xs = [x for v in by_hour.values() for x in v]
    if len(xs) < 48 or len(by_hour) < 12:
        return 0.0
    total = statistics.pvariance(xs)
    within = sum(statistics.pvariance(v) * len(v) for v in by_hour.values() if len(v) > 1) / len(xs)
    return max(0.0, 1 - within / total) if total > 0 else 0.0


def _seasonal_fit(frame: CohortFrame, feats: list[str]) -> dict:
    """Hour-of-week and hour-of-day buckets only for features with a daily cycle; the others keep one median and MAD,
    because buckets fitted to a wandering series (memory, say) describe the days they saw and nothing else."""
    full = baseline.fit([frame])
    model = {}
    for f in feats:
        if f not in full or not full[f].get("all"):
            continue
        model[f] = full[f] if seasonal_strength(frame, f) >= SEASONAL_STRENGTH else {**full[f], "week": {}, "day": {}}
    return {"features": model}


def _seasonal_points(state: dict, frame: CohortFrame, start: int) -> dict[str, list[Point | None]]:
    model = state["features"]
    out = {}
    for f in model:
        pts: list[Point | None] = []
        for i in range(start, len(frame.timestamps)):
            x = frame.values[f][i]
            e = baseline.expected(model, f, frame.timestamps[i]) if x is not None else None
            if e is None:
                pts.append(None)
                continue
            med, spread, _ = e
            pts.append(((x - med) / max(spread, ABS_FLOOR.get(f, 1e-6)), x, med))
        out[f] = pts
    return out


def _trend(vals: list[float | None], window: int, ts: list[int] | None = None, seasonal: list[float] | None = None, spread: float | None = None,
           clip: float = EWMA_CLIP) -> list[float | None]:
    """Trailing mean of the `window` points before each one (not including it): None with under a quarter of them known.
    With a seasonal profile and a spread, each value enters the mean at most `clip` spreads from what was expected
    there, so an anomaly does not drag the trend it is measured against."""
    out: list[float | None] = []
    kept: list[float | None] = []
    total, count = 0.0, 0
    for i, x in enumerate(vals):
        tr = total / count if count >= max(4, window // 4) else None
        out.append(tr)
        v = x
        if v is not None and tr is not None and spread and seasonal is not None and ts is not None:
            e = tr + seasonal[(ts[i] % 86400) // 3600]
            v = max(e - clip * spread, min(e + clip * spread, v))
        kept.append(v)
        if v is not None:
            total, count = total + v, count + 1
        if i - window >= 0 and kept[i - window] is not None:
            total, count = total - kept[i - window], count - 1  # type: ignore[operator]
    return out


def _decomp_fit(frame: CohortFrame, feats: list[str]) -> dict:
    out = {}
    for f in feats:
        vals = frame.values[f]
        tr = _trend(vals, TREND_POINTS)
        by_hour: dict[int, list[float]] = {}
        for i, x in enumerate(vals):
            if x is not None and tr[i] is not None:
                by_hour.setdefault(hour_of_week(frame.timestamps[i]) % 24, []).append(x - tr[i])  # type: ignore[operator]
        if sum(len(v) for v in by_hour.values()) < 48:
            continue
        seasonal = [round(statistics.median(by_hour[h]), 9) if len(by_hour.get(h, [])) >= 4 else 0.0 for h in range(24)]
        level = statistics.median(v for v in vals if v is not None)
        res = [x - tr[i] - seasonal[hour_of_week(frame.timestamps[i]) % 24] for i, x in enumerate(vals) if x is not None and tr[i] is not None]  # type: ignore[operator]
        _, mad = _mad(res)
        tr = _trend(vals, TREND_POINTS, frame.timestamps, seasonal, _floor(f, level, 1.4826 * mad))  # again, robust to what the first pass let in
        res = [x - tr[i] - seasonal[hour_of_week(frame.timestamps[i]) % 24] for i, x in enumerate(vals) if x is not None and tr[i] is not None]  # type: ignore[operator]
        if len(res) < 48:
            continue
        med, mad = _mad(res)
        out[f] = {"seasonal": seasonal, "residual_median": med, "spread": _floor(f, level, 1.4826 * mad)}
    return {"trend_points": TREND_POINTS, "period": "day", "features": out}


def _decomp_points(state: dict, frame: CohortFrame, start: int) -> dict[str, list[Point | None]]:
    out = {}
    for f, s in state["features"].items():
        tr = _trend(frame.values[f], int(state["trend_points"]), frame.timestamps, s["seasonal"], s["spread"])
        pts: list[Point | None] = []
        for i in range(start, len(frame.timestamps)):
            x = frame.values[f][i]
            if x is None or tr[i] is None:
                pts.append(None)
                continue
            expected = tr[i] + s["seasonal"][hour_of_week(frame.timestamps[i]) % 24] + s["residual_median"]  # type: ignore[operator]
            pts.append(((x - expected) / s["spread"], x, expected))
        out[f] = pts
    return out


# ---- multivariate detectors -------------------------------------------------------------------------------------


def _rows(frame: CohortFrame, feats: list[str], start: int = 0) -> list[list[float | None]]:
    return [[frame.values[f][i] for f in feats] for i in range(start, len(frame.timestamps))]


def _half(row: list[float | None]) -> bool:
    return sum(v is not None for v in row) * 2 >= len(row)


def _iforest_fit(frame: CohortFrame, feats: list[str], seed: int) -> dict:
    rows = [r for r in _rows(frame, feats) if _half(r)]
    forest = iforest.fit(rows, seed=seed)
    return {"features": feats, "forest": forest, "pre": forest["pre"], "threshold": forest["threshold"]}


def _iforest_points(state: dict, frame: CohortFrame, start: int) -> list[float | None]:
    return [iforest.score(state["forest"], r) if _half(r) else None for r in _rows(frame, state["features"], start)]


def _dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _lof_fit(frame: CohortFrame, feats: list[str], seed: int) -> dict:
    rows = [r for r in _rows(frame, feats) if _half(r)]
    pre = iforest.preprocess_fit(rows)
    data = [x for r in rows if (x := iforest.transform(pre, r)) is not None]
    if len(data) < 2 * LOF_K:
        raise ValueError(f"local outlier factor needs at least {2 * LOF_K} usable rows, got {len(data)}")
    rng = random.Random(seed)
    refs = [[round(v, 6) for v in r] for r in (rng.sample(data, LOF_REFERENCE) if len(data) > LOF_REFERENCE else data)]
    k = min(LOF_K, len(refs) - 1)
    d = [[_dist(a, b) for b in refs] for a in refs]
    knn = [sorted(range(len(refs)), key=lambda j: (d[i][j], j))[1:k + 1] for i in range(len(refs))]
    kdist = [d[i][knn[i][-1]] for i in range(len(refs))]
    lrd = [1.0 / (sum(max(kdist[o], d[i][o]) for o in knn[i]) / k + 1e-9) for i in range(len(refs))]
    lof = [sum(lrd[o] for o in knn[i]) / k / lrd[i] for i in range(len(refs))]
    return {"features": feats, "pre": pre, "refs": refs, "kdist": [round(x, 9) for x in kdist], "lrd": [round(x, 9) for x in lrd], "k": k,
            "threshold": round(max(1.5, _quantile(lof, 0.99)), 6)}


def lof_score(state: dict, row: list[float | None]) -> float | None:
    x = iforest.transform(state["pre"], row)
    if x is None:
        return None
    refs, k = state["refs"], int(state["k"])
    ds = sorted(((_dist(x, r), j) for j, r in enumerate(refs)))[:k]
    reach = sum(max(state["kdist"][j], dd) for dd, j in ds) / k
    lrd_x = 1.0 / (reach + 1e-9)
    return sum(state["lrd"][j] for _, j in ds) / k / lrd_x


def _lof_points(state: dict, frame: CohortFrame, start: int) -> list[float | None]:
    return [lof_score(state, r) if _half(r) else None for r in _rows(frame, state["features"], start)]


# ---- structure checks, before anything is scored ----------------------------------------------------------------


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _check_univariate(state: Any, allowed: list[str], shape: Callable[[Any], bool], extra: tuple[str, ...] = ()) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("features"), dict) or not isinstance(state.get("thresholds"), dict):
        raise ValueError("state has no features or thresholds")
    if not set(state["features"]) <= set(allowed) or not set(state["thresholds"]) <= set(state["features"]):
        raise ValueError("state names features outside its schema")
    for k in extra:
        if not _num(state.get(k)):
            raise ValueError(f"state parameter {k} is not a number")
    for f, v in state["features"].items():
        if not shape(v):
            raise ValueError(f"state for {f} is malformed")
    if not all(_num(t) and Z_FLOOR <= t <= Z_CEIL for t in state["thresholds"].values()):
        raise ValueError("thresholds out of range")


def _check_multivariate(state: Any, allowed: list[str], keys: set[str]) -> None:
    if not isinstance(state, dict) or set(state) != keys:
        raise ValueError("state has unexpected keys")
    feats = state["features"]
    if not isinstance(feats, list) or not feats or not set(feats) <= set(allowed):
        raise ValueError("state names features outside its schema")
    pre = state["pre"]
    if not isinstance(pre, dict) or len(pre.get("median") or []) != len(feats) or len(pre.get("scale") or []) != len(feats):
        raise ValueError("preprocessing does not match the features")
    if not all(_num(v) for v in [*pre["median"], *pre["scale"]]) or not all(s > 0 for s in pre["scale"]):
        raise ValueError("preprocessing values are invalid")
    if not _num(state["threshold"]) or state["calibration"] not in ("held_out", "in_sample"):
        raise ValueError("threshold is not a number")


def _validate_iforest(state: Any, allowed: list[str]) -> None:
    _check_multivariate(state, allowed, {"features", "forest", "pre", "threshold", "calibration"})
    iforest.validate(state["forest"], len(state["features"]))


def _validate_lof(state: Any, allowed: list[str]) -> None:
    _check_multivariate(state, allowed, {"features", "pre", "refs", "kdist", "lrd", "k", "threshold", "calibration"})
    n, d = len(state["refs"]), len(state["features"])
    if not 2 <= n <= 4096 or not isinstance(state["k"], int) or not 1 <= state["k"] < n:
        raise ValueError("reference set size out of bounds")
    if len(state["kdist"]) != n or len(state["lrd"]) != n or any(not isinstance(r, list) or len(r) != d or not all(_num(v) for v in r) for r in state["refs"]):
        raise ValueError("reference set is malformed")
    if not all(_num(v) and v >= 0 for v in [*state["kdist"], *state["lrd"]]) or not 1.0 <= state["threshold"] <= 1e6:
        raise ValueError("reference statistics are invalid")


# ---- the registry -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Algorithm:
    name: str
    kind: str  # univariate | multivariate
    describe: str
    history: Callable[[dict], int]  # points of history scoring needs before the window


ALGORITHMS: dict[str, Algorithm] = {
    "robust_zscore": Algorithm("robust_zscore", "univariate", "median and MAD of the training period", lambda s: 0),
    "rolling_zscore": Algorithm("rolling_zscore", "univariate", "mean and standard deviation of the trailing 3 hours", lambda s: int(s.get("window", ROLLING_POINTS))),
    "ewma": Algorithm("ewma", "univariate", "exponentially weighted mean and variance, robust to outliers", lambda s: 72),
    "seasonal_baseline": Algorithm("seasonal_baseline", "univariate", "median and MAD per hour of week, then hour of day", lambda s: 0),
    "seasonal_decomposition": Algorithm("seasonal_decomposition", "univariate", "trailing 24-hour trend and an hour-of-day profile; scores the residual",
                                        lambda s: int(s.get("trend_points", TREND_POINTS))),
    "isolation_forest": Algorithm("isolation_forest", "multivariate", "isolation depth over the usable features", lambda s: 0),
    "local_outlier_factor": Algorithm("local_outlier_factor", "multivariate", "local density against a reference sample of training rows", lambda s: 0),
}
NAMES = tuple(ALGORITHMS)

_UNI_FIT = {"robust_zscore": _robust_fit, "rolling_zscore": _rolling_fit, "ewma": _ewma_fit, "seasonal_baseline": _seasonal_fit, "seasonal_decomposition": _decomp_fit}
_UNI_POINTS = {"robust_zscore": _robust_points, "rolling_zscore": _rolling_points, "ewma": _ewma_points, "seasonal_baseline": _seasonal_points,
               "seasonal_decomposition": _decomp_points}
_MULTI_POINTS = {"isolation_forest": _iforest_points, "local_outlier_factor": _lof_points}


def fit(algorithm: str, frame: CohortFrame, *, seed: int = 0, window_points: int = 12, persist: int = PERSIST) -> dict[str, Any]:
    """The algorithm's JSON state from a training frame. Only usable features (half coverage, not undersampled) are
    learned. Raises ValueError when there is too little to learn from."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unknown algorithm {algorithm}")
    feats = usable(frame)
    if not feats:
        raise ValueError("no feature has half coverage")
    if algorithm in _MULTI_POINTS:
        fitter = _iforest_fit if algorithm == "isolation_forest" else _lof_fit
        state = fitter(frame, feats, seed)
        k = int(len(frame.timestamps) * CAL_SHARE)
        try:  # the threshold comes from held-out windows, and is never below the training points' own quantile
            head = fitter(frame.slice(frame.timestamps[0], frame.timestamps[k]), feats, seed)
            cal = _calibrate_multi(_MULTI_POINTS[algorithm](head, frame, k), window_points, persist)
        except (ValueError, IndexError):
            cal = None
        if cal is not None:
            state["threshold"] = round(max(state["threshold"], cal, 1.5 if algorithm == "local_outlier_factor" else 0.0), 6)
        state["calibration"] = "held_out" if cal is not None else "in_sample"
        return state
    state = _UNI_FIT[algorithm](frame, feats)
    if not state["features"]:
        raise ValueError(f"{algorithm} found no feature with enough history")
    k = int(len(frame.timestamps) * CAL_SHARE)
    head = frame.slice(frame.timestamps[0], frame.timestamps[k]) if 0 < k < len(frame.timestamps) else frame
    head_state = _UNI_FIT[algorithm](head, [f for f in feats if f in state["features"]]) if head is not frame else state
    held_out = _UNI_POINTS[algorithm](head_state, frame, k) if head is not frame else _UNI_POINTS[algorithm](state, frame, 0)
    cal = _calibrate(held_out, window_points, persist)
    state["thresholds"] = {f: cal.get(f, Z_FLOOR) for f in state["features"]}
    state["persist"] = persist
    return state


def pointwise(algorithm: str, state: dict, frame: CohortFrame, start: int = 0) -> Any:
    """Per point from `start` on, using the points before it as history: {feature: [(z, x, expected) | None]} for a
    univariate detector, [score | None] for a multivariate one."""
    if ALGORITHMS[algorithm].kind == "multivariate":
        return _MULTI_POINTS[algorithm](state, frame, start)
    return _UNI_POINTS[algorithm](state, frame, start)


def summarize(algorithm: str, state: dict, points: Any, frame: CohortFrame, start: int, persist: int | None = None) -> dict[str, Any]:
    persist = int(persist or state.get("persist") or PERSIST)
    if ALGORITHMS[algorithm].kind == "multivariate":
        return summarize_multivariate(state, points, frame, start, persist)
    return summarize_univariate(state, points, frame.timestamps[start:], persist)


def score(algorithm: str, state: dict, frame: CohortFrame, start: int = 0, persist: int | None = None) -> dict[str, Any]:
    """The window frame[start:], with frame[:start] as history."""
    return summarize(algorithm, state, pointwise(algorithm, state, frame, start), frame, start, persist)


def window_of(points: Any, a: int, b: int) -> Any:
    """points[a:b] for either shape."""
    if isinstance(points, dict):
        return {f: p[a:b] for f, p in points.items()}
    return points[a:b]


def history_points(algorithm: str, state: dict) -> int:
    return ALGORITHMS[algorithm].history(state)


def validate(algorithm: str, state: Any, features: list[str]) -> None:
    """Structure and bounds of a stored state, so a malformed artifact is refused before it scores anything."""
    if algorithm == "isolation_forest":
        return _validate_iforest(state, features)
    if algorithm == "local_outlier_factor":
        return _validate_lof(state, features)
    shapes: dict[str, tuple[Callable[[Any], bool], tuple[str, ...]]] = {
        "robust_zscore": (lambda v: isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v) and v[1] > 0, ()),
        "rolling_zscore": (lambda v: _num(v) and v > 0, ("window",)),
        "ewma": (lambda v: isinstance(v, dict) and set(v) == {"mean", "var", "floor"} and all(_num(x) for x in v.values()) and v["floor"] > 0, ("alpha", "clip")),
        "seasonal_baseline": (lambda v: isinstance(v, dict) and {"week", "day", "all", "samples"} <= set(v), ()),
        "seasonal_decomposition": (lambda v: isinstance(v, dict) and isinstance(v.get("seasonal"), list) and len(v["seasonal"]) == 24 and all(_num(x) for x in v["seasonal"])
                                   and _num(v.get("residual_median")) and _num(v.get("spread")) and v["spread"] > 0, ("trend_points",)),
    }
    if algorithm not in shapes:
        raise ValueError(f"unknown algorithm {algorithm}")
    shape, extra = shapes[algorithm]
    _check_univariate(state, features, shape, extra)
    if algorithm == "rolling_zscore" and not 4 <= state["window"] <= 2016:
        raise ValueError("rolling window out of bounds")
    if algorithm == "ewma" and not (0 < state["alpha"] < 1 and 1 <= state["clip"] <= 20):
        raise ValueError("EWMA parameters out of bounds")
    if algorithm == "seasonal_decomposition" and not 12 <= state["trend_points"] <= 4032:
        raise ValueError("trend window out of bounds")
