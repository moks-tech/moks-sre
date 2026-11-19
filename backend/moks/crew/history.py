"""What the CrewAI console shows of a run (docs/crewai-console.md): its steps, its log and its model-call transcripts.

Written by the CrewAI worker while the crew runs, always on the event loop (the crew's thread hands every model and
tool call back to it). Every write is best effort in its own short transaction: a failed write is logged once and
never fails, slows or changes the investigation. Runs queued before the console existed have no crew_runs row, and
then nothing is written.

Log lines are buffered and written at most every half second, at each step change and at the end, so a live viewer
sees them within a second. Each run keeps at most MAX_LINES lines; each transcript field is cut at MAX_FIELD
characters. What is written is MOKS's own text and the redacted evidence excerpts the model saw; nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text

from ..db import session_scope

log = logging.getLogger("moks.crewai.history")

MAX_LINES = 5000
MAX_FIELD = 200_000
FLUSH_SECONDS = 0.5
HISTORY_DEFAULTS = {"transcripts": True, "log_days": 90, "transcript_days": 14}


async def settings(db: Any) -> dict[str, Any]:
    from ..models import CrewSetting

    row = await db.get(CrewSetting, "history")
    return {**HISTORY_DEFAULTS, **((row.value if row else None) or {})}


def _cut(value: str) -> str:
    return value if len(value) <= MAX_FIELD else value[:MAX_FIELD] + f"\n… cut at {MAX_FIELD} characters"


class History:
    def __init__(self, investigation_id: uuid.UUID):
        self.id = investigation_id
        self.enabled = False
        self.transcripts = False
        self.lines = 0
        self.buffer: list[dict[str, Any]] = []
        self.step: str = "setup"
        self.positions: dict[str, int] = {}
        self.usage: dict[str, dict[str, Any]] = {}
        self.flushed_at = time.monotonic()
        self.warned = False
        self._flusher: asyncio.Task | None = None

    async def open(self) -> "History":
        from ..models import CrewRun

        try:
            async with session_scope() as db:
                self.enabled = (await db.get(CrewRun, self.id)) is not None
                self.transcripts = self.enabled and bool((await settings(db))["transcripts"])
        except Exception:
            self._failed("open")
        return self

    def _failed(self, what: str) -> None:
        if not self.warned:
            self.warned = True
            log.warning("CrewAI run history not written", extra={"event": "crewai_history_failed", "operation": what, "investigation_id": str(self.id)}, exc_info=True)

    # ---- steps -------------------------------------------------------------------------

    async def start(self, step: str, label: str) -> None:
        if not self.enabled:
            return
        await self.flush()
        self.step = step
        self.positions.setdefault(step, len(self.positions))
        self.usage.setdefault(step, {"model_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": Decimal("0")})
        try:
            async with session_scope() as db:
                await db.execute(text("""INSERT INTO crew_run_steps (investigation_id, step, position, label, status) VALUES (:i, :s, :p, :l, 'running')
                                         ON CONFLICT (investigation_id, step) DO UPDATE SET status = 'running', label = EXCLUDED.label, finished_at = NULL, error = NULL"""),
                                 {"i": self.id, "s": step, "p": self.positions[step], "l": label[:120]})
        except Exception:
            self._failed("step")

    async def finish(self, step: str, status: str, error: str | None = None) -> None:
        if not self.enabled or step not in self.positions:
            return
        await self.flush()
        u = self.usage.get(step) or {}
        try:
            async with session_scope() as db:
                await db.execute(text("""UPDATE crew_run_steps SET status = :st, finished_at = now(), error = :e, model_calls = :mc, tool_calls = :tc,
                                         tokens_in = :ti, tokens_out = :to, cost_usd = :c WHERE investigation_id = :i AND step = :s AND status = 'running'"""),
                                 {"i": self.id, "s": step, "st": status, "e": (error or None) and error[:2000], "mc": u.get("model_calls", 0), "tc": u.get("tool_calls", 0),
                                  "ti": u.get("tokens_in", 0), "to": u.get("tokens_out", 0), "c": u.get("cost_usd", Decimal("0"))})
        except Exception:
            self._failed("step")

    async def close(self, status: str, error: str | None = None) -> None:
        """At the end of the crew: every step still running ends with the run's outcome."""
        if not self.enabled:
            return
        for step in list(self.positions):
            await self.finish(step, status, error)
        await self.flush()

    def count(self, step: str, *, model: bool = False, tool: bool = False, tokens_in: int = 0, tokens_out: int = 0, cost: Decimal | float = 0) -> None:
        u = self.usage.setdefault(step, {"model_calls": 0, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": Decimal("0")})
        u["model_calls"] += int(model)
        u["tool_calls"] += int(tool)
        u["tokens_in"] += tokens_in
        u["tokens_out"] += tokens_out
        u["cost_usd"] += Decimal(str(cost))

    # ---- log lines --------------------------------------------------------------------

    def line(self, message: str, level: str = "info", data: dict[str, Any] | None = None, step: str | None = None, call_id: int | None = None) -> None:
        if not self.enabled:
            return
        if self.lines >= MAX_LINES:
            return
        self.lines += 1
        if self.lines == MAX_LINES:
            message, level, data = f"Log limit of {MAX_LINES} lines reached; later lines are not kept.", "warning", None
        self.buffer.append({"i": self.id, "s": step or self.step, "at": datetime.now(timezone.utc), "l": level, "m": message[:4000], "d": data, "c": call_id})
        if time.monotonic() - self.flushed_at >= FLUSH_SECONDS:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flusher is None or self._flusher.done():
            try:
                self._flusher = asyncio.get_running_loop().create_task(self.flush())
            except RuntimeError:  # no loop in this thread; the next step change or the end writes them
                pass

    async def flush(self) -> None:
        if not self.buffer:
            return
        rows, self.buffer = self.buffer, []
        self.flushed_at = time.monotonic()
        try:
            import json

            async with session_scope() as db:
                await db.execute(text("INSERT INTO crew_run_logs (investigation_id, step, at, level, message, data, call_id) "
                                      "VALUES (:i, :s, :at, :l, :m, CAST(:d AS jsonb), :c)"),
                                 [{**r, "d": json.dumps(r["d"], default=str) if r["d"] is not None else None} for r in rows])
        except Exception:
            self._failed("log")

    # ---- transcripts ------------------------------------------------------------------

    async def call(self, step: str, *, provider: str, model: str, system: str, turns: list[dict[str, str]], response: str, tokens_in: int, tokens_out: int,
                   cost: Decimal | float, duration_ms: int, outcome: str = "ok", error: str | None = None) -> int | None:
        if not (self.enabled and self.transcripts):
            return None
        from ..models import CrewRunCall

        try:
            async with session_scope() as db:
                row = CrewRunCall(investigation_id=self.id, step=step, provider=provider[:20], model=model[:200], system=_cut(system),
                                  prompt=[{"role": t.get("role"), "content": _cut(str(t.get("content") or ""))} for t in turns], response=_cut(response or ""),
                                  tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=Decimal(str(cost)), duration_ms=duration_ms, outcome=outcome[:20],
                                  error=(error or None) and error[:2000])
                db.add(row)
                await db.flush()
                return row.id
        except Exception:
            self._failed("transcript")
            return None


async def purge(now: datetime | None = None) -> dict[str, int]:
    """Retention: log lines and transcripts older than the console's settings. Runs and steps stay."""
    async with session_scope() as db:
        s = await settings(db)
        logs = (await db.execute(text("DELETE FROM crew_run_logs WHERE at < now() - make_interval(days => :d)"), {"d": int(s["log_days"])})).rowcount
        calls = (await db.execute(text("DELETE FROM crew_run_calls WHERE created_at < now() - make_interval(days => :d)"), {"d": int(s["transcript_days"])})).rowcount
    return {"logs": logs or 0, "calls": calls or 0}


async def has_run(db: Any, investigation_id: uuid.UUID) -> bool:
    from ..models import CrewRun

    return (await db.execute(select(CrewRun.investigation_id).where(CrewRun.investigation_id == investigation_id))).first() is not None
