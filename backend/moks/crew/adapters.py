"""CrewAI classes over MOKS. Imported only by runner.run(), after prepare_environment().

MoksCrewLLM is CrewAI's documented custom-LLM extension point (BaseLLM). It sends every call through
llm.budget to the provider MOKS already has configured, so reservations, daily limits, metering and the
per-investigation token budget apply, and CrewAI never builds a client of its own (no LiteLLM, no native
SDK, no API key from the environment). MOKS providers do not expose vendor function calling, so the
LLM reports supports_function_calling() False and CrewAI uses its text protocol (Thought / Action /
Action Input / Observation), whose tool calls arrive here as plain arguments.
"""
from __future__ import annotations

import secrets
from typing import Any

from crewai import Agent, BaseLLM, Crew, Process, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, PrivateAttr

from ..orchestrator import prompts
from .config import OUTPUT_CONTRACT, fill, rules as _rules
from .runner import AGENTS, Run
from .tools import TOOLS

STOP = "\nObservation:"
ROLE = {"user": "user", "assistant": "assistant", "tool": "user"}


def _turns(messages: Any) -> tuple[str, list[dict[str, str]]]:
    """CrewAI's messages as MOKS's (system, alternating turns starting with the person)."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    turns: list[dict[str, str]] = []
    for m in messages:
        role = ROLE.get(str(m.get("role")))
        if role is None:
            continue
        content = str(m.get("content") or "")
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n\n" + content
        else:
            turns.append({"role": role, "content": content})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "Begin."})
    return system, turns


class MoksCrewLLM(BaseLLM):
    _run: Any = PrivateAttr()
    _agent: str = PrivateAttr()

    def __init__(self, run: Run, agent: str):
        super().__init__(model=f"moks:{run.cfg.provider}/{run.cfg.model}", temperature=0.1, provider="moks")
        self._run, self._agent = run, agent

    def call(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None, from_agent=None, response_model=None) -> str:
        system, turns = _turns(messages)
        text = self._run.complete(self._agent, system, turns)
        return text.split(STOP, 1)[0]  # MOKS providers take no stop sequences, so they are applied here

    async def acall(self, messages, tools=None, callbacks=None, available_functions=None, from_task=None, from_agent=None, response_model=None) -> str:
        raise NotImplementedError("MOKS runs CrewAI's synchronous path in its worker thread; see crew/runner.py")

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 32_000


class MoksTool(BaseTool):
    _run_ref: Any = PrivateAttr()

    def _run(self, **kwargs: Any) -> str:
        return self._run_ref.call_tool(self.name, kwargs)


class NoArgs(BaseModel):
    pass


class EvidenceList(BaseTool):
    """The reviewer's only tool. It reads MOKS's registry, not a live system, and records nothing."""
    name: str = "evidence_list"
    description: str = "The ids of all evidence collected so far, with source, arguments and whether it succeeded."
    args_schema: type[BaseModel] = NoArgs
    _run_ref: Any = PrivateAttr()

    def _run(self, **kwargs: Any) -> str:
        return self._run_ref.evidence_list()


def _tool(run: Run, spec) -> MoksTool:
    t = MoksTool(name=spec.name, description=spec.description, args_schema=spec.args, cache_function=lambda *_: False)
    t._run_ref = run
    return t


class _NoTaskStore:
    """CrewAI keeps every task's output in a SQLite file under the user's data directory, for `crewai replay`.
    That would put evidence on the pod's disk, outside MOKS's database, so it is replaced with nothing."""

    def __getattr__(self, name: str):
        return lambda *a, **k: [] if name == "load" else None


def build_crew(run: Run, target_text: str, question: str) -> Crew:
    """The crew from the run's configuration (moks/crew/config.py). Prompts, the agents' tool subsets and the limits
    come from it; the structure, the untrusted-data rule and the reviewer's output contract stay here."""
    cfg = run.crew
    tag = secrets.token_hex(3)
    llms = {k: MoksCrewLLM(run, k) for k in AGENTS}
    common = {"allow_delegation": False, "max_iter": run.limits["max_iterations"], "max_retry_limit": 0, "verbose": False, "cache": False,
              "memory": False, "reasoning": False, "respect_context_window": True}
    asked = prompts.untrusted("the person's question", question, 1000)

    def text(value: str) -> str:
        return fill(value, target_text, asked)

    agents: dict[str, Agent] = {}
    for key in ("workload", "change"):
        a = cfg["agents"][key]
        allowed = set(a.get("tools") or [])
        agents[key] = Agent(role=a["role"], llm=llms[key], tools=[_tool(run, t) for t in TOOLS if t.agent == key and t.name in allowed], **common,
                            goal=text(a["goal"]), backstory=text(a["backstory"]) + " " + _rules(tag))
    listing = EvidenceList()
    listing._run_ref = run
    r = cfg["agents"]["review"]
    agents["review"] = Agent(role=r["role"], llm=llms["review"], tools=[listing], **common, goal=text(r["goal"]), backstory=text(r["backstory"]) + " " + _rules(tag))

    t = cfg["tasks"]
    t1 = Task(agent=agents["workload"], description=text(t["workload"]["description"]), expected_output=text(t["workload"]["expected_output"]))
    t2 = Task(agent=agents["change"], context=[t1], description=text(t["change"]["description"]), expected_output=text(t["change"]["expected_output"]))
    t3 = Task(agent=agents["review"], context=[t1, t2], description=text(t["review"]["description"]) + "\n" + OUTPUT_CONTRACT,
              expected_output=text(t["review"]["expected_output"]))
    by_role = {cfg["agents"][k]["role"]: k for k in AGENTS}

    def task_done(output) -> None:
        agent = by_role.get(getattr(output, "agent", None))
        if agent:
            run.finished(agent)

    crew = Crew(agents=[agents["workload"], agents["change"], agents["review"]], tasks=[t1, t2, t3], process=Process.sequential, verbose=False,
                memory=False, planning=False, cache=False, share_crew=False, tracing=False, task_callback=task_done)
    crew._task_output_handler = _NoTaskStore()
    return crew
