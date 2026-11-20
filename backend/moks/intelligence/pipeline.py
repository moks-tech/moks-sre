"""The fifteen stages, their state, and the budgets they run under.

Each stage runs inside Pipeline.stage(): a `phase` event and a `pipeline` event (the whole stage list) go to the
investigation's stream as it starts and ends, a step goes to the CrewAI console's run log, and an OpenTelemetry span
named `unified stage <key>` wraps it, carrying names and counts only. A stage that fails, times out or runs out of
budget ends `failed`, `partial` or `skipped` with the reason, and the pipeline goes on: the report is written from
whatever was collected. Cancellation is the one exception: the stage ends `cancelled` and the cancellation propagates.

Budgets: tool calls (never above max_tool_calls), wall time (never above the investigation's remaining time), and
per-stage shares of both, so one slow source cannot starve the rest. Shares scale down when the ceiling is lower.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from ..observability import mark_failed, span

log = logging.getLogger("moks.intelligence")


@dataclass(frozen=True)
class StageDef:
    key: str
    label: str
    kind: str        # collect | reason | decide
    tools: int       # share of the tool-call budget at the default ceiling
    seconds: float   # wall-time limit at the default ceiling
    description: str


STAGES: tuple[StageDef, ...] = (
    StageDef("detect", "Incident detection", "collect", 2, 25, "Firing alerts and the namespace's health findings: what shows an incident"),
    StageDef("classify", "Incident classification", "decide", 0, 5, "Category and severity from the question and the detection signals"),
    StageDef("identify", "Resource and service identification", "collect", 4, 25, "The workload, its pods by owner reference, its Service and its dependencies"),
    StageDef("telemetry", "Metrics and logs collection", "collect", 5, 30, "Golden signals, pod resources, error logs and events for the target"),
    StageDef("traces", "Distributed trace analysis", "collect", 2, 20, "A trace backend when one is connected, else trace ids followed across services through logs"),
    StageDef("changes", "Deployment and Git change analysis", "collect", 3, 25, "Rollouts, configuration and commits in the window"),
    StageDef("anomalies", "ML anomaly correlation", "collect", 1, 15, "Anomaly-model observations for the target and its dependencies, set against the onset"),
    StageDef("history", "Historical incident retrieval (RAG)", "collect", 2, 20, "Approved past incidents and postmortems like this one"),
    StageDef("semantic", "Embedding-based semantic search", "collect", 1, 20, "Runbooks and documents about these symptoms, by meaning as well as words"),
    StageDef("hypotheses", "Hypothesis generation", "reason", 0, 70, "Failure patterns the evidence matches, and what the analyst agent proposes"),
    StageDef("more_evidence", "Additional evidence collection", "collect", 4, 30, "Allowlisted probes for what the leading hypotheses still lack"),
    StageDef("validate", "Independent evidence validation", "reason", 0, 70, "Citations, support and contradictions checked against the ledger; the reviewer agent's challenges"),
    StageDef("rca", "Root-cause analysis", "decide", 0, 5, "Hypotheses ranked by transparent scores"),
    StageDef("remediation", "Recommended remediation", "decide", 0, 5, "Allowlisted actions to consider, each needing an approved action plan"),
    StageDef("report", "Final investigation report", "decide", 0, 10, "Summary, timeline, impact, evidence, hypotheses, verification and limitations"),
)
BY_KEY = {s.key: s for s in STAGES}
DEFAULT_TOOLS = sum(s.tools for s in STAGES)
REPORT_RESERVE_SECONDS = 10.0  # at most this much is kept back for the report


class BudgetSpent(Exception):
    """The run's tool or time budget is used up: the stage ends partial, later collection stages are skipped."""


@dataclass
class Target:
    environment: str
    namespace: str
    workload: str | None = None
    kind: str = "Deployment"
    service: str | None = None
    pods: frozenset[str] = frozenset()
    owners: frozenset[tuple[str, str, str | None]] = frozenset()
    dependencies: frozenset[str] = frozenset()
    repository: str = ""
    minutes: int = 60
    how: str = ""  # how it was identified: requested, from triage findings, named in the question
    also: list[str] = field(default_factory=list)  # other workloads with findings, not investigated in depth

    def regex(self) -> str | None:
        return "(" + "|".join(sorted(p.replace(".", "[.]") for p in self.pods)) + ")" if self.pods else None

    def label(self) -> str:
        return f"{self.kind} {self.workload} in {self.namespace}" if self.workload else f"namespace {self.namespace}"

    def out(self) -> dict[str, Any]:
        return {"environment": self.environment, "namespace": self.namespace, "workload": self.workload, "kind": self.kind, "service": self.service,
                "pods": sorted(self.pods), "dependencies": sorted(self.dependencies), "repository": self.repository or None, "how": self.how, "also_affected": self.also}


@dataclass
class StageState:
    key: str
    label: str
    status: str = "pending"  # pending | running | completed | partial | skipped | failed | cancelled
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str | None = None
    partial: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text[:300])

    def skip(self, reason: str) -> None:
        self.status, self.reason = "skipped", reason[:300]

    def out(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "status": self.status, "started_at": self.started_at, "finished_at": self.finished_at, "duration_ms": self.duration_ms,
                "tool_calls": self.tool_calls, "model_calls": self.model_calls, "evidence": list(self.evidence), "notes": list(self.notes), "reason": self.reason}


class Budget:
    def __init__(self, tool_calls: int, seconds: float):
        self.tool_limit = max(0, tool_calls)
        self.tool_used = 0
        self.started = time.monotonic()
        self.seconds = max(1.0, seconds)
        self.reserve = min(REPORT_RESERVE_SECONDS, self.seconds * 0.1)  # always left for the reasoning and report stages
        scale = min(1.0, self.tool_limit / DEFAULT_TOOLS) if DEFAULT_TOOLS else 1.0
        self.shares = {s.key: (max(1, int(round(s.tools * scale))) if s.tools else 0) for s in STAGES}
        self.used: dict[str, int] = {}

    def left_seconds(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    def stage_seconds(self, key: str) -> float:
        return max(0.0, min(BY_KEY[key].seconds, self.left_seconds() - (0 if key == "report" else self.reserve)))

    def can_call(self, key: str, extra_share: int = 0) -> str | None:
        """Why a call may not run now, or None."""
        if self.tool_used >= self.tool_limit:
            return f"the pipeline's budget of {self.tool_limit} tool calls is used up"
        if self.used.get(key, 0) >= self.shares.get(key, 0) + extra_share:
            return f"this stage's share of {self.shares.get(key, 0) + extra_share} tool call(s) is used up"
        if self.left_seconds() <= self.reserve:
            return "the pipeline's time budget is used up"
        return None

    def spend(self, key: str) -> None:
        self.tool_used += 1
        self.used[key] = self.used.get(key, 0) + 1

    def out(self) -> dict[str, Any]:
        return {"tool_calls": {"limit": self.tool_limit, "used": self.tool_used, "shares": self.shares},
                "seconds": {"limit": round(self.seconds, 1), "used": round(time.monotonic() - self.started, 1)}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Pipeline:
    def __init__(self, inv: Any, history: Any, budget: Budget, version: str):
        self.inv, self.history, self.budget, self.version = inv, history, budget, version
        self.stages: dict[str, StageState] = {s.key: StageState(s.key, s.label) for s in STAGES}

    def out(self) -> list[dict[str, Any]]:
        return [s.out() for s in self.stages.values()]

    async def _publish(self) -> None:
        await self.inv._emit("pipeline", {"engine": "unified", "version": self.version, "stages": [{k: v for k, v in s.out().items() if k != "notes"} for s in self.stages.values()],
                                          "budget": self.budget.out()})

    @contextlib.asynccontextmanager
    async def stage(self, key: str) -> AsyncIterator[StageState]:
        st, d = self.stages[key], BY_KEY[key]
        n = list(BY_KEY).index(key) + 1
        st.status, st.started_at = "running", _now()
        began = time.monotonic()
        seconds = self.budget.stage_seconds(key)
        await self.inv._emit("phase", {"phase": key, "label": f"Stage {n}/{len(STAGES)}: {d.label}"})
        await self.history.start(key, f"{n}. {d.label}")
        await self._publish()
        with span(f"unified stage {key}", **{"moks.unified.stage": key, "moks.unified.kind": d.kind}) as s:
            try:
                if seconds <= 0 and d.kind == "collect":
                    st.skip("the pipeline's time budget is used up")
                    yield st
                else:
                    async with asyncio.timeout(max(seconds, 1.0)):
                        yield st
            except asyncio.CancelledError:
                st.status, st.reason = "cancelled", "the investigation was cancelled"
                raise
            except TimeoutError:
                st.status, st.reason = "partial", f"stopped at this stage's time limit of {seconds:.0f}s"
            except BudgetSpent as exc:
                st.status, st.reason = "partial", str(exc)
            except Exception as exc:
                log.warning("stage failed", extra={"event": "unified_stage_failed", "operation": key, "investigation_id": str(self.inv.id), "errorType": type(exc).__name__},
                            exc_info=True)
                st.status, st.reason = "failed", f"{type(exc).__name__}: {getattr(exc, 'message', None) or str(exc)}"[:300]
            finally:
                if st.status == "running":
                    st.status = "partial" if st.partial else "completed"
                st.finished_at, st.duration_ms = _now(), int((time.monotonic() - began) * 1000)
                s.set_attribute("moks.outcome", st.status)
                s.set_attribute("moks.unified.tool_calls", st.tool_calls)
                if st.status in ("failed", "partial"):
                    mark_failed(s, "unified_stage", st.status)
                with contextlib.suppress(Exception):
                    if st.status != "cancelled":
                        self.history.line(f"{d.label}: {st.status}" + (f" ({st.reason})" if st.reason else "") + (f"; evidence {', '.join(st.evidence)}" if st.evidence else ""),
                                          "info" if st.status in ("completed", "skipped") else "warning", {"event": "stage", "stage": key, "status": st.status}, step=key)
                        await self.history.finish(key, "succeeded" if st.status in ("completed", "skipped", "partial") else "failed", st.reason)
                        await self._publish()
