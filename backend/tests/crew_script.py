"""A scripted model for the CrewAI incident flow: it plays each of the seven agents from a scenario's `script`
(moks/evals/datasets/crewai_incidents.yaml) through llm.budget, so the whole workflow runs with no provider.

It decides from the conversation it is given, like a model would: which agent it is (the role in the system prompt),
which step (the output contract in the task), and what MOKS has returned so far. Citations are written as tool names
(`cite: [k8s_pod_logs]`) and resolved to the evidence ids MOKS actually gave for those tools, so a script never
hard-codes ids. It measures MOKS's pipeline (binding, evidence, validation, grading, report), not a model's reasoning.
"""
from __future__ import annotations

import json
import re
from typing import Any

from moks.llm.providers import LLMResponse

ROLES = {"Incident commander": "commander", "Kubernetes investigator": "kubernetes", "Observability investigator": "observability",
         "Change analyst": "change", "Historical knowledge analyst": "knowledge", "Evidence reviewer": "reviewer", "Root cause analyst": "rca"}
SEEN = re.compile(r"Evidence (E\d+) \(([a-z0-9_]+),")
LISTED = re.compile(r"^(E\d+): ([a-z0-9_]+) ", re.M)
FILED = re.compile(r"\b(F\d+) \[")


def act(tool: str, args: dict | None = None) -> str:
    return f"Thought: I need {tool}.\nAction: {tool}\nAction Input: {json.dumps(args or {})}"


def final(body: Any) -> str:
    return "Thought: I now know the final answer\nFinal Answer: " + (body if isinstance(body, str) else json.dumps(body))


def task_of(text: str, agent: str) -> str:
    if '"severity": "SEV1|SEV2|SEV3|SEV4"' in text:
        return "plan"
    if '"hypotheses": [{"statement"' in text:
        return "hypotheses"
    if '"verdicts": [{"id"' in text:
        return "review"
    if '"root_cause": "H1 or null"' in text:
        return "rca"
    return agent


class ScriptedSRE:
    """`script` maps a step to {"tools": [[name, args], ...], "answer": {...}} (or a list of answers for retries).
    Replies may also be overridden per step with raw strings or exceptions (`raw`)."""

    def __init__(self, script: dict[str, Any]):
        self.script = script
        self.calls: list[tuple[str, str]] = []
        self.count: dict[str, int] = {}

    def ids(self, transcript: str, tools: list[str]) -> list[str]:
        found: dict[str, str] = {}
        for ref, tool in [*SEEN.findall(transcript), *LISTED.findall(transcript)]:
            found.setdefault(ref, tool)
        return [ref for ref, tool in found.items() if tool in tools]

    def cite(self, value: Any, transcript: str) -> Any:
        """Replaces {"cite": [tool, ...]} with the evidence ids seen for those tools; finding ids pass through."""
        if isinstance(value, dict):
            out = {k: self.cite(v, transcript) for k, v in value.items() if k != "cite"}
            if "cite" in value:
                key = "supporting" if "statement" in value and "observation" not in value else "evidence"
                refs = self.ids(transcript, [c for c in value["cite"] if not re.match(r"^[FHE]\d+$", c)])
                out[key] = [*refs, *[c for c in value["cite"] if re.match(r"^[FHE]\d+$", c)]]
            return out
        if isinstance(value, list):
            return [self.cite(v, transcript) for v in value]
        return value

    async def __call__(self, provider, system, messages, **kw):
        agent = next((k for role, k in ROLES.items() if role in system), "?")
        transcript = "\n".join(str(m["content"]) for m in messages)
        task = task_of(transcript, agent)
        step = "review_2" if task == "review" and "; reviewer: " in transcript else task  # round 2 sees round 1's verdicts in the case file
        self.calls.append((step, transcript))
        plan = self.script.get(step) or self.script.get(task) or {}
        n = self.count[step] = self.count.get(step, 0) + 1
        raw = plan.get("raw")
        if isinstance(raw, list) and n <= len(raw):
            reply = raw[n - 1]
            if isinstance(reply, Exception):
                raise reply
            if not isinstance(reply, str):
                reply = await reply()
            return LLMResponse(reply, 40, 20, "fake", "m")
        n -= len(raw) if isinstance(raw, list) else 0
        tools = plan.get("tools") or []
        if n <= len(tools):
            item = tools[n - 1]
            name, args = (item[0], item[1] if len(item) > 1 else {}) if isinstance(item, list) else (item, {})
            return LLMResponse(act(name, args), 40, 20, "fake", "m")
        answer = plan.get("answer", {"findings": [], "gaps": []} if task == agent else {})
        return LLMResponse(final(self.cite(answer, transcript) if not isinstance(answer, str) else answer), 120, 60, "fake", "m")
