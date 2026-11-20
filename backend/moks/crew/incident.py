"""The incident flow (docs/crewai-sre-engine.md): MOKS runs the workflow, CrewAI runs each step's agent.

    plan -> kubernetes -> observability -> change -> knowledge -> hypotheses -> review [-> MOKS collects what it asked for -> review] -> rca

Each step is one CrewAI task in a crew of its own, kicked off by MOKS. That is what makes the workflow's guarantees
possible: a step has its own time limit and retry, its answer is checked against its contract (a CrewAI guardrail)
and graded in code before the next step starts, the review loop is bounded by MOKS rather than by a conversation,
and a checkpoint after every step lets a resumed attempt skip what is done. CrewAI's Flow was not used: its state
persistence writes to a local SQLite file, which would put evidence on the pod's disk (the same reason the crew's
task-output store is switched off in adapters.py).

Agents never see each other's words as instructions. A later step receives MOKS's case file (crew/validation.py Case)
inside an untrusted-data block, built from findings and hypotheses MOKS has already checked.

Everything a step calls goes through the workload flow's machinery (crew/runner.py Run): the Bridge, the ToolExecutor,
the owner-reference target, the evidence registry, llm.budget and the console history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any

from opentelemetry import trace as trace_api

from ..config import get_settings
from ..observability import mark_failed, span
from . import CrewUnavailable
from . import incident_config as ic
from . import validation as val
from .history import History
from .runner import CrewStopped, Run, _release, _slot, _target_text
from .schema import CrewError, EvidenceRequest, IncidentResult, RawRequest, Severity, TaskRun
from .tools import BY_NAME, Target

log = logging.getLogger("moks.crewai.incident")

CHECKPOINT_VERSION = 1
TASK_RETRIES = 1  # a step that fails for a reason other than a limit runs once more
RETRYABLE = {"timeout", "unavailable", "bad_response"}  # LLMError kinds worth one more attempt; budget refusals are not
COLLECTION_SHARE = 0.75  # planning and the specialists may spend this share of the token budget; the rest is kept for hypotheses, review and RCA
ANSWER_CHARS = 20_000
GRACE_SECONDS = 5  # past a step's time limit, how long MOKS waits for a call in flight before it ends the step itself

EMPTY = {"plan": {"severity": None, "affected": [], "symptom_start": None, "objectives": []}, "hypotheses": {"hypotheses": []},
         "review": {"verdicts": [], "conflicts": [], "missing_telemetry": [], "evidence_requests": []},
         "rca": {"summary": "", "root_cause": None, "contributing": [], "timeline": [], "impact": "", "next_steps": []}}


class CrewFallback(CrewUnavailable):
    """The crew cannot start here. Investigator._investigate runs the LangGraph investigation instead."""


def synthetic(task: str, why: str) -> str:
    """The answer MOKS gives in an agent's place when a step reaches a limit: valid for the step's contract, empty of
    claims, with the reason as a gap. CrewAI ends the task on it and the workflow goes on."""
    body = EMPTY.get(task) or {"findings": [], "gaps": [f"MOKS ended this step: {why}"]}
    return f"Thought: MOKS ended this step: {why}.\nFinal Answer: {json.dumps(body)}"


class IncidentRun(Run):
    """A Run whose steps are tasks: per-step limits, per-agent tool caps, a token budget with a reserve, retries,
    evidence requests run by MOKS, usage per agent, and checkpoints."""

    def __init__(self, *args: Any, target_text: str = "", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.target_text = target_text
        self.epoch = 0  # bumped when a step ends: calls from a step that is over raise instead of running
        self.task_name: str | None = None
        self.step: str | None = None
        self.task_started = 0.0
        self.task_calls = 0
        self.ended_by: str | None = None
        self.agent_tools: dict[str, int] = {}
        self.capped: set[str] = set()
        self.usage: dict[str, dict[str, Any]] = {}
        self.tokens = 0
        self.budget_hit = False
        self.errors: list[CrewError] = []
        self.task_runs: list[TaskRun] = []
        self.answers: dict[str, str] = {}
        self.done: list[str] = []

    def _step_for(self, agent: str) -> str:
        return self.step or agent

    # ---- limits ---------------------------------------------------------------------------------

    def _limit(self) -> str | None:
        lim = self.limits
        if time.monotonic() - self.task_started > lim["task_timeout_seconds"]:
            return f"the step's time limit of {lim['task_timeout_seconds']}s was reached"
        if self.task_calls > lim["max_iterations"] + 2:
            return f"the step's limit of {lim['max_iterations']} iterations was reached"
        cap = lim["token_budget"] if self.task_name in ("hypotheses", "review", "rca") else int(lim["token_budget"] * COLLECTION_SHARE)
        if self.tokens >= cap:
            self.budget_hit = True
            return f"the crew's token budget was reached ({self.tokens} of {lim['token_budget']} tokens" + ("" if cap == lim["token_budget"] else ", the rest kept for review") + ")"
        return None

    # ---- called from the crew's thread ------------------------------------------------------------

    def task_complete(self, epoch: int, agent: str, system: str, turns: list[dict[str, str]]) -> str:
        if epoch != self.epoch:
            raise CrewStopped("this step has ended")
        if self.bridge.closed.is_set():
            raise CrewStopped(self.bridge.reason)
        self.task_calls += 1
        self.llm_calls[agent] = self.llm_calls.get(agent, 0) + 1
        why = self._limit()
        if why:
            self.ended_by = self.ended_by or why
            self.bridge.post(self._note(f"{self.labels[agent]}: {why}; MOKS ended the step with what it has", self.step, "warning"))
            return synthetic(self.task_name or "", why)
        wanted = self.limits["report_tokens"] if self.task_name in ic.STRUCTURED else self.limits["step_tokens"]
        return self.bridge.run(self._complete_retrying(agent, system, turns, wanted), self.limits["task_timeout_seconds"] + GRACE_SECONDS)

    def task_tool(self, epoch: int, agent: str, name: str, raw: dict[str, Any]) -> str:
        if epoch != self.epoch:
            raise CrewStopped("this step has ended")
        if time.monotonic() - self.task_started > self.limits["task_timeout_seconds"]:
            self.ended_by = self.ended_by or f"the step's time limit of {self.limits['task_timeout_seconds']}s was reached"
            return "Not run: this step's time limit was reached. Give your Final Answer now with the evidence already collected."
        cap = self.limits["max_tool_calls_per_agent"]
        if self.agent_tools.get(agent, 0) >= cap:
            if agent not in self.capped:
                self.capped.add(agent)
                self.registry.gap("limits", f"the {self.labels[agent].lower()} reached its limit of {cap} tool calls")
            return f"Not run: your limit of {cap} tool calls is reached. Give your Final Answer now with the evidence already collected."
        return self.bridge.run(self._agent_call(BY_NAME[name], raw, agent), self.s.crewai_tool_timeout_seconds + 15)

    def registry_list(self, epoch: int) -> str:
        if epoch != self.epoch:
            raise CrewStopped("this step has ended")
        gaps = "\nGaps:\n" + "\n".join(f"- {g['source']}: {g['reason']}" for g in self.registry.gaps) if self.registry.gaps else ""
        return "Evidence you may cite:\n" + self.registry.listing() + gaps

    def registry_detail(self, epoch: int, ref: str) -> str:
        if epoch != self.epoch:
            raise CrewStopped("this step has ended")
        entry = self.registry.records.get(str(ref).strip())
        if entry is None:
            return f"There is no evidence {ref}. Cite only ids from evidence_list."
        return self._observation(entry, again=True)

    # ---- on the loop ---------------------------------------------------------------------------------

    async def _complete_retrying(self, agent: str, system: str, turns: list[dict[str, str]], wanted: int) -> str:
        from ..llm.providers import LLMError

        before = (self.inv.tokens_in, self.inv.tokens_out, self.inv.cost)
        attempt = 0
        while True:
            try:
                text = await self._complete(agent, system, turns, wanted)
                break
            except LLMError as exc:
                if exc.kind not in RETRYABLE or attempt >= self.limits["model_retries"]:
                    raise
                attempt += 1
                self.history.line(f"Model call failed ({exc.kind}); retry {attempt} of {self.limits['model_retries']}", "warning", {"agent": agent}, step=self.step)
                await asyncio.sleep(min(8.0, 2.0 ** (attempt - 1)))
        u = self.usage.setdefault(agent, {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": Decimal(0), "tool_calls": 0, "retries": 0})
        tin, tout = self.inv.tokens_in - before[0], self.inv.tokens_out - before[1]
        u["model_calls"] += 1
        u["retries"] += attempt
        u["tokens_in"] += tin
        u["tokens_out"] += tout
        u["cost_usd"] += self.inv.cost - before[2]
        self.tokens += tin + tout
        return text

    async def _agent_call(self, tool: Any, raw: dict[str, Any], agent: str) -> str:
        before = self.tool_calls
        out = await self._call_tool(tool, raw, agent)
        ran = self.tool_calls - before
        if ran:
            self.agent_tools[agent] = self.agent_tools.get(agent, 0) + ran
            self.usage.setdefault(agent, {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": Decimal(0), "tool_calls": 0, "retries": 0})["tool_calls"] += ran
        return out

    async def collect(self, wanted: list[RawRequest], round_: int, case: val.Case) -> int:
        """The reviewer's evidence requests, run by MOKS with the owning specialist's binding and checks. Returns how many
        new evidence records they produced."""
        tools_of = {k: set(self.crew["agents"][k].get("tools") or []) for k in ic.SPECIALISTS}
        new = 0
        for r in wanted:
            owner = next((k for k in ic.SPECIALISTS if r.tool in tools_of[k]), None)
            if owner is None:
                case.requests.append(EvidenceRequest(round=round_, tool=r.tool, args=r.args, reason=r.reason, for_=r.for_, status="refused",
                                                     why="no specialist in this crew's configuration has that tool"))
                continue
            refs, refused = set(self.registry.records), len(self.refused)
            text = await self._call_tool(BY_NAME[r.tool], r.args, owner)
            added = [x for x in self.registry.records if x not in refs]
            if added:
                new += 1
                status, ref, why = "collected", added[0], ""
            elif len(self.refused) > refused:
                status, ref, why = "refused", None, self.refused[-1]["reason"]
            elif text.startswith("Already collected as"):
                status, ref, why = "already_collected", text.split()[3].rstrip("."), ""
            else:
                status, ref, why = "not_run", None, text.split("\n", 1)[0][:300]
            case.requests.append(EvidenceRequest(round=round_, tool=r.tool, args=r.args, reason=r.reason, for_=r.for_, status=status, evidence=ref, why=why))
            self.history.line(f"Review request {r.tool} {json.dumps(r.args, sort_keys=True)[:200]}: {status}" + (f" ({ref})" if ref else f" ({why})" if why else ""),
                              "info" if status == "collected" else "warning", {"event": "evidence_request", "tool": r.tool, "status": status, "evidence": ref}, step=self.step)
        return new

    # ---- one step ---------------------------------------------------------------------------------

    async def run_task(self, task: str, adapters: Any, case: val.Case, round_: int = 1) -> tuple[TaskRun, str]:
        agent = ic.TASK_AGENT[task]
        self.task_name, self.step = task, task if round_ == 1 else f"{task}_{round_}"
        label = f"{self.labels[agent]}: {ic.TASK_LABELS[task]}" + (f" (round {round_})" if round_ > 1 else "")
        await self.history.start(self.step, label)
        await self.inv._emit("phase", {"phase": f"crewai_{task}", "label": f"CrewAI: {label}", "crewai": {"task": task, "agent": agent, "round": round_, "status": "running"}})
        tools = self.crew["agents"][agent].get("tools") or []
        self.history.line(f"{label} started" + (f" with {len(tools)} tool(s): {', '.join(tools)}" if tools else ""), data={"agent": agent, "task": task})
        tr = TaskRun(task=task, agent=agent, label=label, round=round_, status="done")
        started, calls0, tools0 = time.monotonic(), sum(self.llm_calls.values()), self.tool_calls
        tok0 = (self.inv.tokens_in, self.inv.tokens_out)
        seen: dict[str, str] = {}
        text = ""
        self.task_started = started
        while True:
            tr.attempts += 1
            self.epoch += 1
            self.task_calls, self.ended_by = 0, None
            crew = adapters.build_task_crew(self, task, case.text(), self.epoch, seen)
            self.task_span = trace_api.get_tracer("moks").start_span(f"crewai task {task}", context=self.root_context,
                                                                     attributes={"moks.crewai.agent": agent, "moks.crewai.task": task, "moks.crewai.round": round_})
            self.task_context = trace_api.set_span_in_context(self.task_span, self.root_context)
            outcome = "done"
            try:
                left = max(1.0, self.limits["task_timeout_seconds"] + GRACE_SECONDS - (time.monotonic() - started))
                async with asyncio.timeout(left):
                    result = await crew.akickoff()
                text = str(getattr(result, "raw", "") or "")
                if self.ended_by:
                    tr.status, tr.ended_by, outcome = "partial", self.ended_by, "partial"
                    code = "budget_exhausted" if "token budget" in self.ended_by else "iteration_limit" if "iterations" in self.ended_by else "task_timeout"
                    self.errors.append(CrewError(code=code, task=task, message=self.ended_by))
                break
            except TimeoutError:
                self.epoch += 1
                self.bridge.cancel_inflight()
                tr.status, tr.ended_by, outcome = "partial", f"the step's time limit of {self.limits['task_timeout_seconds']}s was reached", "timeout"
                self.errors.append(CrewError(code="task_timeout", task=task, message=tr.ended_by))
                text = seen.get(task, "")
                break
            except (asyncio.CancelledError, CrewStopped):
                outcome = "stopped"
                raise
            except Exception as exc:
                from ..llm.budget import BudgetDenied
                from ..llm.providers import LLMError
                from ..orchestrator.engine import BudgetExceeded

                why = (getattr(exc, "message", None) or str(exc) or type(exc).__name__)[:300]
                if isinstance(exc, (BudgetDenied, BudgetExceeded)):
                    self.errors.append(CrewError(code="budget_exhausted", task=task, message=why))
                    raise CrewStopped(f"MOKS's LLM budget stopped the crew: {why}") from None
                if "guardrail" in str(exc).lower():
                    tr.status, tr.ended_by, outcome = "invalid_output", why, "invalid_output"
                    self.errors.append(CrewError(code="invalid_output", task=task, message=f"the answer never matched the step's contract: {why}"))
                    text = seen.get(task, "")
                    break
                if tr.attempts <= TASK_RETRIES:
                    self.history.line(f"{label} failed ({why}); running the step once more", "warning", step=self.step)
                    continue
                tr.status, tr.ended_by, outcome = "failed", why, "failed"
                self.errors.append(CrewError(code="provider_error" if isinstance(exc, LLMError) else "task_failed", task=task, message=why))
                text = seen.get(task, "")
                break
            finally:
                self.task_span.set_attribute("moks.outcome", outcome)
                if outcome not in ("done", "partial"):
                    mark_failed(self.task_span, f"task_{outcome}", outcome)
                self.task_span.end()
                self.task_span = None
        self.epoch += 1  # anything the finished step's thread still tries now raises
        tr.seconds = round(time.monotonic() - started, 2)
        tr.model_calls = sum(self.llm_calls.values()) - calls0
        tr.tool_calls = self.tool_calls - tools0
        tr.tokens_in, tr.tokens_out = self.inv.tokens_in - tok0[0], self.inv.tokens_out - tok0[1]
        self.task_runs.append(tr)
        self.answers[self.step] = text[:ANSWER_CHARS]
        await self.bridge.flush()
        await self.history.finish(self.step, "failed" if tr.status == "failed" else "succeeded", tr.ended_by if tr.status != "done" else None)
        await self.inv._emit("note", {"message": f"CrewAI: {label}: {tr.status.replace('_', ' ')}" + (f" ({tr.ended_by})" if tr.ended_by else ""),
                                      "crewai": {"task": task, "agent": agent, "round": round_, "status": tr.status, "seconds": tr.seconds,
                                                 "model_calls": tr.model_calls, "tool_calls": tr.tool_calls}})
        return tr, text

    # ---- checkpoints -------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Decisions and progress for the investigation's checkpoint: the steps done and their answers, which evidence id
        each agent collected, and the counters. Never evidence payloads: those are re-read from the evidence table."""
        return {"version": CHECKPOINT_VERSION, "flow": ic.FLOW, "done": list(self.done), "answers": dict(self.answers),
                "agents": {ref: e["agent"] for ref, e in self.registry.records.items()}, "seen": dict(self.seen), "tool_calls": self.tool_calls,
                "agent_tools": dict(self.agent_tools), "tokens": self.tokens, "usage": json.loads(json.dumps(self.usage, default=str)),
                "tasks": [t.model_dump() for t in self.task_runs], "errors": [e.model_dump() for e in self.errors]}

    async def checkpoint(self, done: str, following: str) -> None:
        self.done.append(done)
        await self.inv._checkpoint(f"crewai:{done}", f"crewai:{following}", {"namespace": self.target.namespace, "crewai": self.snapshot()})

    async def restore(self, cp: dict[str, Any] | None) -> list[str]:
        """A resumed attempt: the registry rebuilt from the evidence table (aged evidence is historical by now), the counters,
        and the finished steps, whose answers are absorbed again in order. Returns the steps done."""
        if not cp or cp.get("version") != CHECKPOINT_VERSION or cp.get("flow") != ic.FLOW:
            return []
        records = {e["ref"]: e for e in self.inv.evidence}
        self.members = await self.resolve_members()
        self.members_at = time.monotonic()
        for ref, agent in (cp.get("agents") or {}).items():
            if ref in records and ref not in self.registry.records:
                self._register(records[ref], agent, self.members)
        self.seen.update({k: v for k, v in (cp.get("seen") or {}).items() if v in self.registry.records})
        self.tool_calls = max(self.tool_calls, int(cp.get("tool_calls") or 0))
        self.agent_tools.update(cp.get("agent_tools") or {})
        self.tokens = int(cp.get("tokens") or 0)
        for agent, u in (cp.get("usage") or {}).items():
            self.usage[agent] = {**u, "cost_usd": Decimal(str(u.get("cost_usd") or 0))}
        self.task_runs = [TaskRun.model_validate({**t, "status": "restored"}) for t in cp.get("tasks") or []]
        self.errors = [CrewError.model_validate(e) for e in cp.get("errors") or []]
        self.answers = dict(cp.get("answers") or {})
        self.done = list(cp.get("done") or [])
        self.history.line(f"Resumed after {self.done[-1] if self.done else 'setup'}: {len(self.done)} step(s) restored, {len(self.registry.records)} evidence record(s)", "warning")
        return self.done


# ---- the workflow ----------------------------------------------------------------------------------

def _absorb(r: IncidentRun, case: val.Case, task: str, text: str, tr: TaskRun | None, round_: int = 1) -> list[RawRequest]:
    """A step's answer into the case, then everything graded again. Returns the review's evidence requests."""
    wanted: list[RawRequest] = []
    parsed, why = val.parse(task, text)
    if parsed is None:
        case.gaps.append(f"{ic.TASK_LABELS[task]}: no usable answer ({why})")
        if tr is not None and tr.status == "done":
            tr.status = "invalid_output"
            r.errors.append(CrewError(code="invalid_output", task=task, message=why or "no answer"))
    elif task == "plan":
        val.accept_plan(case, parsed, r.registry, r.views)
    elif task in ic.SPECIALISTS:
        val.accept_findings(case, task, ic.TASK_AGENT[task], parsed, r.registry, r.views)
    elif task == "hypotheses":
        val.accept_hypotheses(case, parsed, r.registry)
    elif task == "review":
        wanted = val.apply_review(case, parsed, round_, r.limits["followup_calls"])
    elif task == "rca":
        case.rca = parsed
    _detector_findings(r, case)
    _refresh(r, case)
    return wanted


def _detector_findings(r: IncidentRun, case: val.Case) -> None:
    """MOKS's own detectors (k8s_triage's automatic findings about the target) join the case as findings of their own,
    so a crash loop or an OOM kill is on record even when an agent missed it."""
    known = {(f.task, f.observation) for f in case.findings}
    for d in r.inv.findings:
        refs = [x for x in d.get("evidence") or [] if x in r.registry.records]
        title = str(d.get("title") or "").strip()
        if not refs or not title or ("detectors", title) in known or d.get("namespace") != r.target.namespace:
            continue
        resource = next(iter(d.get("resources") or []), f"{r.target.kind}/{r.target.workload}")
        val.accept_findings(case, "detectors", "moks", val.RawFindings(findings=[{"observation": title, "resource": resource, "evidence": refs}]), r.registry, r.views)
        known.add(("detectors", title))


def _refresh(r: IncidentRun, case: val.Case) -> None:
    case.onset = val.onset(r.registry, r.views, r.target.namespace, case.window)
    case.changes = val.changes(r.registry, r.views, case.window[1], case.change_window, case.onset, r.target.namespace)
    case.severity = val.severity(r.registry, r.views, r.target, bool(r.inv.environment_info.get("is_production")), case.plan.severity if case.plan else None)
    val.grade(case, r.registry, r.views)


async def workflow(r: IncidentRun, case: val.Case, adapters: Any, done: list[str]) -> None:
    """Steps 1-10 of the doc. Every step is checkpointed; a step already done (a resumed attempt) is absorbed from its answer."""
    order = ["plan", *ic.SPECIALISTS, "hypotheses"]
    for i, task in enumerate(order):
        if task in done:
            _absorb(r, case, task, r.answers.get(task, ""), None)
            continue
        tr, text = await r.run_task(task, adapters, case)
        _absorb(r, case, task, text, tr)
        await r.checkpoint(task, order[i + 1] if i + 1 < len(order) else "review")
    rounds = r.limits["review_rounds"]
    for n in range(1, rounds + 1):
        key = "review" if n == 1 else f"review_{n}"
        if key in done:
            wanted = _absorb(r, case, "review", r.answers.get(key, ""), None, n)
            continue
        tr, text = await r.run_task("review", adapters, case, n)
        wanted = _absorb(r, case, "review", text, tr, n)
        if not wanted or n == rounds:
            await r.checkpoint(key, "rca")
            break
        r.step = f"evidence_{n}"
        await r.history.start(r.step, f"MOKS collects the evidence the review asked for (round {n})")
        await r.inv._emit("phase", {"phase": "crewai_evidence", "label": f"CrewAI: collecting {len(wanted)} piece(s) of evidence for the review",
                                    "crewai": {"task": "evidence", "round": n, "status": "running", "requests": len(wanted)}})
        added = await r.collect(wanted, n, case)
        await r.history.finish(r.step, "succeeded")
        _detector_findings(r, case)
        _refresh(r, case)
        await r.checkpoint(key, f"review_{n + 1}")
        if not added:
            r.history.line("The review's requests produced no new evidence; no further review round", "warning")
            break
    if "rca" not in done:
        tr, text = await r.run_task("rca", adapters, case)
        _absorb(r, case, "rca", text, tr)
        await r.checkpoint("rca", "validate")
    else:
        _absorb(r, case, "rca", r.answers.get("rca", ""), None)


async def run(inv: Any, executor: Any, ctx: Any, question: str, namespace: str, scope_info: dict, provider: Any, cfg: Any, request: dict[str, Any],
              resumed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Graph-shaped state ({"namespace", "report", "mode", "crewai"}) for Investigator._investigate. Raises CrewFallback when the
    crew cannot start and the LangGraph investigation should run instead, CrewUnavailable when nothing should run."""
    from . import capability, console, prepare_environment

    s = get_settings()
    testing = request.get("trigger") == "test" or isinstance(request.get("draft"), dict)
    soft = CrewFallback if s.crewai_fallback == "langgraph" and not testing else CrewUnavailable
    history = await History(inv.id).open()
    await history.start("setup", "Set up run")
    try:
        state = capability(s, cfg)
        if not state["available"]:
            raise soft(f"CrewAI investigation unavailable: {state['reason']}")
        try:
            crew, used = await console.run_config(request)
        except CrewUnavailable as exc:
            raise soft(str(exc)) from None
        flow = await console.flow_state(ic.FLOW)
        if not flow["enabled"]:
            raise soft(f"CrewAI investigation unavailable: the {ic.FLOW_SPEC['label'].lower()} flow is switched off in the CrewAI console")
        if namespace == "*" or not ctx.scope.allows(namespace):
            raise CrewUnavailable(f"CrewAI investigation unavailable: namespace {namespace} is outside this person's scope")
    except CrewUnavailable as exc:
        history.line(str(exc) + (": MOKS runs its standard investigation instead" if isinstance(exc, CrewFallback) else ""), "error")
        await history.close("failed", str(exc))
        raise
    if ctx.focus_namespace not in (None, namespace):
        inv.notes.append(f"The resumed attempt had focused on {ctx.focus_namespace}; the CrewAI investigation stays on {namespace}.")
    ctx.focus_namespace = inv.focus_namespace = namespace
    lookback = request.get("change_window") or crew["defaults"].get("change_window") or "24h"
    target = Target(environment=ctx.environment.name, namespace=namespace, workload=request["workload"], kind=request.get("kind") or "Deployment",
                    minutes=max(1, int((ctx.time_to - ctx.time_from).total_seconds() // 60)), repository=inv._repository(ctx),
                    change_hours=max(1, -(-ic.CHANGE_WINDOWS[lookback] // 60)))
    limits = crew["limits"]
    target_text = _target_text(target, scope_info) + f"; changes are looked for in the last {lookback}"
    history.line(f"CrewAI {state['version']} · model {state['llm']} · {ic.FLOW_SPEC['label']} · configuration: {used}", data={"crewai": state["version"], "llm": state["llm"], "config": used})
    history.line(f"Target: {target_text}")
    history.line("Limits: " + ", ".join(f"{k} {v}" for k, v in limits.items()), data={"limits": limits})
    await inv._emit("phase", {"phase": "crewai", "label": "CrewAI: waiting for a crew slot"})
    if not await _slot(s.crewai_slot_wait_seconds):
        why = f"CrewAI investigation unavailable: {s.crewai_max_concurrent} crews are already running on this worker"
        history.line(why, "error")
        await history.close("failed", why)
        raise soft(why)
    r: IncidentRun | None = None
    case = val.Case(window=(ctx.time_from, ctx.time_to), change_window=lookback)
    outcome, stopped = "completed", None
    try:
        prepare_environment()
        from . import adapters  # imports crewai, after telemetry is off

        with span("crewai investigation", **{"moks.crewai.version": state["version"], "moks.crewai.flow": ic.FLOW, "moks.crewai.workload": target.workload,
                                              "moks.namespace": target.namespace}) as root:
            r = IncidentRun(inv, executor, ctx, provider, cfg, target, question, asyncio.get_running_loop(), crew=crew, history=history, target_text=target_text)
            done = await r.restore((resumed or {}).get("crewai"))
            await history.finish("setup", "succeeded")
            try:
                async with asyncio.timeout(limits["timeout_seconds"]):
                    await workflow(r, case, adapters, done)
            except TimeoutError:
                outcome, stopped = "partial", f"the crew's time limit of {limits['timeout_seconds']}s was reached"
                r.errors.append(CrewError(code="run_timeout", task=r.task_name, message=stopped))
            except asyncio.CancelledError:
                r.bridge.close("cancelled")
                r.end_spans("cancelled")
                await r.bridge.flush()
                history.line("Cancelled", "warning")
                await history.close("cancelled", "cancelled")
                raise
            except CrewStopped as exc:
                outcome, stopped = "partial", str(exc)
            except Exception as exc:  # a CrewAI error ends the crew, not the report
                outcome, stopped = "partial", f"the crew stopped: {getattr(exc, 'message', None) or type(exc).__name__}"
                r.errors.append(CrewError(code="task_failed", task=r.task_name, message=stopped[:300]))
                log.warning("crew stopped", extra={"event": "crewai_stopped", "investigation_id": str(inv.id), "errorType": type(exc).__name__})
            finally:
                r.bridge.close(stopped or "finished")
                r.end_spans(outcome)
                await r.bridge.flush()
            root.set_attribute("moks.outcome", outcome)
            root.set_attribute("moks.crewai.tool_calls", r.tool_calls)
            root.set_attribute("moks.crewai.tokens", r.tokens)
            if outcome != "completed":
                mark_failed(root, "crew_partial", "partial")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        history.line(f"The crew could not run: {getattr(exc, 'message', None) or type(exc).__name__}", "error")
        await history.close("failed", type(exc).__name__)
        raise
    finally:
        _release()
    if stopped:
        history.line(f"Crew stopped early: {stopped}", "error")
        await history.close("failed", stopped)
        inv.notes.append(f"CrewAI incident investigation incomplete: {stopped}. The report covers the steps that ran.")
        r.registry.gap("crew", stopped)
    await history.start("validate", "Validate the report")
    _detector_findings(r, case)
    _refresh(r, case)
    report, decided, cause = val.to_report(case, r.registry, inv.notes)
    mode, fallback = "crewai", None
    if not val.usable(case):  # the crew produced nothing MOKS could accept: LangGraph's report writer over the evidence it collected
        history.line("No finding or hypothesis survived validation; LangGraph's report writer runs over the evidence the crew collected", "warning")
        inv._correlate()
        report, _ = await inv._write_report(provider, cfg, question, scope_info)
        mode, fallback = "crewai_fallback", "langgraph_report"
        decided["fallback"] = fallback
        r.errors.append(CrewError(code="fallback", message="no finding or hypothesis survived validation; the report is LangGraph's"))
    result = IncidentResult(
        severity=case.severity or Severity(level="unknown", criteria=["nothing was graded"]), affected=case.affected,
        objectives=case.plan.objectives if case.plan else [], symptom_onset=val.iso(case.onset), findings=case.findings,
        hypotheses=sorted(case.hypotheses, key=lambda h: h.rank), root_cause=cause,
        contributing=[h for h in (case.rca.contributing if case.rca else []) if any(x.id == h and x.status != "insufficient_evidence" for x in case.hypotheses)],
        changes=case.changes, change_window=lookback, timeline=val.timeline(case, r.registry, r.views),
        review={"rounds": sum(1 for t in r.task_runs if t.task == "review"), "verdicts": case.verdicts, "conflicts": case.conflicts, "missing_telemetry": case.missing},
        requests=case.requests, unsupported=case.unsupported, impact=case.rca.impact if case.rca else "", tasks=r.task_runs, errors=r.errors)
    _log(history, result, decided)
    await history.close("succeeded")
    cited = {ref for f in result.findings for ref in f.citations} | {ref for h in result.hypotheses for ref in h.supporting + h.contradicting}
    cited |= {ref for c in result.changes for ref in c.evidence} | {ref for e in result.timeline for ref in e.evidence}
    cited |= {ref for f in report.observed_facts for ref in f.evidence} | {ref for c in report.suspected_causes for ref in c.evidence}
    usage = {"by_agent": {k: {**v, "cost_usd": str(v["cost_usd"])} for k, v in r.usage.items()},
             "total": {"model_calls": sum(v["model_calls"] for v in r.usage.values()), "tool_calls": r.tool_calls, "tokens": r.tokens,
                       "tokens_in": sum(v["tokens_in"] for v in r.usage.values()), "tokens_out": sum(v["tokens_out"] for v in r.usage.values()),
                       "cost_usd": str(sum((v["cost_usd"] for v in r.usage.values()), Decimal(0)))},
             "token_budget": limits["token_budget"], "budget_reached": r.budget_hit}
    block = {"version": state["version"], "flow": ic.FLOW, "status": outcome, "stopped": stopped,
             "agents": [{"agent": t.task if t.round == 1 else f"{t.task}_{t.round}", "label": t.label, "status": "done" if t.status in ("done", "restored") else t.status}
                        for t in r.task_runs],
             "tool_calls": r.tool_calls, "limits": {**state["limits"], **limits, "max_tool_calls": r.tool_limit}, "config": used, "refused": r.refused,
             "gaps": r.registry.gaps, "validation": decided, "severity": result.severity.level, "fallback": fallback, "usage": usage,
             "errors": [e.model_dump() for e in r.errors], "investigation": result.model_dump(mode="json", by_alias=True),
             # Excerpts come from the registry, cut from the stored evidence. Model text never lands here.
             "evidence": [r.registry.records[x] for x in sorted(cited, key=lambda x: int(x[1:])) if x in r.registry.records],
             "registry": [{k: v for k, v in e.items() if k != "excerpt"} for e in r.registry.records.values()]}
    return {"namespace": namespace, "report": report, "mode": mode, "crewai": block}


def _log(history: History, result: IncidentResult, decided: dict[str, Any]) -> None:
    by_status: dict[str, int] = {}
    for f in result.findings:
        by_status[f.status] = by_status.get(f.status, 0) + 1
    history.line(f"Severity {result.severity.level}: {'; '.join(result.severity.criteria[:3])}", data={"event": "severity", **result.severity.model_dump()})
    history.line(f"Findings: {len(result.findings)} ({', '.join(f'{v} {k.replace(chr(95), chr(32))}' for k, v in by_status.items()) or 'none'}); "
                 f"hypotheses: {len(result.hypotheses)}; changes: {len(result.changes)}", data={"event": "graded"})
    for h in result.hypotheses:
        history.line(f"{h.id} {h.status.replace('_', ' ')} ({h.confidence:.2f}): {h.statement[:200]}", data={"event": "hypothesis", "id": h.id, "status": h.status,
                                                                                                          "confidence": h.confidence, "criteria": [c.name for c in h.criteria if c.met]})
    rejected, removed = decided.get("citations_rejected") or [], decided.get("claims_removed") or []
    history.line(f"Checked by MOKS: {len(rejected)} invented citation(s) rejected, {len(removed)} unsupported claim(s) set aside"
                 + (f"; root cause {result.root_cause['hypothesis']} ({result.root_cause['label']})" if result.root_cause else "; no root cause established"),
                 "warning" if rejected or removed else "info", {"event": "validation", "rejected": rejected, "removed": removed})
