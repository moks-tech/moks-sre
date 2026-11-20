"""The SRE intelligence engine: feature catalog and provenance, the seven detectors, the evaluation pipeline,
incident correlation, historical intelligence, governance (snapshots, reproduction, health) and the CrewAI evidence
path. Synthetic series only; nothing here has seen production data.
"""
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from moks.db import session_scope
from moks.ml import anomaly, benchmark, correlate, detectors, features, history, registry
from moks.models import LearningRecord, MLCorrelation, MLModel, MLObservation
from moks.orchestrator import evidence as ev
from tests.conftest import login, make_env, make_user
from tests.test_ml_anomaly_skills import NOW, FakePrometheus, env_with_admin, set_mode

STEP = 300


# ---- features: catalog, sources, sampling, provenance -----------------------------------------------------------


def test_every_feature_query_parses():
    promql = pytest.importorskip("promql_parser")
    qs = {**features.queries("shop", {"queue_depth": "rabbitmq_queue_messages", "db_connections": "db_client_connections_usage"}), **features.node_queries(step=600, rate_window=900)}
    for name, q in qs.items():
        promql.parse(q)
    for sel, _ in features._bases("shop", {"queue_depth": "q"}).values():
        promql.parse(features.probe_query(sel))
    for sel, _ in features._bases(None, None).values():
        promql.parse(features.probe_query(sel))
    promql.parse(correlate.edges_query(["shop", "billing"], 7200))
    assert correlate.errors_query("shop", 3600).startswith('sum by (workload) (label_replace(count_over_time({namespace="shop"}')  # LogQL: shape only


def test_utilization_counts_only_containers_with_a_limit_and_windows_follow_the_step():
    q = features.queries("shop", step=600, rate_window=300)
    assert "container_spec_memory_limit_bytes" in q["memory_utilization"] and "> 0" in q["memory_utilization"]  # a sidecar's limit never divides the app's usage
    assert "[600s]" in q["request_rate"]  # the rate window is never shorter than the step
    assert "queue_depth" not in q and "db_connections" not in q  # optional gauges are asked for only when configured
    assert features.queries("shop", {"queue_depth": "q_depth"})["queue_depth"].startswith("sum by (workload) (label_replace(max_over_time(q_depth")
    with pytest.raises(ValueError):
        features.queries("shop", {"queue_depth": 'x"} or vector(1)'})


def _raw(name, wl, ts, f):
    return {name: [{"metric": {"workload": wl}, "values": [[t, f(t)] for t in ts if f(t) is not None]}]}


def test_traffic_comes_from_one_source_per_cohort_and_inference_keeps_the_pinned_one():
    ts = list(range(0, 3600, STEP))
    raw = {**_raw("request_rate", "w", ts, lambda t: 10.0 if t < 600 else None), **_raw("mesh_request_rate", "w", ts, lambda t: 12.0),
           **_raw("mesh_errors_rate", "w", ts, lambda t: 1.2), **_raw("memory_working_set", "w", ts, lambda t: 1e8)}
    auto = features.assemble("e", "n", STEP, ts, raw)[0]
    assert auto.quality["sources"]["traffic"] == "mesh" and auto.values["error_ratio"] == pytest.approx([0.1] * len(ts))  # the app covers too little of the window
    assert auto.provenance["features"]["request_rate"]["source"] == "prometheus:mesh" and auto.provenance["features"]["memory_working_set"]["source"] == "prometheus:cadvisor"
    pinned = features.assemble("e", "n", STEP, ts, raw, pins={"w": {"traffic": "app"}})[0]
    assert pinned.values["request_rate"][:2] == [10.0, 10.0] and pinned.values["request_rate"][2:] == [None] * (len(ts) - 2)  # never silently swapped to the mesh


def test_a_counter_sampled_too_rarely_is_reported_and_not_used():
    ts = list(range(0, 7200, STEP))
    raw = {**_raw("request_rate", "w", ts, lambda t: 5.0), **_raw("memory_working_set", "w", ts, lambda t: 1e8)}
    probes = {"requests": {"state": "present", "queries": ["request_rate", "errors_rate"], "interval_seconds": 240.0, "selector": "x"},
              "container_memory_working_set_bytes": {"state": "present", "queries": ["memory_working_set", "memory_utilization"], "interval_seconds": 30.0, "selector": "y"}}
    fr = features.assemble("e", "n", STEP, ts, raw, probes=probes)[0]
    assert "request_rate" in fr.quality["undersampled"] and "memory_working_set" not in fr.quality["undersampled"]
    assert "request_rate" not in features.usable(fr) and "memory_working_set" in features.usable(fr)
    assert fr.provenance["features"]["request_rate"]["native_interval_seconds"] == 240.0 and fr.provenance["features"]["request_rate"]["rate_window_seconds"] == 300


async def test_the_probe_skips_absent_metrics_and_says_so():
    prom = FakePrometheus(absent={"restarts"})
    c = await features.collect_raw(prom, "e", "shop", NOW - timedelta(hours=2), NOW, STEP)
    assert c.probes["restarts"]["state"] == "absent" and c.probes["requests"]["interval_seconds"] == 30.0
    assert all(outcome == "absent" for _, outcome in c.status["restarts"]) and "restarts" not in {x[0] for x in prom.calls}
    fr = features.frames_of(c)[0]
    assert "restart_rate" in fr.quality["absent"] and "no series for" in fr.quality["absent_reasons"]["restart_rate"]


def test_nodes_are_their_own_cohorts_at_the_infrastructure_level():
    ts = list(range(0, 3600, STEP))
    raw = {"node_cpu": [{"metric": {"node": "w1"}, "values": [[t, 0.4] for t in ts]}], "node_memory": [{"metric": {"node": "w1"}, "values": [[t, 0.6] for t in ts]}],
           "node_pressure": []}
    fr = features.assemble("e", "ignored", STEP, ts, raw, nodes=True)[0]
    assert fr.cohort == "e/_node/w1" and fr.level == "infrastructure" and fr.features == features.NODE_FEATURES
    assert fr.values["node_pressure"] == [None] * len(ts) and "node_pressure" in fr.quality["absent"]  # no kube-state-metrics is not "no pressure"


# ---- detectors --------------------------------------------------------------------------------------------------


def cohort(name="cart"):
    c = benchmark.generate(5, days=10, test_days=3)
    return c, c.frames[f"synthetic/shop/{name}"]


@pytest.mark.parametrize("alg", detectors.NAMES)
def test_every_detector_has_json_state_that_validates_and_fits_deterministically(alg):
    _, fr = cohort("cart")
    train = fr.slice(fr.timestamps[0], fr.timestamps[288 * 7])
    train.quality = benchmark._quality(train).quality
    a, b = detectors.fit(alg, train, seed=1), detectors.fit(alg, train, seed=1)
    assert json.loads(json.dumps(a, allow_nan=False)) == a == b
    detectors.validate(alg, a, list(features.FEATURES))
    bad = json.loads(json.dumps(a))
    bad["features"] = {"not_a_feature": 1} if isinstance(bad["features"], dict) else ["not_a_feature"]
    with pytest.raises((ValueError, KeyError, TypeError)):
        detectors.validate(alg, bad, list(features.FEATURES))


@pytest.mark.parametrize("alg", ["rolling_zscore", "ewma", "seasonal_decomposition", "robust_zscore", "local_outlier_factor"])
def test_scores_never_depend_on_later_points(alg):
    _, fr = cohort("web")
    split = 288 * 7
    train = benchmark._quality(fr.slice(fr.timestamps[0], fr.timestamps[split]))
    state = detectors.fit(alg, train, seed=0)
    before = detectors.pointwise(alg, state, fr, split)
    later = fr.slice(fr.timestamps[0], fr.timestamps[-1] + STEP)
    for f in later.features:
        later.values[f] = [v if i < split + 100 or v is None else v * 50 for i, v in enumerate(later.values[f])]
    after = detectors.pointwise(alg, state, later, split)
    cut = lambda p: {f: v[:100] for f, v in p.items()} if isinstance(p, dict) else p[:100]
    assert cut(before) == cut(after)


def test_direction_matters_lower_latency_is_not_a_problem_lower_traffic_is():
    _, fr = cohort("web")
    split = 288 * 7
    state = detectors.fit("seasonal_baseline", benchmark._quality(fr.slice(fr.timestamps[0], fr.timestamps[split])))  # it knows the daily cycle
    for f, factor, expect in (("latency_p95", 0.05, False), ("request_rate", 0.02, True)):
        x = fr.slice(fr.timestamps[0], fr.timestamps[-1] + STEP)
        x.values = {k: list(v) for k, v in fr.values.items()}
        for i in range(split + 20, split + 40):
            x.values[f][i] = x.values[f][i] * factor
        r = detectors.score("seasonal_baseline", state, x.slice(x.timestamps[split + 18], x.timestamps[split + 30]))
        assert (f in r["flagged_features"]) is expect, (f, r["deviations"][:2])


def test_a_one_point_blip_is_not_an_anomaly_but_a_sustained_change_is():
    zs = [0.1, 8.0, 0.2, 0.1, 5.0, 6.0, 7.0]
    assert detectors.first_flag(zs, 3.0, 2) == 5 and detectors.sustained(zs, 2)[0] == 6.0
    assert detectors.first_flag([0.0, -9.0, -9.0], 3.0, 2, "higher") is None


# ---- evaluation ---------------------------------------------------------------------------------------------------


def test_event_scoring_counts_alarms_delays_and_benign_changes_exactly():
    events = [benchmark.Event("c", 20, 25, "spike", ("x",), "i1"), benchmark.Event("c", 60, 62, "rollout", ("x",), benign=True), benchmark.Event("c", 90, 95, "shift", ("x",), "i2")]
    flag = lambda on: {"unusual": on}
    flags = [(b - 12, b, flag(b in (24, 27, 63, 120))) for b in range(12, 130, 3)]
    m = benchmark.combine([benchmark.score_events(flags, events)])
    assert (m["faults"], m["detected"], m["alarms"], m["true_alarms"], m["benign_alarms"], m["false_alarms"]) == (2, 1, 3, 1, 1, 1)  # 24 and 27 are one alarm
    assert m["recall"] == 0.5 and m["precision"] == 0.5 and m["f1"] == 0.5 and m["median_delay_seconds"] == 4 * STEP
    assert 0 < m["fpr"] < 0.1


def test_correlation_accuracy_is_measured_on_pairs():
    truth = {"a": "i1", "b": "i1", "c": "i2", "d": "noise"}
    assert benchmark.correlation_scores(truth, {"a": "g1", "b": "g1", "c": "g2", "d": "g3"})["pair_f1"] == 1.0
    split = benchmark.correlation_scores(truth, {"a": "g1", "b": "g2", "c": "g3", "d": "g4"})
    assert split["pair_recall"] == 0.0 and split["groups"] == 4
    assert benchmark.adjusted_rand(truth, {"a": "g1", "b": "g1", "c": "g2", "d": "g3"}) == 1.0


def test_the_benchmark_compares_detectors_and_scores_correlation():
    r = benchmark.run(seed=3, algorithms=("robust_zscore", "ewma"), days=10, test_days=3)
    assert set(r["algorithms"]) == {"robust_zscore", "ewma"} and r["ranking"]
    for m in r["algorithms"].values():
        assert {"recall", "precision", "f1", "fpr", "median_delay_seconds", "inference_ms_median"} <= set(m)
    assert r["correlation"]["oracle"]["pair_f1"] == 1.0 and r["correlation"]["detected"]["pair_precision"] >= 0.8
    assert "not how it performs on real workloads" in r["note"]


def test_injected_faults_are_sized_by_the_cohorts_own_spread():
    _, fr = cohort("postgres")
    out, events = benchmark.inject(fr, 288 * 7, random.Random(1), count=6)
    assert len(events) == 6 and {e.kind for e in events} == {"spike", "level_shift", "drift"}
    e = events[0]
    f = e.features[0]
    assert out.values[f][e.start] != fr.values[f][e.start] and out.values[f][e.start - 1] == fr.values[f][e.start - 1]
    assert fr.values[f][e.start] is not None


# ---- correlation ------------------------------------------------------------------------------------------------


def A(wl, first, ns="shop", feats=("latency_p95",)):
    return correlate.Anomaly(key=(f"e/{ns}/{wl}", first), environment="e", namespace=ns, workload=wl, first=first, last=first + 1800, features=feats, score=5.0,
                             level="infrastructure" if ns == "_node" else "service")


def test_dependent_anomalies_become_one_group_and_unrelated_ones_do_not():
    t = 1_800_000_000
    topo = correlate.Topology(edges={(("shop", "web"), ("shop", "cart")), (("shop", "cart"), ("shop", "postgres"))},
                              placement={("shop", "cart"): {"w1"}, ("shop", "web"): {"w2"}, ("shop", "postgres"): {"w2"}, ("other", "batch"): {"w3"}},
                              rollouts={("shop", "postgres"): [t - 1200]})
    anomalies = [A("postgres", t), A("cart", t + 300), A("web", t + 600), A("batch", t + 300, ns="other"), A("w1", t + 100, ns="_node", feats=("node_memory_utilization",)),
                 A("cart", t + 9000)]
    groups = correlate.group(anomalies, topo)
    by = {frozenset(m.workload for m in g.members) for g in groups}
    assert frozenset({"postgres", "cart", "web", "w1"}) in by and frozenset({"batch"}) in by and frozenset({"cart"}) in by
    main = next(g for g in groups if len(g.members) == 4)
    kinds = [h["kind"] for h in main.hypotheses]
    assert {"rollout", "dependency", "node"} <= set(kinds) and all(h["status"] == "candidate" and not h["validated"] for h in main.hypotheses)
    reasons = {r["type"] for ln in main.links for r in ln.reasons}
    assert {"calls", "node", "rollout"} <= reasons


def test_a_shared_namespace_and_good_timing_alone_do_not_join_two_anomalies():
    t = 1_800_000_000
    groups = correlate.group([A("a", t), A("b", t + 60)], correlate.Topology())
    assert len(groups) == 2


async def _obs(env, ns, wl, at, score=6.0, role="champion", feats=("error_ratio",)):
    async with session_scope() as db:
        o = MLObservation(id=uuid.uuid4(), capability="anomaly_detection", algorithm="robust_zscore", environment=env, namespace=ns, service=wl, cohort=f"{env}/{ns}/{wl}",
                          window_from=at - timedelta(hours=1), window_to=at, status="scored", mode="shadow", score=score, threshold=3.0, unusual=True,
                          deviations=[{"feature": feats[0], "z": score, "observed": 1, "typical": 0, "direction": "higher"}], data_quality={},
                          context={"role": role, "first_flagged_at": (at - timedelta(minutes=10)).isoformat(), "flagged_features": list(feats), "model_version": 1},
                          fingerprint=uuid.uuid4().hex)
        db.add(o)
    return o


async def test_groups_are_stored_updated_in_place_closed_and_shown_only_to_readers_of_every_namespace(client):
    env, admin = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    t = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=6)  # the API lists the last hours of the real clock
    topo = correlate.Topology(edges={(("shop", "web"), ("shop", "cart"))})
    try:
        await _obs(env, "shop", "cart", t)
        await _obs(env, "shop", "web", t + timedelta(minutes=5))
        await _obs(env, "shop", "web", t + timedelta(minutes=5), role="challenger")  # challengers never drive grouping
        first = await correlate.run(env, now=t + timedelta(minutes=10), topo_override=topo)
        await _obs(env, "shop", "web", t + timedelta(minutes=20))
        again = await correlate.run(env, now=t + timedelta(minutes=25), topo_override=topo)
        async with session_scope() as db:
            rows = (await db.execute(select(MLCorrelation).where(MLCorrelation.environment == env))).scalars().all()
            assert len(rows) == 1 and {m["workload"] for m in rows[0].members} == {"cart", "web"} and rows[0].status == "open"
            assert rows[0].hypotheses[0]["kind"] == "dependency" and "cart" in rows[0].hypotheses[0]["statement"]
        closed = await correlate.run(env, now=t + timedelta(hours=5), topo_override=topo)
    finally:
        await set_mode("anomaly_detection", "disabled")
    assert first["created"] == 1 and again["created"] == 0 and again["updated"] == 1 and closed["closed"] == 1
    h = await login(client, admin)
    groups = (await client.get("/moks/api/ml/correlations", params={"environment": env, "hours": 24}, headers=h)).json()["groups"]
    assert len(groups) == 1 and "unvalidated" in groups[0]["note"]
    detail = (await client.get(f"/moks/api/ml/correlations/{groups[0]['id']}", headers=h)).json()
    assert "similar_incidents" in detail and "history" in detail
    r = await client.post(f"/moks/api/ml/correlations/{groups[0]['id']}/feedback", json={"verdict": "correct"}, headers=h)
    assert r.status_code == 200 and r.json()["feedback"]["verdict"] == "correct"
    other = f"o-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(other, [f"viewer:{env}:billing"])
    ho = await login(client, other)
    assert (await client.get("/moks/api/ml/correlations", params={"environment": env, "hours": 24}, headers=ho)).json()["groups"] == []
    assert (await client.get(f"/moks/api/ml/correlations/{groups[0]['id']}", headers=ho)).status_code == 404


# ---- history ------------------------------------------------------------------------------------------------------


async def _record(env, ns, service, *, kind="incident", state="approved", symptoms=("oomkilled", "crashloop"), signature=("memory", "limit"), verified=True,
                  resolution="raised the memory limit", days_ago=10, resource=None):
    async with session_scope() as db:
        r = LearningRecord(id=uuid.uuid4(), schema_version=1, kind=kind, environment=env, namespace=ns, service=service, resource=resource, realm="operational",
                           event_from=datetime.now(timezone.utc) - timedelta(days=days_ago),
                           features={"symptoms": list(symptoms), "signature": list(signature), "services": [service], "resource_kinds": ["pod"] if not resource else [resource.split("/")[0].lower()]},
                           label={"confirmed_cause": "heap above the limit", "resolution": resolution, "verification": "verified" if verified else "unverified"},
                           label_state=state, outcome={"resolution": "resolved", "resolved_at": (datetime.now(timezone.utc) - timedelta(days=days_ago) + timedelta(hours=2)).isoformat()},
                           versions={}, fingerprint=uuid.uuid4().hex, content_hash=uuid.uuid4().hex)
        db.add(r)


async def test_history_uses_reviewed_readable_records_only():
    env = f"hi-{uuid.uuid4().hex[:6]}"
    await make_env(env)
    for d in (30, 20, 5):
        await _record(env, "shop", "payments", days_ago=d)
    await _record(env, "shop", "payments", state="unreviewed", days_ago=3)  # never counted
    await _record(env, "billing", "ledger", days_ago=4)  # outside the reader's scope
    await _record(env, "shop", "cart", symptoms=("evicted",), signature=("node", "pressure"), resource="Node/w2", days_ago=8, verified=False)
    await _record(env, "shop", "web", symptoms=("evicted",), signature=("node", "pressure"), resource="Node/w2", days_ago=2, verified=False)
    from moks.rbac import Binding, Principal

    reader = Principal(str(uuid.uuid4()), "r@example.com", [Binding("viewer", env, "shop")])
    async with session_scope() as db:
        out = await history.summarize(db, reader, environment=env)
        only = await history.summarize(db, reader, environment=env, service="payments")
    assert out["records"] == 5 and {s["service"] for s in out["services"]} == {"payments", "cart", "web"}  # not ledger, not the unreviewed one
    rec = out["recurring"][0]
    assert rec["occurrences"] == 3 and "oomkilled" in rec["pattern"]["symptoms"] and rec["median_hours_to_resolve"] == 2.0
    assert any(i["problem"] == "evicted" and i["occurrences"] == 2 for i in out["infrastructure"])
    assert out["what_worked"][0]["resolution"] == "raised the memory limit" and out["what_worked"][0]["times"] == 3  # unverified ones are not "what worked"
    assert only["records"] == 3 and "not what is happening now" in only["note"]


# ---- governance -------------------------------------------------------------------------------------------------


async def test_a_model_reproduces_from_its_snapshot_and_a_changed_snapshot_is_refused():
    env, _ = await env_with_admin()
    out = await anomaly.train(env, "shop", query=FakePrometheus(), now=NOW - timedelta(hours=2), algorithms=("ewma",))
    m = out["cohorts"][0]["models"][0]
    r = await anomaly.reproduce(uuid.UUID(m["id"]))
    assert r["reproducible"] and r["state_sha256"] == r["expected_state_sha256"]
    async with session_scope() as db:
        model = await db.get(MLModel, uuid.UUID(m["id"]))
        from moks.models import MLArtifact

        snap = await db.get(MLArtifact, model.training["snapshot_sha256"])
        snap.body = snap.body.replace(b'"step":300', b'"step":301')
    with pytest.raises(anomaly.artifacts.ArtifactError, match="checksum"):
        await anomaly.reproduce(uuid.UUID(m["id"]))


async def test_approved_incidents_are_masked_out_of_training():
    ts = list(range(0, 86400, STEP))
    fr = features.CohortFrame("e", "n", "w", STEP, ts, {"request_rate": [1.0] * len(ts)}, {}, ("request_rate",))
    start = datetime.fromtimestamp(36000, timezone.utc)
    n = anomaly._mask(fr, [{"start": start, "end": start + timedelta(minutes=30)}])
    masked = [t for t, v in zip(ts, fr.values["request_rate"]) if v is None]
    assert n == len(masked) and masked[0] == 36000 - 1800 and masked[-1] == 36000 + 1800


async def test_a_model_from_the_previous_schema_is_reported_not_scored():
    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        out = await anomaly.train(env, "shop", query=FakePrometheus(), now=NOW - timedelta(hours=2), algorithms=("robust_zscore",))
        mid = uuid.UUID(out["cohorts"][0]["models"][0]["id"])
        async with session_scope() as db:
            m = await db.get(MLModel, mid)
            m.feature_schema = "metrics-anomaly/1"
            m.metrics = {**m.metrics, "gates": [{"name": "x", "passed": True}]}
            m.status = "champion"
        await anomaly.tick(env, query=FakePrometheus(), now=NOW, correlate_after=False)
        async with session_scope() as db:
            o = (await db.execute(select(MLObservation).where(MLObservation.model_id == mid))).scalar_one()
            health = await anomaly.health(db, await db.get(MLModel, mid))
    finally:
        await set_mode("anomaly_detection", "disabled")
    assert o.status == "stale_schema" and not o.unusual and "Retrain" in o.data_quality["error"]
    assert any(r["reason"] == "stale_schema" for r in health["recommendations"]) and not health["healthy"]


async def test_observations_carry_type_confidence_validation_and_provenance():
    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        out = await anomaly.train(env, "shop", query=FakePrometheus(), now=NOW - timedelta(hours=2), algorithms=("robust_zscore",))
        async with session_scope() as db:
            m = await db.get(MLModel, uuid.UUID(out["cohorts"][0]["models"][0]["id"]))
            m.metrics = {**m.metrics, "gates": [{"name": "x", "passed": True}]}
            await registry.promote(db, m, None, "champion")
        await anomaly.tick(env, query=FakePrometheus(spike_after=int((NOW - timedelta(minutes=40)).timestamp())), now=NOW, correlate_after=False)
        async with session_scope() as db:
            o = (await db.execute(select(MLObservation).where(MLObservation.model_id == m.id))).scalar_one()
    finally:
        await set_mode("anomaly_detection", "disabled")
    c = o.context
    assert o.unusual and c["anomaly_type"] == "service" and c["first_flagged_at"] and c["flagged_features"]
    assert c["confidence"]["level"] in ("low", "medium") and c["validation"]["status"] == "unvalidated" and c["validation"]["mode"] == "shadow"
    assert o.data_quality["provenance"]["features"]["request_rate"]["source"] == "prometheus:app" and o.data_quality["history_points"] == 0
    d = next(x for x in o.deviations if x["flagged"])
    assert {"observed", "typical", "z", "threshold", "pattern", "first_flagged_at"} <= set(d)


# ---- investigations: evidence and the crew -------------------------------------------------------------------------


def test_ml_output_is_its_own_evidence_kind_and_never_an_observation():
    assert ev.classify(ok=True, tool="ml_anomalies") == ev.MODEL_OUTPUT and ev.classify(ok=True, tool="incident_history") == ev.DOCUMENTATION
    ledger = ev.Ledger.build([{"ref": "E1", "tool": "ml_anomalies", "ok": True, "kind": ev.MODEL_OUTPUT, "summary": "payments latency unusual", "data": {}, "args": {"namespace": "shop"},
                               "collected_at": datetime.now(timezone.utc).isoformat()}], namespace="shop")
    v = ev.judge("payments latency is elevated", ["E1"], ledger, [])
    assert v.disposition == "hypothesis" and v.kind == ev.MODEL_OUTPUT and "statistical lead" in v.reasons[-1]


def test_the_crew_cannot_turn_an_ml_signal_into_a_finding_or_a_confident_cause():
    from moks.crew.evidence import Registry, validate

    reg = Registry()
    reg.records = {"E1": {"id": "E1", "source": "ml_anomalies", "ok": True, "kind": ev.MODEL_OUTPUT, "excerpt": "", "truncated": False},
                   "E2": {"id": "E2", "source": "k8s_list_pods", "ok": True, "kind": ev.OBSERVATION, "excerpt": "", "truncated": False}}
    raw = {"summary": "s", "findings": [{"statement": "error ratio is anomalous", "evidence": ["E1"]}, {"statement": "pods restart", "evidence": ["E2"]}],
           "likely_causes": [{"cause": "the database is slow", "confidence": "high", "evidence": ["E1"]}]}
    report, decided = validate(raw, reg, [])
    assert [f.statement for f in report.observed_facts] == ["pods restart"]
    assert {c.cause: c.confidence for c in report.suspected_causes} == {"the database is slow": "low", "error ratio is anomalous": "low"}
    assert any("ML signals are leads" in x["reason"] for x in decided["confidence_lowered"])
    assert decided["ml_only_findings_demoted"] == [{"statement": "error ratio is anomalous", "evidence": ["E1"]}] and not decided["claims_removed"]
    assert any("resting only on ML signals" in n for n in report.notes)


def test_the_crew_tool_is_bound_to_the_target_and_refuses_another_namespace():
    from moks.crew import tools as ct

    t = ct.Target("e", "shop", "payments-7d9f8c6b5d-abcde", "Pod", 30, "")
    args = ct.bind(ct.BY_NAME["ml_signals"], t, {}, log_lines=50)
    assert args == {"namespace": "shop", "workload": "payments", "minutes": 60}
    assert ct.bind(ct.BY_NAME["incident_history"], ct.Target("e", "shop", "cart", "Deployment", 600, ""), {}, log_lines=50) == {"namespace": "shop", "service": "cart", "days": 180}
    with pytest.raises(ct.Refused):
        ct.bind(ct.BY_NAME["ml_signals"], t, {"namespace": "kube-system"}, log_lines=50)
    from moks.crew import config

    assert "unvalidated statistical leads" in config.rules("x") and "ml_signals" in config.TOOLS_BY_AGENT["workload"]


async def test_the_signals_tool_shows_only_what_the_reader_may_read(client):
    from moks.ml import signals
    from moks.rbac import Binding, Principal

    env, _ = await env_with_admin()
    await set_mode("anomaly_detection", "shadow")
    try:
        o = await _obs(env, "shop", "payments", NOW, feats=("latency_p95",))
        async with session_scope() as db:
            mine = await signals.for_target(db, Principal(str(uuid.uuid4()), "a@x", [Binding("viewer", env, "shop")]), env, "shop", "payments", NOW, 120)
            theirs = await signals.for_target(db, Principal(str(uuid.uuid4()), "b@x", [Binding("viewer", env, "billing")]), env, "shop", "payments", NOW, 120)
        await set_mode("anomaly_detection", "disabled")
        async with session_scope() as db:
            off = await signals.for_target(db, Principal(str(uuid.uuid4()), "a@x", [Binding("viewer", env, "shop")]), env, "shop", "payments", NOW, 120)
    finally:
        await set_mode("anomaly_detection", "disabled")
    a = mine["anomalies"][0]
    assert mine["status"] == "ok" and a["observation_id"] == str(o.id) and a["resource"] == "shop/payments"
    assert {"anomaly_type", "detected_at", "features", "model", "confidence", "validation"} <= set(a) and {"baseline", "observed", "deviation"} <= set(a["features"][0])
    assert theirs["anomalies"] == [] and off["status"] == "disabled"


async def test_the_anomaly_eval_kind_records_gates():
    out = await benchmark.run_eval({"cases": [{"seed": 3}], "algorithms": ["robust_zscore"], "days": 10, "gates": {"min_f1": 0.0, "max_fpr": 1.0, "min_correlation_f1": 0.0}, "description": "test"})
    assert out["results"][0]["best"] and {g["name"] for g in out["gates"]} == {"best_f1", "best_fpr", "correlation_pair_f1"} and all(g["passed"] for g in out["gates"])
    assert all("synthetic" in g["evidence"] for g in out["gates"])


def test_the_incident_crew_grades_an_ml_signal_as_a_lead_never_a_confirmed_observation():
    from moks.crew import incident_config
    from moks.crew import validation as val
    from moks.crew.schema import RawFindings
    from tests.test_crewai_validation import POD, case, finding, registry

    ml = {"tool": "ml_anomalies", "kind": ev.MODEL_OUTPUT, "agent": "observability",
          "data": {"anomalies": [{"resource": "shop/payments", "features": [{"feature": "error_ratio", "deviation": 9.0}]}]}}
    reg, views = registry(POD, ml)
    c = case()
    val.accept_findings(c, "observability", "observability", RawFindings(findings=[finding("payments error ratio is anomalous", "pod/payments-5c4-xyz", ["E2"]),
                                                                                  finding("CrashLoopBackOff", "pod/payments-5c4-xyz", ["E1"])]), reg, views)
    val.grade(c, reg, views)
    status = {f.observation: f.status for f in c.findings}
    assert status == {"payments error ratio is anomalous": "possible_explanation", "CrashLoopBackOff": "confirmed_observation"}
    assert "ml_signals" in incident_config.TOOLS_BY_AGENT["observability"] and "incident_history" in incident_config.TOOLS_BY_AGENT["knowledge"]
    assert "ml_signals" in incident_config.REQUESTABLE  # the reviewer may ask MOKS to collect it, through the same binding
