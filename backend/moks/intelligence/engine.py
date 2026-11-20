"""Runs the unified pipeline inside an Investigator, in place of the LangGraph graph or the workload crew.

Investigator._investigate calls run() when the queued request is the CrewAI console flow `unified_investigation`.
Everything before (authorization at run time, connectors, the AccessGuard, the lease, the cancel watcher) and after
(validate_claims, verify_causes, relevance, the stored report, the reply in the conversation) is the code every
investigation passes. run() returns graph-shaped state plus `unified`, the full report block.

Cancellation: the stage in progress ends `cancelled`, the pipeline's state so far is left on the Investigator as
`partial_payload`, and the CancelledError propagates; the Investigator stores it beside its partial report.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from .. import audit
from ..config import get_settings
from ..crew import CrewUnavailable
from ..crew.history import History
from ..observability import span
from . import ENGINE_VERSION, FLOW, agents as ag, collect, hypotheses as hy, report as rp, review as rv, scoring
from .evidence import Scope, from_record
from .pipeline import STAGES, Budget, Pipeline, Target

log = logging.getLogger("moks.intelligence")



def _items(c: collect.Collector, inv: Any, scope: Scope) -> dict[str, Any]:
    by_ref: dict[str, list[str]] = {}
    for s in c.signals:
        by_ref.setdefault(s.ref, []).append(s.key)
    scope.stages, scope.agents, scope.target = dict(c.stage_of), dict(c.by_of), c.target
    return {ref: from_record(rec, scope, signals=by_ref.get(ref)) for ref, rec in c.records.items()}


def _context(c: collect.Collector, items: dict[str, Any]) -> scoring.Context:
    t = c.target
    return scoring.Context(items=items, workload=t.workload, namespace=t.namespace, service=t.service, pods=t.pods, dependencies=t.dependencies,
                           read_tools=set(c.read_tools), probe_tools=collect.PROBE_TOOLS)


def _rank(hyps: list[hy.Hypothesis], signals: list, ctx: scoring.Context, onset: datetime | None) -> list[hy.Hypothesis]:
    for h in hyps:
        h.explained_by = None
        h.score = scoring.score(h, signals, ctx, onset)
    ranked = sorted(hyps, key=lambda h: (-h.score["score"], h.id))
    for h in scoring.explain_away(ranked):
        h.score = scoring.score(h, signals, ctx, onset)
    return sorted(ranked, key=lambda h: (-h.score["score"], h.id))


def _agents_block(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if k != "_runs"}


async def run(inv: Any, executor: Any, ctx: Any, question: str, namespace: str, scope_info: dict, provider: Any, cfg: Any, request: dict[str, Any]) -> dict[str, Any]:
    from ..crew import console
    from ..crew.runner import _release, _slot

    s = get_settings()
    history = await History(inv.id).open()
    await history.start("setup", "Set up the unified investigation")
    try:
        if request.get("trigger") != "benchmark":
            flow = await console.flow_state(FLOW)
            if not flow["enabled"]:
                raise CrewUnavailable("Unified investigation unavailable: the flow is switched off in the CrewAI console")
        if namespace == "*" or not ctx.scope.allows(namespace):
            raise CrewUnavailable(f"Unified investigation unavailable: namespace {namespace} is outside this person's scope")
    except CrewUnavailable as exc:
        history.line(str(exc), "error")
        await history.close("failed", str(exc))
        raise
    ctx.focus_namespace = inv.focus_namespace = namespace  # knowledge and similar-incident retrieval stay in the target's namespace
    minutes = max(1, int((ctx.time_to - ctx.time_from).total_seconds() // 60))
    target = Target(environment=ctx.environment.name, namespace=namespace, workload=request.get("workload") or None, kind=request.get("kind") or "Deployment",
                    repository=inv._repository(ctx), minutes=minutes)
    budget = Budget(tool_calls=min(s.unified_max_tool_calls, max(0, inv.tool_budget - inv.tool_calls)), seconds=max(3.0, min(s.unified_timeout_seconds, inv.time_left() - min(15.0, inv.time_left() * 0.2))))  # the rest is for validation and storing
    pipe = Pipeline(inv, history, budget, ENGINE_VERSION)
    c = collect.Collector(inv=inv, executor=executor, ctx=ctx, budget=budget, target=target, question=question, request=request, window=(ctx.time_from, ctx.time_to))
    scope = Scope(investigation_id=str(inv.id), environment=ctx.environment.name, principal_id=str(inv.owner) if inv.owner else None, time_from=ctx.time_from, time_to=ctx.time_to,
                  realm=request.get("realm") or "production", connector_ids=dict(inv.connector_ids), max_age_seconds=s.evidence_max_age_seconds)
    use_agents = bool(cfg is not None and provider is not None and request.get("agents", True) and ag.installed())
    agent_state: dict[str, Any] = {"mode": "crewai" if use_agents else "rules", "reason": None if use_agents else
                                   ("no active chat model" if cfg is None else "crewai is not installed at the pinned version in this process" if not ag.installed() else "agents switched off for this run"),
                                   "analyst": None, "reviewer": None}
    production = bool((inv.environment_info or {}).get("is_production"))
    history.line(f"Unified pipeline v{ENGINE_VERSION}: target {target.label()}, window {minutes} min, budget {budget.tool_limit} tool calls / {budget.seconds:.0f}s, "
                 f"reasoning {agent_state['mode']}", data={"budget": budget.out(), "agents": agent_state["mode"]})
    await history.finish("setup", "succeeded")
    classification: dict[str, Any] = {}
    ranked: list[hy.Hypothesis] = []
    remediation: list[dict] = []
    items: dict[str, Any] = {}
    status = "completed"
    slot = False
    block: dict[str, Any] | None = None
    report: Any = None

    def partial() -> dict[str, Any]:
        return {"engine": "unified", "version": ENGINE_VERSION, "status": "cancelled", "target": target.out(), "pipeline": pipe.out(), "budget": budget.out(),
                "gaps": [g.out() for g in c.gaps], "hypotheses": [{"id": h.id, "title": h.title, "score": h.score} for h in ranked],
                "evidence_items": [i.model_dump() for i in _items(c, inv, scope).values()]}

    with span("unified investigation", **{"moks.unified.version": ENGINE_VERSION, "moks.namespace": namespace, "moks.unified.agents": agent_state["mode"]}) as root:
        try:
            for key, fn in (("detect", collect.detect),):
                async with pipe.stage(key) as st:
                    if st.status == "running":
                        await fn(c, st)
            async with pipe.stage("classify") as st:
                classification = collect.classify(c, st, production)
            async with pipe.stage("identify") as st:
                if st.status == "running":
                    await collect.identify(c, st)
                    for rec in c.records.values():  # the target's detector findings: verify_causes and the report's findings table read them
                        if rec["tool"] == "k8s_triage" and rec.get("ok") and isinstance(rec.get("data"), dict):
                            for f in rec["data"].get("findings") or []:
                                if collect.sig._finding_mine(target if target.workload else None, f) and not any(x.get("title") == f.get("title") for x in inv.findings):
                                    inv.findings.append({**f, "namespace": namespace, "evidence": [rec["ref"]]})
            for key, fn in (("telemetry", collect.telemetry), ("traces", collect.traces), ("changes", collect.changes), ("anomalies", collect.anomalies),
                            ("history", collect.history), ("semantic", collect.semantic)):
                async with pipe.stage(key) as st:
                    if st.status == "running":
                        await fn(c, st)
            items = _items(c, inv, scope)
            start = rp.onset(c.signals)
            ctx_s = _context(c, items)

            async with pipe.stage("hypotheses") as st:
                hyps = hy.generate(c.signals, target.service or target.workload or "")
                ranked = _rank(hyps, c.signals, ctx_s, start.at if start else None)
                st.note(f"{len(hyps)} library pattern(s) matched the evidence")
                requested: list[str] = []
                if use_agents:
                    slot = await _slot(s.crewai_slot_wait_seconds)
                    if not slot:
                        agent_state.update(mode="rules", reason=f"{s.crewai_max_concurrent} crews already running on this worker")
                if use_agents and slot:
                    run_a = ag.AgentRun(inv, provider, cfg, history, asyncio.get_running_loop(), s.unified_agent_timeout_seconds)
                    try:
                        raw = await ag.analyst(run_a, target=target.label(), question=question, classification=classification, items=list(items.values()), signals=c.signals,
                                               candidates=ranked, patterns=[p.key for p in hy.PATTERNS], probes={k: p.label for k, p in collect.PROBES.items()}, records=c.records)
                        rejected: list[str] = []
                        for h_raw in [x for x in raw.get("hypotheses") or [] if isinstance(x, dict)][: s.unified_max_hypotheses]:
                            h, bad = hy.from_agent(h_raw, ranked, items, target.service or target.workload or "")
                            rejected += bad
                            if h is not None and h not in ranked:
                                ranked.append(h)
                        requested, refused = ag.probe_requests(raw, set(collect.PROBES), s.unified_max_probes)
                        c.refused += [{"probe": p, "by": "analyst", "reason": "not an allowlisted probe"} for p in refused]
                        agent_state["analyst"] = {"status": "completed", "proposed": len(raw.get("hypotheses") or []), "citations_rejected": sorted(set(rejected)),
                                                  "probes_requested": requested, "probes_refused": refused, "notes": " ".join(str(raw.get("notes") or "").split())[:400],
                                                  "usage": {k: str(v) for k, v in run_a.usage.items()}, "model_written": True}
                        st.model_calls += run_a.usage["model_calls"]
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # a failed agent costs its contribution, never the report
                        agent_state["analyst"] = {"status": "failed", "reason": f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:300]}
                        st.partial = True
                        st.note("the analyst agent failed; hypotheses come from the pattern library only")
                ranked = _rank(ranked, c.signals, ctx_s, start.at if start else None)
                st.data["requested"] = requested

            async with pipe.stage("more_evidence") as st:
                wanted = [(p, "analyst") for p in pipe.stages["hypotheses"].data.get("requested", [])] + collect.wanted_probes(ranked[:3], s.unified_max_probes)
                done: set[str] = set()
                for probe, why in wanted:
                    if probe in done or len(done) >= s.unified_max_probes:
                        continue
                    done.add(probe)
                    await c.probe(st, probe, why if why == "analyst" else f"hypothesis {why}")
                if not done:
                    st.skip("the leading hypotheses lack nothing a probe can collect")
                c.refresh()
                items = _items(c, inv, scope)
                start = rp.onset(c.signals)
                ctx_s = _context(c, items)
                ranked = _rank([hy.rematch(h, c.signals, target.service or target.workload or "") for h in ranked], c.signals, ctx_s, start.at if start else None)

            async with pipe.stage("validate") as st:
                namespaces = {namespace}
                for h in ranked:
                    h.review = rv.review(h, items, c.signals, environment=target.environment, namespaces=namespaces)
                if use_agents and slot and ranked:
                    run_r = ag.AgentRun(inv, provider, cfg, history, asyncio.get_running_loop(), s.unified_agent_timeout_seconds)
                    try:
                        raw = await ag.reviewer(run_r, target=target.label(), items=list(items.values()), signals=c.signals, claims=ranked[: s.unified_max_hypotheses], records=c.records)
                        by_id = {str(r.get("id")): r for r in raw.get("reviews") or [] if isinstance(r, dict)}
                        for h in ranked:
                            h.review = rv.merge_agent(h, h.review, by_id.get(h.id), items)
                        agent_state["reviewer"] = {"status": "completed", "reviewed": len(by_id), "usage": {k: str(v) for k, v in run_r.usage.items()}, "model_written": True}
                        st.model_calls += run_r.usage["model_calls"]
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        agent_state["reviewer"] = {"status": "failed", "reason": f"{type(exc).__name__}: {getattr(exc, 'message', None) or exc}"[:300]}
                        st.partial = True
                        st.note("the reviewer agent failed; the code's review stands alone")
                ranked = _rank(ranked, c.signals, ctx_s, start.at if start else None)
                st.data["verdicts"] = {h.id: h.review.get("verdict") for h in ranked}

            async with pipe.stage("rca") as st:
                kept = [h for h in ranked if h.score["score"] > 0 or h.review.get("verdict") != "unsupported"][: s.unified_max_hypotheses]
                not_shown = [{"id": h.id, "pattern": h.pattern, "title": h.title, "origin": h.origin, "score": h.score["score"], "verdict": h.review.get("verdict"),
                              "why": "ranked below the report's limit" if h.score["score"] > 0 else "nothing in the evidence supports it"} for h in ranked if h not in kept]
                ranked = kept
                st.data["ranking"] = [{"id": h.id, "pattern": h.pattern, "score": h.score["score"], "band": h.score["band"]} for h in ranked]
                st.data["not_shown"] = not_shown

            async with pipe.stage("remediation") as st:
                remediation = rp.remediation_for(ranked, target)
                st.note("each action needs an approved action plan; the pipeline runs nothing")

            if any(x.status in ("partial", "failed", "skipped") and STAGES[i].kind == "collect" for i, x in enumerate(pipe.stages.values()) if x.key != "more_evidence"):
                status = "partial"
            async with pipe.stage("report") as st:
                block, report = rp.build(target=target, incident=c.incident, classification=classification, ranked=ranked, items=items, signals=c.signals,
                                         records=list(c.records.values()), gaps=[g.out() for g in c.gaps], stages=pipe.out(), budget=budget.out(), production=production,
                                         remediation=remediation, agents=_agents_block(agent_state), refused=c.refused, status=status, notes=list(inv.notes),
                                         not_shown=pipe.stages["rca"].data.get("not_shown") or [])
        except asyncio.CancelledError:
            inv.partial_payload = {"unified": partial()}
            history.line("Cancelled", "warning")
            await history.close("cancelled", "cancelled")
            raise
        finally:
            if slot:
                _release()
        if report is None:  # the report stage itself failed: MOKS's rule-based report over what was collected, with the pipeline's state
            from ..orchestrator.report import rules_report

            status = "partial"
            report = rules_report("", inv.findings, inv._views(), [*inv.notes, f"The unified report could not be written ({pipe.stages['report'].reason}); this is the rule-based report."])
            block = {**partial(), "status": status, "executive_summary": report.summary}
        block["pipeline"] = pipe.out()  # the report stage's own end
        root.set_attribute("moks.outcome", status)
        root.set_attribute("moks.unified.tool_calls", budget.tool_used)
        root.set_attribute("moks.unified.hypotheses", len(ranked))
    await history.close("succeeded")
    await audit.record(None, action="investigation.unified", actor_id=inv.owner, resource_type="investigation", resource_id=str(inv.id), environment=target.environment,
                       namespace=namespace, outcome=status, details={"engine_version": ENGINE_VERSION, "stages": {x["key"]: x["status"] for x in block["pipeline"]},
                                                                     "tool_calls": budget.tool_used, "hypotheses": [h.pattern for h in ranked][:5],
                                                                     "agents": agent_state["mode"], "gaps": len(c.gaps)})
    mode = "unified_agents" if agent_state.get("analyst") or agent_state.get("reviewer") else "unified_rules"
    return {"namespace": namespace, "report": report, "mode": mode, "unified": block}
