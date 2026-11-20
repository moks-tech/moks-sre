"""Shared LLM budget: every call reserves its worst-case cost first, inside one lock per provider.

Admission counts settled spend plus open reservations, so concurrent calls cannot all pass a check
and overrun the budget. After the call the reservation becomes the real cost.

Rules that apply to every budget here:
- The day is the UTC calendar day and the month the UTC calendar month, the same periods Anthropic's spend limits use.
- A budget of 0 allows no paid use. Free models (zero price) still run, since they cost nothing.
- No limit is expressed as None. Every LLM has a daily budget; its monthly budget and its provider's daily and monthly
  totals (llm_provider_budgets, e.g. the credit on one Anthropic account) are optional. A call must fit all of them.

When the outcome is unknown (timeout, dropped connection, or a worker that died), the provider may
have charged for it, so the reservation stays counted at its full estimate for that day. Only a
definite refusal gives the capacity back. Each attempt of a retried call has its own reservation
and its own result, so retries are never counted twice.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select, text, update

from ..config import get_settings
from ..db import session_scope
from ..models import LLMConfig, LLMProviderBudget, LLMReservation, LLMUsage
from ..observability import EMBED_LATENCY, EMBED_REQUESTS, span as traced
from .providers import BudgetDenied, LLMError, Message, Provider, complete_metered

log = logging.getLogger("moks.budget")
CHARS_PER_TOKEN = 3            # deliberately pessimistic for English and logs
HOLD_MARGIN_SECONDS = 60
DEFINITE = {"rate_limited", "auth", "config", "bad_request", "invalid_request", "budget"}  # refused, so not charged
WAIT_STEP = 0.2
WHERE = "an administrator can change it in Settings → LLM providers"

__all__ = ["BudgetDenied", "complete", "embed", "estimate", "overview", "reserve"]


def estimate(cfg: LLMConfig, chars: int, max_tokens: int) -> Decimal:
    tokens_in = Decimal(chars) / CHARS_PER_TOKEN + 1
    return ((tokens_in * Decimal(cfg.input_price_per_mtok) + Decimal(max_tokens) * Decimal(cfg.output_price_per_mtok)) / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def _start(unit: str = "day"):
    return func.date_trunc(unit, func.now(), "UTC")


def _today():
    return _start("day")


def resets_at(unit: str, now: datetime | None = None) -> datetime:
    """When the current UTC day or month ends."""
    now = now or datetime.now(timezone.utc)
    if unit == "day":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _committed(db, *where, unit: str = "day") -> Decimal:
    """One statement, so one snapshot: a call settling between two reads would be counted nowhere."""
    since = _start(unit)
    used = select(func.coalesce(func.sum(LLMUsage.cost_usd), 0)).where(LLMUsage.created_at >= since, *[w[0] for w in where]).scalar_subquery()
    held = select(func.coalesce(func.sum(LLMReservation.estimated_usd), 0)).where(
        LLMReservation.created_at >= since, LLMReservation.status.in_(("held", "uncertain")), *[w[1] for w in where]).scalar_subquery()
    return Decimal((await db.execute(select(used + held))).scalar_one())


def _of_llm(cfg: LLMConfig) -> tuple:
    return (LLMUsage.llm_config_id == cfg.id, LLMReservation.llm_config_id == cfg.id)


def _of_provider(provider: str) -> tuple:
    """Usage keeps its provider after its LLM is deleted; an open hold always has its LLM."""
    return (LLMUsage.provider == provider, LLMReservation.llm_config_id.in_(select(LLMConfig.id).where(LLMConfig.provider == provider)))


def _over(spent: Decimal, est: Decimal, limit: Decimal) -> bool:
    return spent + est > limit or (spent > 0 and spent >= limit)  # the second: a free call once the money is gone


def _denied(reason: str, label: str, unit: str, spent: Decimal, est: Decimal, limit: Decimal) -> BudgetDenied:
    period = "today" if unit == "day" else "this month"
    reset = resets_at(unit).strftime("%Y-%m-%d %H:%M UTC")
    more = f", and this call could cost up to ${est:.4f}" if spent < limit else ""
    return BudgetDenied(reason, f"{label} is reached: ${spent:.2f} of ${limit:.2f} used {period}{more}. It resets at {reset}; {WHERE}.")


async def _expire() -> None:
    """A hold that outlived its call belongs to a worker that died: the money may be spent. Its own
    transaction, so a refusal below does not undo it."""
    async with session_scope() as db:
        await db.execute(update(LLMReservation).where(LLMReservation.status == "held", LLMReservation.expires_at < func.now()).values(status="uncertain", settled_at=func.now()))


async def _try_reserve(cfg: LLMConfig, est: Decimal, user_id, investigation_id, purpose: str, seconds: int) -> uuid.UUID:
    s = get_settings()
    await _expire()
    async with session_scope() as db:
        # Always in this order (LLM, provider, person), so two calls never wait on each other's locks.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"llm:{cfg.id}"})
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"llm-provider:{cfg.provider}"})
        if user_id:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"llm-user:{user_id}"})
        inflight = (await db.execute(select(func.count()).select_from(LLMReservation).where(LLMReservation.llm_config_id == cfg.id, LLMReservation.status == "held"))).scalar_one()
        if inflight >= cfg.max_concurrency:
            raise BudgetDenied("concurrency", f"{cfg.name} is at its limit of {cfg.max_concurrency} concurrent calls")
        spent, limit = await _committed(db, _of_llm(cfg)), Decimal(cfg.daily_budget_usd)
        if _over(spent, est, limit):
            raise _denied("provider", f"The daily LLM budget of {cfg.name}", "day", spent, est, limit)
        if cfg.monthly_budget_usd is not None:
            spent, limit = await _committed(db, _of_llm(cfg), unit="month"), Decimal(cfg.monthly_budget_usd)
            if _over(spent, est, limit):
                raise _denied("monthly", f"The monthly LLM budget of {cfg.name}", "month", spent, est, limit)
        total = await db.get(LLMProviderBudget, cfg.provider)
        for unit, limit in (("day", total.daily_budget_usd), ("month", total.monthly_budget_usd)) if total else ():
            if limit is None:
                continue
            spent = await _committed(db, _of_provider(cfg.provider), unit=unit)
            if _over(spent, est, Decimal(limit)):
                raise _denied(f"total_{'daily' if unit == 'day' else 'monthly'}",
                              f"The {'daily' if unit == 'day' else 'monthly'} total for all {cfg.provider} LLMs", unit, spent, est, Decimal(limit))
        if user_id and s.llm_user_daily_budget_usd is not None and est > 0:
            mine = await _committed(db, (LLMUsage.user_id == user_id, LLMReservation.user_id == user_id))
            if mine + est > Decimal(str(s.llm_user_daily_budget_usd)):
                raise BudgetDenied("user", f"your daily LLM budget of ${s.llm_user_daily_budget_usd} is used")
        if investigation_id and s.llm_investigation_budget_usd is not None and est > 0:
            this = await _committed(db, (LLMUsage.investigation_id == investigation_id, LLMReservation.investigation_id == investigation_id))
            if this + est > Decimal(str(s.llm_investigation_budget_usd)):
                raise BudgetDenied("investigation", f"this investigation's LLM budget of ${s.llm_investigation_budget_usd} is used")
        row = LLMReservation(llm_config_id=cfg.id, user_id=user_id, investigation_id=investigation_id, purpose=purpose, estimated_usd=est,
                             expires_at=datetime.now(timezone.utc) + timedelta(seconds=seconds))
        db.add(row)
        await db.flush()
        return row.id


async def reserve(cfg: LLMConfig, est: Decimal, *, user_id=None, investigation_id=None, purpose: str = "other", wait_seconds: float = 0) -> uuid.UUID:
    """Waits only for a free concurrency slot. A spent budget does not come back by waiting."""
    seconds = cfg.timeout_seconds + HOLD_MARGIN_SECONDS
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        try:
            return await _try_reserve(cfg, est, user_id, investigation_id, purpose, seconds)
        except BudgetDenied as exc:
            if exc.reason != "concurrency" or asyncio.get_running_loop().time() >= deadline:
                raise
        await asyncio.sleep(WAIT_STEP)


async def settle(reservation_id: uuid.UUID, cfg: LLMConfig, cost: Decimal, tokens_in: int, tokens_out: int) -> None:
    """Idempotent. A late result replaces a conservative hold with the real figure."""
    async with session_scope() as db:
        row = (await db.execute(select(LLMReservation).where(LLMReservation.id == reservation_id).with_for_update())).scalar_one()
        if row.status in ("settled", "released"):
            return
        row.status, row.settled_at = "settled", datetime.now(timezone.utc)
        db.add(LLMUsage(llm_config_id=cfg.id, investigation_id=row.investigation_id, user_id=row.user_id, purpose=row.purpose, provider=cfg.provider, model=cfg.model,
                        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost, estimated_usd=row.estimated_usd))


async def close(reservation_id: uuid.UUID, status: str) -> None:
    async with session_scope() as db:
        await db.execute(update(LLMReservation).where(LLMReservation.id == reservation_id, LLMReservation.status == "held").values(status=status, settled_at=func.now()))


async def _unknown(reservation_id: uuid.UUID) -> None:
    await close(reservation_id, "uncertain")


async def complete(provider: Provider, system: str, messages: list[Message], *, json_mode: bool, max_tokens: int, user_id=None,
                   investigation_id=None, purpose: str = "other", wait_seconds: float = 10) -> Any:
    """The one way to call a chat model."""
    cfg = provider.cfg
    chars = len(system) + sum(len(str(m.get("content", ""))) for m in messages)
    rid = await reserve(cfg, estimate(cfg, chars, provider.output_tokens(max_tokens)), user_id=user_id, investigation_id=investigation_id, purpose=purpose, wait_seconds=wait_seconds)
    try:
        resp = await complete_metered(provider, system, messages, json_mode=json_mode, max_tokens=max_tokens)
    except LLMError as exc:
        await close(rid, "released" if exc.kind in DEFINITE else "uncertain")
        raise
    except BaseException:
        await asyncio.shield(_unknown(rid))  # cancelled mid-call: the provider may have billed it
        raise
    await settle(rid, cfg, resp.cost_usd, resp.tokens_in, resp.tokens_out)
    return resp


async def embed(provider: Provider, cfg: LLMConfig, texts: list[str], *, user_id=None, purpose: str = "embedding", wait_seconds: float = 30) -> list[list[float]]:
    """The one way to call an embedding model. Providers do not report usage, so the estimate is the cost."""
    est = estimate(cfg, sum(len(t) for t in texts), 0)
    rid = await reserve(cfg, est, user_id=user_id, purpose=purpose, wait_seconds=wait_seconds)
    start = time.monotonic()
    try:
        with traced("llm.embed", **{"gen_ai.system": cfg.provider, "gen_ai.request.model": cfg.model, "moks.purpose": purpose, "moks.embed.texts": len(texts)}):
            out = await provider.embed(texts)  # a failure is marked on the span by type and kind, never by message
    except LLMError as exc:
        EMBED_REQUESTS.labels(cfg.provider, exc.kind).inc()
        EMBED_LATENCY.labels(cfg.provider).observe(time.monotonic() - start)
        await close(rid, "released" if exc.kind in DEFINITE else "uncertain")
        raise
    except BaseException:
        await asyncio.shield(_unknown(rid))
        raise
    EMBED_REQUESTS.labels(cfg.provider, "ok").inc()
    EMBED_LATENCY.labels(cfg.provider).observe(time.monotonic() - start)
    await settle(rid, cfg, est, sum(len(t) for t in texts) // CHARS_PER_TOKEN, 0)
    return out


def _usd(v: Any) -> str | None:
    return None if v is None else str(Decimal(v).quantize(Decimal("0.0001")))


async def overview(db) -> dict[str, Any]:
    """Spend against every budget, today and this month (UTC), counted the way admission counts it: settled cost plus
    open holds. For Settings → LLM providers and FinOps. The forecast assumes the rest of the month spends at the
    average daily rate so far (at least one day, so an early burst does not read as a whole month of it)."""
    day, month = _start("day"), _start("month")
    open_ = LLMReservation.status.in_(("held", "uncertain"))
    used = (await db.execute(select(LLMUsage.llm_config_id, LLMUsage.provider, func.sum(case((LLMUsage.created_at >= day, LLMUsage.cost_usd), else_=0)),
                                    func.sum(LLMUsage.cost_usd)).where(LLMUsage.created_at >= month).group_by(LLMUsage.llm_config_id, LLMUsage.provider))).all()
    held = (await db.execute(select(LLMReservation.llm_config_id, LLMConfig.provider,
                                    func.sum(case((LLMReservation.created_at >= day, LLMReservation.estimated_usd), else_=0)), func.sum(LLMReservation.estimated_usd))
                             .join(LLMConfig, LLMConfig.id == LLMReservation.llm_config_id).where(LLMReservation.created_at >= month, open_)
                             .group_by(LLMReservation.llm_config_id, LLMConfig.provider))).all()
    llms: dict[Any, list[Decimal]] = {}
    providers: dict[str, list[Decimal]] = {}
    for lid, prov, today, mon in [*used, *held]:
        for box in ([llms.setdefault(lid, [Decimal(0), Decimal(0)])] if lid else []) + [providers.setdefault(prov, [Decimal(0), Decimal(0)])]:
            box[0] += Decimal(today or 0)
            box[1] += Decimal(mon or 0)
    configs = (await db.execute(select(LLMConfig))).scalars().all()
    totals = {t.provider: t for t in (await db.execute(select(LLMProviderBudget))).scalars()}
    now = datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = (resets_at("month", now) - first).days
    elapsed = max((now - first).total_seconds() / 86400, 1.0)
    names = sorted({c.provider for c in configs} | set(totals) | set(providers))
    out_providers = []
    for p in names:
        today, mon = providers.get(p, [Decimal(0), Decimal(0)])
        t = totals.get(p)
        out_providers.append({"provider": p, "llms": sum(1 for c in configs if c.provider == p),
                              "daily_budget_usd": _usd(t.daily_budget_usd) if t else None, "monthly_budget_usd": _usd(t.monthly_budget_usd) if t else None,
                              "spent_today_usd": _usd(today), "spent_month_usd": _usd(mon),
                              "forecast_month_usd": _usd(mon / Decimal(str(elapsed)) * days)})
    return {"day_resets_at": resets_at("day", now).isoformat(), "month_resets_at": resets_at("month", now).isoformat(), "month_start": first.date().isoformat(),
            "llms": {str(c.id): {"spent_today_usd": _usd(llms.get(c.id, [Decimal(0)] * 2)[0]), "spent_month_usd": _usd(llms.get(c.id, [Decimal(0)] * 2)[1])}
                     for c in configs},
            "providers": out_providers}
