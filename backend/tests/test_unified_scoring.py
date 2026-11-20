"""The unified pipeline's pure parts (docs/UNIFIED_INVESTIGATION.md): the evidence contract, the signals read from evidence,
the hypothesis library, transparent scoring and the independent review. No database, no model."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from moks.intelligence import agents, evidence as contract, hypotheses as hy, review as rv, scoring
from moks.intelligence.collect import PROBES
from moks.intelligence.pipeline import Budget, Target
from moks.intelligence.signals import Signal, change, extract, trace_ids

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
SCOPE = contract.Scope(investigation_id="i-1", environment="prod", principal_id="u-1", time_from=NOW - timedelta(hours=1), time_to=NOW)
TARGET = Target(environment="prod", namespace="shop", workload="payments", service="payments", pods=frozenset({"payments-7f9c8d5b6-abcde"}))


def rec(ref: str, tool: str, data, args=None, ok: bool = True, **kw) -> dict:
    return {"ref": ref, "tool": tool, "args": args or {"namespace": "shop"}, "ok": ok, "summary": f"{tool} result", "data": data, "error": kw.get("error"),
            "denied": kw.get("denied", False), "truncated": kw.get("truncated", False), "kind": kw.get("kind"), "collected_at": (NOW - timedelta(minutes=1)).isoformat(),
            "source": kw.get("source", {})}


def series(values: list[float], start: datetime = NOW - timedelta(minutes=60), step: int = 60) -> list[list[float]]:
    return [[(start + timedelta(seconds=i * step)).timestamp(), v] for i, v in enumerate(values)]


# ---- the contract ---------------------------------------------------------------------------------------

def test_the_contract_describes_a_read_from_the_row_alone():
    item = contract.from_record(rec("E3", "k8s_pod_logs", "ERROR boom", {"namespace": "shop", "pod": "payments-7f9c8d5b6-abcde", "previous": True}), SCOPE, now=NOW)
    assert item.contract == "moks.evidence/v1" and item.source_type == "logs" and item.service == "payments"
    assert item.resource.uri == "k8s://prod/shop/Pod/payments-7f9c8d5b6-abcde" and item.resource.kind == "Pod"
    assert item.raw_ref.api == "/investigations/i-1/evidence/E3" and item.access.decision == "allow" and item.access.untrusted_content
    assert item.confidence.level == "medium" and item.confidence.basis[0].startswith("source logs: 0.75")
    with pytest.raises(ValidationError):
        contract.EvidenceItem.model_validate({**item.model_dump(), "model_says": "trust me"})  # nothing outside the contract


def test_failed_refused_and_truncated_reads_say_so():
    failed = contract.from_record(rec("E1", "prom_query", None, ok=False, error="timeout"), SCOPE, now=NOW)
    assert failed.status == "failed" and failed.confidence.level == "none" and "timeout" in failed.observation
    denied = contract.from_record(rec("E2", "k8s_list_pods", None, {"namespace": "secure"}, ok=False, denied=True, error="no read access", kind="access_denied"), SCOPE, now=NOW)
    assert denied.status == "denied" and denied.access.decision == "deny" and denied.access.reason == "no read access" and denied.source_ref.connector_type == "policy"
    cut = contract.from_record(rec("E3", "k8s_triage", {}, truncated=True), SCOPE, now=NOW)
    assert cut.status == "partial" and cut.confidence.score == round(0.95 * 0.7, 2)
    stale = contract.from_record({**rec("E4", "k8s_triage", {}), "collected_at": (NOW - timedelta(hours=2)).isoformat()}, SCOPE, now=NOW)
    assert any("freshness" in b for b in stale.confidence.basis)


def test_knowledge_and_shadow_models_are_weak_evidence():
    kb = contract.from_record(rec("E5", "kb_search", [], {"query": "pool", "k": 4}), SCOPE, now=NOW)
    assert kb.source_type == "knowledge" and kb.resource.uri.startswith("knowledge://prod") and kb.confidence.score == 0.3
    ml = contract.from_record(rec("E6", "ml_observations", [{"mode": "shadow", "status": "scored"}], {"namespace": "shop", "service": "payments"}), SCOPE, now=NOW)
    assert ml.source_type == "ml_anomaly" and ml.confidence.score == 0.35 and ml.resource.uri == "ml://prod/shop/payments"
    trace = contract.from_record(rec("E7", "loki_search", {"lines": []}, {"namespace": "shop", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}), SCOPE, now=NOW)
    assert trace.source_type == "traces"


def test_the_schema_is_published():
    s = contract.schema()
    assert s["$id"] == "moks.evidence/v1"
    assert {"id", "investigation_id", "source_type", "source_ref", "resource", "environment", "timestamp", "time_range", "observation", "raw_ref", "confidence", "access"} <= set(s["required"])


# ---- signals --------------------------------------------------------------------------------------------

def test_change_finds_the_step_and_its_onset():
    pts = series([0.2] * 30 + [1.5] * 20)
    c = change(pts)
    assert c.ratio > 5 and c.early == pytest.approx(0.2) and c.late == pytest.approx(1.5)
    assert c.onset == datetime.fromtimestamp(pts[30][0], timezone.utc)
    flat = change(series([0.2] * 50))
    assert flat.ratio == pytest.approx(1.0) and flat.onset is None
    assert change(series([1, 2])) is None


def test_metrics_show_presence_and_absence():
    data = {"signals": {"latency_p95": {"series": [{"metric": {"workload": "payments"}, "values": series([0.2] * 30 + [1.4] * 20)}]},
                        "error_ratio": {"series": [{"metric": {"workload": "payments"}, "values": series([0.0] * 50)}]},
                        "request_rate": {"series": [{"metric": {"workload": "payments"}, "values": series([40.0] * 50)}]},
                        "db_connections": {"series": [{"metric": {"workload": "payments"}, "values": series([4.0] * 30 + [10.0] * 20)}]},
                        "db_connections_max": {"series": [{"metric": {"workload": "payments"}, "values": series([10.0] * 50)}]}}}
    sig = {(s.key, s.present) for s in extract(rec("E7", "prom_golden_signals", data), TARGET)}
    assert {("metric.latency_increase", True), ("metric.db_connections_saturated", True), ("metric.db_connections_increase", True)} <= sig
    assert ("metric.error_rate_increase", False) in sig and ("metric.traffic_surge", False) in sig
    onset = next(s for s in extract(rec("E7", "prom_golden_signals", data), TARGET) if s.key == "metric.latency_increase").at
    assert onset == datetime.fromtimestamp(series([0] * 31)[30][0], timezone.utc)


def test_logs_are_read_for_named_conditions_and_trace_ids():
    data = {"lines": [{"ts": (NOW - timedelta(minutes=5)).isoformat(), "labels": {"pod": "payments-7f9c8d5b6-abcde"},
                       "line": "ERROR HikariPool-1 - Connection is not available trace_id=4bf92f3577b34da6a3ce929d0e0e4736"},
                      {"ts": (NOW - timedelta(minutes=4)).isoformat(), "labels": {"pod": "cart-1"}, "line": "ERROR java.lang.OutOfMemoryError"}]}
    r = rec("E8", "loki_search", data, {"namespace": "shop", "errors_only": True})
    keys = {s.key for s in extract(r, TARGET)}
    assert "log.db_pool_exhausted" in keys and "log.oom" not in keys  # another workload's lines are not the target's
    assert trace_ids(r) == ["4bf92f3577b34da6a3ce929d0e0e4736"]


def test_a_failed_read_gives_no_signal():
    assert extract(rec("E9", "k8s_list_pods", None, ok=False), TARGET) == []


def test_rollouts_in_and_out_of_the_window():
    window = (NOW - timedelta(hours=1), NOW)
    inside = {"revisions": [{"revision": 8, "created": (NOW - timedelta(minutes=20)).isoformat(), "images": ["p:2"], "replicaSet": "p-8"},
                            {"revision": 7, "created": (NOW - timedelta(days=3)).isoformat(), "images": ["p:1"], "replicaSet": "p-7"}]}
    sig = extract(rec("E1", "k8s_rollout_history", inside), TARGET, window)
    assert {s.key for s in sig} == {"deploy.rollout", "deploy.image_change"} and all(s.present for s in sig)
    old = {"revisions": [inside["revisions"][1]]}
    [absent] = extract(rec("E2", "k8s_rollout_history", old), TARGET, window)
    assert absent.key == "deploy.rollout" and absent.present is False


# ---- hypotheses and scoring -------------------------------------------------------------------------------

def item(ref: str, tool: str, args=None) -> contract.EvidenceItem:
    return contract.from_record(rec(ref, tool, {}, args), contract.Scope(**{**SCOPE.__dict__, "target": TARGET}), now=NOW)


def pool_world(contradict: bool = False) -> tuple[list[Signal], dict]:
    at = NOW - timedelta(minutes=17)
    sig = [Signal("metric.latency_increase", "E7", "metrics", at=at, detail="p95 rose"), Signal("log.db_pool_exhausted", "E8", "logs", at=at + timedelta(minutes=1)),
           Signal("metric.db_connections_saturated", "E7", "metrics", at=at), Signal("deploy.rollout", "E3", "deployment", at=at - timedelta(minutes=3), detail="revision 8"),
           Signal("history.cause:connection_pool", "E15", "historical_incident", value=0.6)]
    if contradict:
        sig.append(Signal("log.dns_failure", "E9", "logs", at=at))
    items = {"E7": item("E7", "prom_golden_signals", {"namespace": "shop", "workload": "payments"}), "E8": item("E8", "loki_search", {"namespace": "shop", "pod": "payments-7f9c8d5b6-abcde"}),
             "E3": item("E3", "k8s_rollout_history", {"namespace": "shop", "deployment": "payments"}), "E15": item("E15", "similar_incidents", {"k": 5}),
             "E9": item("E9", "loki_search", {"namespace": "shop", "pod": "payments-7f9c8d5b6-abcde", "contains": "dns"})}
    return sig, items


def scored(sig, items, onset=None) -> list[hy.Hypothesis]:
    ctx = scoring.Context(items=items, workload="payments", namespace="shop", service="payments", pods=TARGET.pods)
    hyps = hy.generate(sig, "payments")
    for h in hyps:
        h.score = scoring.score(h, sig, ctx, onset)
    return sorted(hyps, key=lambda h: -h.score["score"])


def test_a_score_is_its_named_factors_and_never_a_probability():
    sig, items = pool_world()
    top = scored(sig, items)[0]
    assert top.pattern == "connection_pool_exhaustion"
    s = top.score
    raw = sum(f["contribution"] for f in s["factors"]) - sum(p["value"] for p in s["penalties"])
    assert s["score"] == int(round(100 * max(0, min(1, raw)))) and s["not_a_probability"] is True and s["method"] == "moks.scoring/v1"
    assert next(f for f in s["factors"] if f["name"] == "historical_similarity")["value"] == 0.6
    assert next(f for f in s["factors"] if f["name"] == "temporal_correlation")["value"] == 1.0


def test_contradicting_evidence_lowers_the_score():
    sig, items = pool_world()
    clean = scored(sig, items)[0].score["score"]
    sig2, items2 = pool_world(contradict=True)
    against = next(h for h in scored(sig2, items2) if h.pattern == "connection_pool_exhaustion")
    assert against.score["score"] < clean and against.contradicting[0].ref == "E9"


def test_a_cause_after_its_effect_scores_low_on_timing():
    sig, items = pool_world()
    late = [s if s.key != "deploy.rollout" else Signal("deploy.rollout", "E3", "deployment", at=NOW, detail="revision 9") for s in sig]
    top = next(h for h in scored(late, items) if h.pattern == "connection_pool_exhaustion")
    t = next(f for f in top.score["factors"] if f["name"] == "temporal_correlation")
    assert t["value"] == 0.1 and "after the onset" in t["explanation"]


def test_missing_evidence_says_whether_it_was_read():
    sig = [Signal("metric.latency_increase", "E7", "metrics", at=NOW)]
    items = {"E7": item("E7", "prom_golden_signals", {"namespace": "shop"})}
    [h] = [x for x in hy.generate(sig, "payments") if x.pattern == "connection_pool_exhaustion"]
    unread = scoring.score(h, sig, scoring.Context(items=items, probe_tools={k: (p.tool,) for k, p in PROBES.items()}), None)
    read = scoring.score(h, sig, scoring.Context(items=items, read_tools={"prom_golden_signals"}, probe_tools={k: (p.tool,) for k, p in PROBES.items()}), None)
    miss = lambda s: next(p for p in s["penalties"] if p["name"] == "missing_evidence")  # noqa: E731
    assert miss(unread)["value"] == scoring.MISSING_UNREAD and "not collected" in miss(unread)["explanation"]
    assert miss(read)["value"] == scoring.MISSING_INCONCLUSIVE and "inconclusive" in miss(read)["explanation"]


def test_an_agents_hypothesis_cannot_be_strong_on_its_own():
    sig, items = pool_world()
    known = scored(sig, items)
    h, rejected = hy.from_agent({"pattern": "other", "title": "Postgres failover", "statement": "the primary failed over", "evidence": ["E7", "E8", "E3", "E999"]}, known, items, "payments")
    assert rejected == ["E999"] and h.pattern == "agent" and h.origin == "agent"
    s = scoring.score(h, sig, scoring.Context(items=items), None)
    assert s["score"] <= scoring.AGENT_ONLY_CAP and s["band"] != "strong"
    none, bad = hy.from_agent({"pattern": "other", "title": "made up", "evidence": ["E404"]}, known, items, "payments")
    assert none is None and bad == ["E404"]
    merged, _ = hy.from_agent({"pattern": "connection_pool_exhaustion", "evidence": ["E15"]}, known, items, "payments")
    assert merged is known[0] and merged.origin == "rules+agent"


def test_a_symptom_is_explained_by_the_cause_and_a_cause_is_not_explained_by_its_effect():
    at = NOW - timedelta(minutes=20)
    sig = [Signal("metric.traffic_surge", "E1", "metrics", at=at), Signal("metric.latency_increase", "E1", "metrics", at=at), Signal("metric.cpu_throttling", "E2", "metrics", at=at),
           Signal("k8s.probe_failed", "E3", "kubernetes", at=at)]
    items = {r: item(r, t, {"namespace": "shop", "pod": "payments-7f9c8d5b6-abcde"}) for r, t in (("E1", "prom_golden_signals"), ("E2", "prom_pod_resources"), ("E3", "k8s_events"))}
    ranked = scored(sig, items)
    scoring.explain_away(ranked)
    by = {h.pattern: h for h in ranked}
    assert by["traffic_surge"].explained_by is None  # upstream of CPU saturation, not explained by it
    assert by["readiness_failure"].explained_by is not None


def test_review_checks_citations_and_scope_and_the_agent_can_only_lower():
    sig, items = pool_world()
    items["E20"] = contract.from_record(rec("E20", "k8s_list_pods", [], {"namespace": "other"}), SCOPE, now=NOW)
    top = scored(sig, items)[0]
    top.agent_refs = ["E20", "E404"]
    code = rv.review(top, items, sig, environment="prod", namespaces={"shop"})
    assert code["verdict"] == "supported"
    assert "E20" in code["out_of_scope"] and any(c["ref"] == "E404" and not c["valid"] for c in code["citations"])
    lowered = rv.merge_agent(top, code, {"verdict": "contradicted", "contradicting": ["E3", "E777"]}, items)
    assert lowered["verdict"] == "contradicted" and lowered["agent"]["citations_rejected"] == ["E777"] and "E3" in top.agent_contradicting
    weak = {**code, "verdict": "partially_supported"}
    assert rv.merge_agent(top, weak, {"verdict": "supported"}, items)["verdict"] == "partially_supported"  # never raised
    capped = scoring.score(top, sig, scoring.Context(items=items), None) if not top.review else None
    top.review = lowered
    assert scoring.score(top, sig, scoring.Context(items=items), None)["score"] <= scoring.VERDICT_CAPS["contradicted"]
    assert capped is None or capped


def test_only_allowlisted_probes_reach_the_executor():
    ok, refused = agents.probe_requests({"probes": ["pool_logs", "exec_shell", "pool_logs"], "hypotheses": [{"probes": ["describe_pod", "kubectl delete pod"]}]}, set(PROBES), 3)
    assert ok == ["pool_logs", "describe_pod"] and refused == ["exec_shell", "kubectl delete pod"]


def test_budget_shares_scale_down_and_never_exceed_the_limit():
    b = Budget(tool_calls=10, seconds=60)
    assert sum(b.shares.values()) <= 12 and all(v >= 1 for k, v in b.shares.items() if k in ("detect", "identify", "telemetry"))
    for _ in range(10):
        b.spend("telemetry")
    assert "used up" in b.can_call("telemetry") and "used up" in b.can_call("detect")
