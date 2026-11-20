"""Settings → LLM providers: a monthly budget per LLM and daily and monthly totals per provider, all optional."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select

from moks.db import session_scope
from moks.models import AuditEvent

from .conftest import login, make_user


async def _admin(client):
    email = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["admin:*:*"])
    return await login(client, email)


async def test_a_monthly_budget_is_optional_and_round_trips(client):
    h = await _admin(client)
    base = {"purpose": "chat", "provider": "anthropic", "model": "claude-haiku-5-5", "credential_ref": {"type": "env", "name": "MOKS_CRED_ANTHROPIC_API_KEY"}}
    plain = await client.post("/moks/api/admin/llm", json={**base, "name": f"plain-{uuid.uuid4().hex[:6]}"}, headers=h)
    assert plain.status_code == 201 and plain.json()["monthly_budget_usd"] is None  # nothing preset: no monthly limit
    capped = await client.post("/moks/api/admin/llm", json={**base, "name": f"capped-{uuid.uuid4().hex[:6]}", "monthly_budget_usd": "25"}, headers=h)
    assert capped.status_code == 201 and Decimal(capped.json()["monthly_budget_usd"]) == 25
    row = next(r for r in (await client.get("/moks/api/admin/llm", headers=h)).json() if r["id"] == capped.json()["id"])
    assert Decimal(row["monthly_budget_usd"]) == 25 and Decimal(row["spent_today_usd"]) == Decimal(row["spent_month_usd"]) == 0
    cleared = await client.put(f"/moks/api/admin/llm/{row['id']}", json={**base, "name": row["name"], "monthly_budget_usd": None}, headers=h)
    assert cleared.status_code == 200 and cleared.json()["monthly_budget_usd"] is None
    assert (await client.post("/moks/api/admin/llm", json={**base, "name": f"neg-{uuid.uuid4().hex[:6]}", "monthly_budget_usd": "-1"}, headers=h)).status_code == 422


async def test_provider_totals_are_set_listed_and_removed(client):
    h = await _admin(client)
    try:
        r = await client.put("/moks/api/admin/llm/totals/azure_openai", json={"daily_budget_usd": "4", "monthly_budget_usd": "100"}, headers=h)
        assert r.status_code == 200, r.text
        assert (r.json()["daily_budget_usd"], r.json()["monthly_budget_usd"]) == ("4.0000", "100.0000")
        listed = (await client.get("/moks/api/admin/llm/totals", headers=h)).json()
        mine = next(p for p in listed["providers"] if p["provider"] == "azure_openai")
        assert mine["monthly_budget_usd"] == "100.0000" and "forecast_month_usd" in mine and listed["month_resets_at"]
        r = await client.put("/moks/api/admin/llm/totals/azure_openai", json={"daily_budget_usd": None, "monthly_budget_usd": "80"}, headers=h)
        assert (r.json()["daily_budget_usd"], r.json()["monthly_budget_usd"]) == (None, "80.0000")
    finally:
        r = await client.put("/moks/api/admin/llm/totals/azure_openai", json={"daily_budget_usd": None, "monthly_budget_usd": None}, headers=h)
    assert r.status_code == 200 and r.json()["monthly_budget_usd"] is None
    async with session_scope() as db:
        changes = (await db.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "llm.total.update",
                                                                                       AuditEvent.resource_id == "azure_openai"))).scalar_one()
    assert changes >= 3  # every change is audited, removal included
    assert (await client.put("/moks/api/admin/llm/totals/nope", json={"monthly_budget_usd": "1"}, headers=h)).status_code == 422


async def test_totals_need_the_llm_admin_permission(client):
    email = f"v-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["viewer:*:*"])
    h = await login(client, email)
    assert (await client.get("/moks/api/admin/llm/totals", headers=h)).status_code == 403
    assert (await client.put("/moks/api/admin/llm/totals/anthropic", json={"monthly_budget_usd": "1"}, headers=h)).status_code == 403
