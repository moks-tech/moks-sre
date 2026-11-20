"""Typed results of the incident flow (docs/crewai-sre-engine.md, section 4).

Two kinds of model here. The `Raw*` models are what an agent may answer: lenient, extra fields ignored, every list
bounded, so a long or malformed answer is cut rather than trusted. Everything else is what MOKS stores: an agent
supplies the words of an observation or a hypothesis and the ids it cites; MOKS sets the ids, sources, resources it
could verify, times, confidence, criteria and status.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["confirmed_observation", "strongly_supported", "possible_explanation", "insufficient_evidence"]
STATUS_LABEL = {"confirmed_observation": "confirmed observation", "strongly_supported": "strongly supported hypothesis",
                "possible_explanation": "possible explanation", "insufficient_evidence": "insufficient evidence"}
Verdict = Literal["supported", "unsupported", "contradicted", "insufficient"]
WINDOWS = ("15m", "1h", "6h", "24h")


def _clip(limit: int):
    def cut(v: Any) -> str:
        return " ".join(str(v or "").split())[:limit]
    return cut


def _ids(v: Any, limit: int = 12) -> list[str]:
    items = v if isinstance(v, list) else [v] if v else []
    return [str(i).strip()[:12] for i in items if str(i).strip()][:limit]


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---- what agents answer ---------------------------------------------------------------------

class RawAffected(_Raw):
    resource: str = ""
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    _r = field_validator("resource", mode="before")(_clip(253))
    _w = field_validator("reason", mode="before")(_clip(300))
    _e = field_validator("evidence", mode="before")(_ids)


class RawPlan(_Raw):
    severity: str | None = None
    affected: list[RawAffected] = Field(default_factory=list, max_length=10)
    symptom_start: str | None = None
    objectives: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("affected", "objectives", mode="before")
    @classmethod
    def _bounded(cls, v: Any) -> list:
        return list(v)[:10] if isinstance(v, list) else []

    @field_validator("objectives", mode="after")
    @classmethod
    def _texts(cls, v: list[str]) -> list[str]:
        return [_clip(300)(x) for x in v if str(x).strip()]


class RawFinding(_Raw):
    observation: str = ""
    resource: str = ""
    evidence: list[str] = Field(default_factory=list)
    at: str | None = None
    _o = field_validator("observation", mode="before")(_clip(600))
    _r = field_validator("resource", mode="before")(_clip(253))
    _e = field_validator("evidence", mode="before")(_ids)


class RawFindings(_Raw):
    findings: list[RawFinding] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    @field_validator("findings", mode="before")
    @classmethod
    def _f(cls, v: Any) -> list:
        return [x for x in v if isinstance(x, dict)][:15] if isinstance(v, list) else []

    @field_validator("gaps", mode="before")
    @classmethod
    def _g(cls, v: Any) -> list:
        return [_clip(300)(x) for x in v if str(x).strip()][:10] if isinstance(v, list) else []


class RawHypothesis(_Raw):
    statement: str = ""
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    would_confirm: str = ""
    _s = field_validator("statement", mode="before")(_clip(600))
    _w = field_validator("would_confirm", mode="before")(_clip(400))
    _a = field_validator("supporting", "contradicting", mode="before")(_ids)


class RawHypotheses(_Raw):
    hypotheses: list[RawHypothesis] = Field(default_factory=list)

    @field_validator("hypotheses", mode="before")
    @classmethod
    def _h(cls, v: Any) -> list:
        return [x for x in v if isinstance(x, dict)][:5] if isinstance(v, list) else []


class RawVerdict(_Raw):
    id: str = ""
    verdict: str = ""
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    _i = field_validator("id", mode="before")(_clip(12))
    _v = field_validator("verdict", mode="before")(_clip(20))
    _r = field_validator("reason", mode="before")(_clip(400))
    _e = field_validator("evidence", mode="before")(_ids)


class RawConflict(_Raw):
    ids: list[str] = Field(default_factory=list)
    description: str = ""
    _i = field_validator("ids", mode="before")(_ids)
    _d = field_validator("description", mode="before")(_clip(400))


class RawRequest(_Raw):
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    for_: str = Field(default="", alias="for")
    _t = field_validator("tool", mode="before")(_clip(40))
    _r = field_validator("reason", mode="before")(_clip(300))
    _f = field_validator("for_", mode="before")(_clip(12))

    @field_validator("args", mode="before")
    @classmethod
    def _a(cls, v: Any) -> dict:
        return v if isinstance(v, dict) and len(v) <= 8 else {}


class RawReview(_Raw):
    verdicts: list[RawVerdict] = Field(default_factory=list)
    conflicts: list[RawConflict] = Field(default_factory=list)
    missing_telemetry: list[str] = Field(default_factory=list)
    evidence_requests: list[RawRequest] = Field(default_factory=list)

    @field_validator("verdicts", "conflicts", "evidence_requests", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list:
        return [x for x in v if isinstance(x, dict)][:30] if isinstance(v, list) else []

    @field_validator("missing_telemetry", mode="before")
    @classmethod
    def _m(cls, v: Any) -> list:
        return [_clip(300)(x) for x in v if str(x).strip()][:10] if isinstance(v, list) else []


class RawTimeline(_Raw):
    at: str = ""
    event: str = ""
    evidence: list[str] = Field(default_factory=list)
    _a = field_validator("at", mode="before")(_clip(40))
    _e2 = field_validator("event", mode="before")(_clip(300))
    _e = field_validator("evidence", mode="before")(_ids)


class RawRCA(_Raw):
    summary: str = ""
    root_cause: str | None = None
    contributing: list[str] = Field(default_factory=list)
    timeline: list[RawTimeline] = Field(default_factory=list)
    impact: str = ""
    next_steps: list[str] = Field(default_factory=list)
    _s = field_validator("summary", mode="before")(_clip(2000))
    _i = field_validator("impact", mode="before")(_clip(600))
    _c = field_validator("contributing", mode="before")(_ids)

    @field_validator("timeline", mode="before")
    @classmethod
    def _t(cls, v: Any) -> list:
        return [x for x in v if isinstance(x, dict)][:20] if isinstance(v, list) else []

    @field_validator("next_steps", mode="before")
    @classmethod
    def _n(cls, v: Any) -> list:
        return [_clip(400)(x) for x in v if str(x).strip()][:8] if isinstance(v, list) else []

    @field_validator("root_cause", mode="before")
    @classmethod
    def _r(cls, v: Any) -> str | None:
        return str(v).strip()[:12] or None if v not in (None, "", "null") else None


RAW = {"plan": RawPlan, "kubernetes": RawFindings, "observability": RawFindings, "change": RawFindings, "knowledge": RawFindings,
       "hypotheses": RawHypotheses, "review": RawReview, "rca": RawRCA}


# ---- what MOKS stores --------------------------------------------------------------------------

class Criterion(BaseModel):
    """One named test of the evidence and whether it passed. Confidence is the sum of the weights that passed."""
    name: str
    met: bool
    weight: float
    detail: str = ""


class TimeRange(BaseModel):
    start: str | None = Field(default=None, alias="from", serialization_alias="from")
    end: str | None = Field(default=None, alias="to", serialization_alias="to")
    point_in_time: bool = False
    model_config = ConfigDict(populate_by_name=True)


class Finding(BaseModel):
    id: str                           # F1, F2, … in the order MOKS accepted them
    observation: str                  # the agent's words
    task: str
    agent: str
    resource: str                     # what the agent said it is about
    resource_verified: bool           # whether that name occurs in the evidence it cites
    evidence_source: list[str]        # the MOKS tools behind the citations
    citations: list[str]              # evidence ids, all in the registry
    timestamp: str | None             # when the newest cited evidence was collected
    event_time: str | None = None     # the time the agent gave, kept only when it occurs in the cited evidence
    time_range: TimeRange
    confidence: float
    criteria: list[Criterion]
    status: Status
    hypotheses: list[str] = Field(default_factory=list)
    review: str | None = None         # the reviewer's verdict, when it gave one


class Hypothesis(BaseModel):
    id: str
    statement: str
    supporting: list[str]             # evidence ids (cited findings resolved to theirs)
    supporting_findings: list[str]
    contradicting: list[str]
    would_confirm: str = ""
    confidence: float
    criteria: list[Criterion]
    status: Status                    # never confirmed_observation: a hypothesis is not a fact
    review: dict[str, Any] | None = None
    rank: int = 0


class Change(BaseModel):
    id: str
    kind: Literal["rollout", "commit", "gitops_sync", "config"]
    resource: str
    at: str
    minutes_before: int               # before the reference time (the end of the investigation window)
    window: Literal["15m", "1h", "6h", "24h", "older"]
    detail: str
    evidence: list[str]
    before_symptoms: bool | None = None


class TimelineEvent(BaseModel):
    at: str
    event: str
    evidence: list[str]
    source: Literal["evidence", "rca"]


class Severity(BaseModel):
    level: Literal["SEV1", "SEV2", "SEV3", "SEV4", "unknown"]
    criteria: list[str]
    proposed: str | None = None       # the commander's, recorded beside MOKS's


class EvidenceRequest(BaseModel):
    round: int
    tool: str
    args: dict[str, Any]
    reason: str
    for_: str = Field(default="", serialization_alias="for")
    status: Literal["collected", "already_collected", "refused", "not_run"]
    evidence: str | None = None
    why: str = ""


class TaskRun(BaseModel):
    task: str
    agent: str
    label: str
    round: int = 1
    status: Literal["done", "partial", "failed", "skipped", "invalid_output", "restored"]
    ended_by: str | None = None
    attempts: int = 0
    seconds: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class CrewError(BaseModel):
    code: Literal["crew_unavailable", "task_timeout", "iteration_limit", "task_failed", "invalid_output", "run_timeout", "budget_exhausted", "tool_limit",
                  "provider_error", "cancelled", "fallback"]
    task: str | None = None
    message: str


class IncidentResult(BaseModel):
    version: int = 1
    severity: Severity
    affected: list[dict[str, Any]] = Field(default_factory=list)
    objectives: list[str] = Field(default_factory=list)
    symptom_onset: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    root_cause: dict[str, Any] | None = None
    contributing: list[str] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    change_window: str = "24h"
    timeline: list[TimelineEvent] = Field(default_factory=list)
    review: dict[str, Any] = Field(default_factory=dict)
    requests: list[EvidenceRequest] = Field(default_factory=list)
    unsupported: list[dict[str, str]] = Field(default_factory=list)
    impact: str = ""
    tasks: list[TaskRun] = Field(default_factory=list)
    errors: list[CrewError] = Field(default_factory=list)
