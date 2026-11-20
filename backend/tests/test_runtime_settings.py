"""Settings: curated runtime values within their bounds, limits that only tighten, audit, every process applying
them, and a deployment view that never returns a secret."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from moks import runtime_settings as rs
from moks.config import get_settings
from moks.db import session_scope
from moks.models import AuditEvent, RuntimeSetting

from .conftest import login, make_user

URL = "/moks/api/admin/settings"


@pytest.fixture(autouse=True)
async def clean():
    yield
    async with session_scope() as db:
        await db.execute(delete(RuntimeSetting))
    await rs.refresh()


async def admin(client) -> dict:
    email = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["admin:*:*"])
    return await login(client, email)


def item(body: dict, key: str) -> dict:
    return next(x for x in body["runtime"] if x["key"] == key)


async def test_admin_tightens_a_limit_and_every_reader_sees_it(client):
    h = await admin(client)
    body = (await client.get(URL, headers=h)).json()
    auto = item(body, "auto_investigations_per_hour")
    assert auto["value"] == auto["helm_value"] == 6 and not auto["overridden"] and auto["max"] == 6  # Helm is the ceiling of a limit
    r = await client.put(f"{URL}/auto_investigations_per_hour", json={"value": 3, "reason": "noisy alerts this week"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["value"] == 3 and r.json()["overridden"] and r.json()["updated_by"].startswith("a-")
    assert get_settings().auto_investigations_per_hour == 3  # alerts.py and detection.py read it at use
    async with session_scope() as db:
        ev = (await db.execute(select(AuditEvent).where(AuditEvent.action == "settings.update", AuditEvent.resource_id == "auto_investigations_per_hour"))).scalars().all()[-1]
        assert ev.details["from"] == 6 and ev.details["to"] == 3 and ev.details["reason"] == "noisy alerts this week"

    r = await client.put(f"{URL}/auto_investigations_per_hour", json={"value": 7, "reason": "more please"}, headers=h)
    assert r.status_code == 422 and "Helm" in r.json()["detail"]  # loosening a limit stays a Helm change

    r = await client.post(f"{URL}/auto_investigations_per_hour/reset", json={"reason": "back to normal"}, headers=h)
    assert r.status_code == 200 and r.json()["value"] == 6 and not r.json()["overridden"]
    assert get_settings().auto_investigations_per_hour == 6


async def test_schedules_and_switches_move_freely_within_their_bounds(client):
    h = await admin(client)
    assert (await client.put(f"{URL}/ml_anomaly_interval_minutes", json={"value": 60, "reason": "less often"}, headers=h)).json()["value"] == 60
    assert get_settings().ml_anomaly_interval_minutes == 60
    assert (await client.put(f"{URL}/ml_anomaly_interval_minutes", json={"value": 2, "reason": "too often"}, headers=h)).status_code == 422
    assert (await client.put(f"{URL}/ml_anomaly_interval_minutes", json={"value": 7.5, "reason": "fraction"}, headers=h)).status_code == 422
    assert (await client.put(f"{URL}/intent_llm_classifier", json={"value": False, "reason": "rules only"}, headers=h)).status_code == 200
    assert get_settings().intent_llm_classifier is False
    assert (await client.put(f"{URL}/intent_llm_classifier", json={"value": "no", "reason": "rules only"}, headers=h)).status_code == 422


async def test_an_optional_limit_can_be_set_and_cleared_when_helm_sets_none(client):
    h = await admin(client)
    r = await client.put(f"{URL}/llm_user_daily_budget_usd", json={"value": 2.5, "reason": "cap spend"}, headers=h)
    assert r.status_code == 200 and r.json()["value"] == 2.5 and r.json()["max"] == 10000
    assert get_settings().llm_user_daily_budget_usd == 2.5
    r = await client.put(f"{URL}/llm_user_daily_budget_usd", json={"value": None, "reason": "no cap"}, headers=h)
    assert r.status_code == 200 and r.json()["value"] is None and not r.json()["overridden"]  # the Helm value: no row is kept
    assert get_settings().llm_user_daily_budget_usd is None
    async with session_scope() as db:
        assert await db.get(RuntimeSetting, "llm_user_daily_budget_usd") is None


async def test_unknown_keys_bad_reasons_and_non_admins_are_refused(client):
    h = await admin(client)
    assert (await client.put(f"{URL}/session_secret", json={"value": "x" * 40, "reason": "rotate"}, headers=h)).status_code == 404
    assert (await client.put(f"{URL}/max_tool_calls", json={"value": 99, "reason": "more"}, headers=h)).status_code == 404  # a hard ceiling is Helm's
    assert (await client.put(f"{URL}/auto_investigations_per_hour", json={"value": 2, "reason": ""}, headers=h)).status_code == 422
    viewer = f"v-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(viewer, ["viewer:*:*"])
    hv = await login(client, viewer)
    assert (await client.get(URL, headers=hv)).status_code == 403
    assert (await client.put(f"{URL}/auto_investigations_per_hour", json={"value": 2, "reason": "quiet"}, headers=hv)).status_code == 403
    assert get_settings().auto_investigations_per_hour == 6


async def test_the_deployment_view_never_returns_a_secret(client):
    h = await admin(client)
    sections = (await client.get(URL, headers=h)).json()["deployment"]
    flat = {i["key"]: i for s in sections for i in s["items"]}
    for key in ("session_secret", "oidc_client_secret", "alert_webhook_token", "secrets_digest_key", "opensearch_password", "database_url"):
        assert flat[key]["secret"] and "value" not in flat[key], key
    assert flat["session_secret"]["set"] is True
    assert flat["auto_investigations_per_hour"]["editable"] and not flat["max_tool_calls"]["editable"]
    assert {s["section"] for s in sections} >= {"Authentication and sessions", "Investigations", "Machine learning", "CrewAI", "Platform"}
    raw = str(sections)
    assert get_settings().session_secret not in raw


def test_credentials_inside_urls_are_masked(monkeypatch):
    monkeypatch.setattr(get_settings(), "opensearch_url", "https://search:hunter2@os.internal:9200")
    flat = {i["key"]: i for s in rs.deployment() for i in s["items"]}
    assert flat["opensearch_url"]["value"] == "https://***@os.internal:9200"


async def test_other_processes_pick_up_changes_and_ignore_rows_that_no_longer_fit():
    async with session_scope() as db:
        db.add(RuntimeSetting(key="crewai_max_iterations", value={"value": 2}))
        db.add(RuntimeSetting(key="knowledge_max_searches", value={"value": 50}))  # above Helm's 3: saved under an older ceiling, say
    await rs.refresh()  # what versions.report does in the worker, executor and every API replica
    assert get_settings().crewai_max_iterations == 2
    assert get_settings().knowledge_max_searches == 3
    async with session_scope() as db:
        listed = {x["key"]: x for x in await rs.describe(db)}
    assert listed["knowledge_max_searches"]["ignored"] and not listed["knowledge_max_searches"]["overridden"]
    async with session_scope() as db:
        await db.execute(delete(RuntimeSetting))
    await rs.refresh()
    assert get_settings().crewai_max_iterations == 5  # removing the row brings the Helm value back


async def test_refresh_leaves_values_it_never_set_alone(monkeypatch):
    monkeypatch.setattr(get_settings(), "crewai_max_tool_calls", 2)  # what other tests do
    await rs.refresh()
    assert get_settings().crewai_max_tool_calls == 2


def test_check_explains_each_refusal():
    spec = rs.SPECS["crewai_timeout_seconds"]
    assert rs.check(spec, "60") == 60
    with pytest.raises(rs.Invalid, match="whole number"):
        rs.check(spec, 60.5)
    with pytest.raises(rs.Invalid, match="at least 10"):
        rs.check(spec, 5)
    with pytest.raises(rs.Invalid, match="lowered here, not raised"):
        rs.check(spec, 600)  # Helm's 120 is the ceiling
    with pytest.raises(rs.Invalid, match="needs a value"):
        rs.check(spec, None)
    with pytest.raises(rs.Invalid, match="number"):
        rs.check(spec, True)
