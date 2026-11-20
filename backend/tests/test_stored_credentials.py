"""Credentials pasted into Settings: stored encrypted, usable by every connector and provider, never shown again,
and importable from the MOKS_CRED_* variables a deployment already has."""
from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select, text

from moks import credentials
from moks.config import get_settings
from moks.connectors.base import ConnectorError, describe_credential_ref, resolve_credential
from moks.db import session_scope
from moks.models import AuditEvent, Connector, StoredCredential

from .conftest import login, make_env, make_user

VALUE = "sk-ant-test-" + uuid.uuid4().hex


async def admin(client) -> dict:
    email = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(email, ["admin:*:*"])
    return await login(client, email)


async def test_a_pasted_value_is_stored_encrypted_resolves_and_never_comes_back(client, caplog):
    caplog.set_level(logging.DEBUG)
    h = await admin(client)
    r = await client.post("/moks/api/admin/credentials", json={"label": "Anthropic API key", "value": VALUE}, headers=h)
    assert r.status_code == 201, r.text
    ref = r.json()["ref"]
    assert ref["type"] == "stored" and VALUE not in r.text
    assert resolve_credential(ref) == VALUE
    assert describe_credential_ref(ref) == "stored (set)"

    listed = await client.get("/moks/api/admin/credentials", headers=h)
    assert listed.status_code == 200 and VALUE not in listed.text
    row = next(c for c in listed.json()["credentials"] if c["id"] == ref["id"])
    assert row["label"] == "Anthropic API key" and row["state"] == "current" and row["used_by"] == []

    async with session_scope() as db:
        stored = await db.get(StoredCredential, uuid.UUID(ref["id"]))
        assert VALUE.encode() not in bytes(stored.ciphertext)
        audits = (await db.execute(select(AuditEvent).where(AuditEvent.resource_id == ref["id"]))).scalars().all()
        assert audits and all(VALUE not in str(a.details) for a in audits)
    assert VALUE not in caplog.text


async def test_replacing_changes_the_value_and_version(client):
    h = await admin(client)
    ref = (await client.post("/moks/api/admin/credentials", json={"label": "token", "value": "first"}, headers=h)).json()["ref"]
    before = credentials.version(ref)
    r = await client.put(f"/moks/api/admin/credentials/{ref['id']}", json={"value": "second"}, headers=h)
    assert r.status_code == 200 and "second" not in r.text
    assert resolve_credential(ref) == "second" and credentials.version(ref) == before + 1
    assert (await client.put(f"/moks/api/admin/credentials/{uuid.uuid4()}", json={"value": "x"}, headers=h)).status_code == 404
    assert (await client.post("/moks/api/admin/credentials", json={"label": "empty", "value": "  "}, headers=h)).status_code == 422


async def test_an_llm_provider_uses_a_stored_key_and_a_used_credential_cannot_be_deleted(client):
    h = await admin(client)
    ref = (await client.post("/moks/api/admin/credentials", json={"label": "claude", "value": VALUE}, headers=h)).json()["ref"]
    name = f"claude-{uuid.uuid4().hex[:6]}"
    r = await client.post("/moks/api/admin/llm", json={"name": name, "purpose": "chat", "provider": "anthropic", "model": "claude-sonnet-4-5", "credential_ref": ref}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["credential"] == "stored (set)" and VALUE not in r.text
    row = next(c for c in (await client.get("/moks/api/admin/credentials", headers=h)).json()["credentials"] if c["id"] == ref["id"])
    assert f"LLM provider {name}" in row["used_by"]
    refused = await client.delete(f"/moks/api/admin/credentials/{ref['id']}", headers=h)
    assert refused.status_code == 409 and name in refused.json()["detail"]
    bad = await client.post("/moks/api/admin/llm", json={"name": f"x-{uuid.uuid4().hex[:6]}", "purpose": "chat", "provider": "anthropic", "model": "m",
                                                          "credential_ref": {"type": "stored"}}, headers=h)
    assert bad.status_code == 422


async def test_unused_credentials_can_be_deleted_and_then_do_not_resolve(client):
    h = await admin(client)
    ref = (await client.post("/moks/api/admin/credentials", json={"label": "spare", "value": "v"}, headers=h)).json()["ref"]
    assert (await client.delete(f"/moks/api/admin/credentials/{ref['id']}", headers=h)).status_code == 200
    with pytest.raises(ConnectorError, match="does not exist"):
        resolve_credential(ref)


async def test_importing_the_environment_points_references_at_stored_copies(client, monkeypatch):
    h = await admin(client)
    var = f"MOKS_CRED_IMPORT_{uuid.uuid4().hex[:6].upper()}"
    monkeypatch.setenv(var, "from-the-secret\n")
    env_name = f"imp-{uuid.uuid4().hex[:6]}"
    await make_env(env_name, connectors=[
        {"type": "grafana", "name": "grafana", "config": {"base_url": "http://g"}, "credential_ref": {"type": "env", "name": var}},
        {"type": "aws_secrets", "name": "aws", "config": {"account_id": "123456789012", "credentials": {"access_key_id": {"type": "env", "name": var},
                                                                                                         "secret_access_key": {"type": "env", "name": "MOKS_CRED_NOT_SET_ANYWHERE"}}},
         "credential_ref": None},
    ])
    r = await client.post("/moks/api/admin/credentials/import-environment", headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert var in out["stored"] and "MOKS_CRED_NOT_SET_ANYWHERE" in out["missing"] and "from-the-secret" not in r.text
    async with session_scope() as db:
        rows = {c.name: c for c in (await db.execute(text("SELECT c.* FROM connectors c JOIN environments e ON e.id = c.environment_id WHERE e.name = :n"), {"n": env_name})).mappings()}
    grafana, aws = rows["grafana"], rows["aws"]
    assert grafana["credential_ref"]["type"] == "stored"
    assert aws["config"]["credentials"]["access_key_id"] == grafana["credential_ref"]  # one stored copy per variable
    assert aws["config"]["credentials"]["secret_access_key"] == {"type": "env", "name": "MOKS_CRED_NOT_SET_ANYWHERE"}
    monkeypatch.delenv(var)
    assert resolve_credential(grafana["credential_ref"]) == "from-the-secret"


async def test_a_value_is_bound_to_its_row_and_to_a_key_this_deployment_has(client, monkeypatch):
    h = await admin(client)
    a = (await client.post("/moks/api/admin/credentials", json={"label": "a", "value": "alpha"}, headers=h)).json()["ref"]
    b = (await client.post("/moks/api/admin/credentials", json={"label": "b", "value": "beta"}, headers=h)).json()["ref"]
    async with session_scope() as db:
        ra, rb = await db.get(StoredCredential, uuid.UUID(a["id"])), await db.get(StoredCredential, uuid.UUID(b["id"]))
        rb.nonce, rb.ciphertext, rb.version = ra.nonce, ra.ciphertext, rb.version + 1
    await credentials.refresh(force=True)
    with pytest.raises(ConnectorError, match="encryption key"):
        resolve_credential(b)

    # A dedicated key: older rows still open with the session-derived key, and re-encryption moves them.
    monkeypatch.setattr(get_settings(), "credentials_key", "k" * 48)
    assert credentials.current_key().source == "MOKS_CREDENTIALS_KEY"
    assert resolve_credential(a) == "alpha"
    moved = await client.post("/moks/api/admin/credentials/reencrypt", headers=h)
    assert moved.status_code == 200 and moved.json()["reencrypted"] >= 1 and moved.json()["unreadable"] >= 1
    states = {c["id"]: c["state"] for c in (await client.get("/moks/api/admin/credentials", headers=h)).json()["credentials"]}
    assert states[a["id"]] == "current" and states[b["id"]] == "unreadable"

    # Losing the key that sealed a row: it says so, and names the place to enter it again.
    monkeypatch.setattr(get_settings(), "credentials_key", "other" * 10)
    monkeypatch.setattr(get_settings(), "session_secret", "s" * 48)
    with pytest.raises(ConnectorError, match="Settings > Credentials"):
        resolve_credential(a)


async def test_only_people_who_configure_credentials_may_store_or_list_them(client):
    viewer = f"v-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(viewer, ["viewer:*:*"])
    hv = await login(client, viewer)
    assert (await client.post("/moks/api/admin/credentials", json={"label": "x", "value": "y"}, headers=hv)).status_code == 403
    assert (await client.get("/moks/api/admin/credentials", headers=hv)).status_code == 403
    assert (await client.post("/moks/api/admin/credentials/import-environment", headers=hv)).status_code == 403


async def test_another_process_picks_up_a_new_credential_without_a_restart(client):
    h = await admin(client)
    ref = (await client.post("/moks/api/admin/credentials", json={"label": "fresh", "value": "new-value"}, headers=h)).json()["ref"]
    credentials.forget()  # as if this were another replica that has not loaded it yet
    with pytest.raises(ConnectorError, match="not loaded here yet"):
        resolve_credential(ref)
    await client.get("/moks/api/admin/credentials", headers=h)  # any request refreshes
    assert resolve_credential(ref) == "new-value"


async def test_connectors_rebuild_when_their_stored_credential_changes(client):
    from moks.connectors import registry

    h = await admin(client)
    ref = (await client.post("/moks/api/admin/credentials", json={"label": "pg", "value": "one"}, headers=h)).json()["ref"]
    row = Connector(id=uuid.uuid4(), environment_id=uuid.uuid4(), type="grafana", name="g", config={}, credential_ref=ref)
    before = registry._build_key(row)
    await client.put(f"/moks/api/admin/credentials/{ref['id']}", json={"value": "two"}, headers=h)
    assert registry._build_key(row) != before


async def test_deployment_credentials_come_from_settings_first_then_the_environment(client, monkeypatch):
    from moks.rag import service

    h = await admin(client)
    monkeypatch.setenv("MOKS_CRED_PINECONE_API_KEY", "from-env")
    monkeypatch.setattr(service, "_pinecone", None)
    before = credentials.named("MOKS_CRED_PINECONE_API_KEY")
    if before is None:
        assert credentials.deployment_value("MOKS_CRED_PINECONE_API_KEY") == "from-env"
    listed = (await client.get("/moks/api/admin/credentials", headers=h)).json()
    assert any(d["name"] == "MOKS_CRED_PINECONE_API_KEY" for d in listed["deployment"])
    first = service.pinecone()
    r = await client.post("/moks/api/admin/credentials", json={"label": "MOKS_CRED_PINECONE_API_KEY", "value": "pasted-in-settings"}, headers=h)
    assert r.status_code == 201
    assert credentials.deployment_value("MOKS_CRED_PINECONE_API_KEY") == "pasted-in-settings"
    second = service.pinecone()
    assert second is not first and second.client.headers["Api-Key"] == "pasted-in-settings"
    row = next(c for c in (await client.get("/moks/api/admin/credentials", headers=h)).json()["credentials"] if c["id"] == r.json()["id"])
    assert row["used_by"] == ["Deployment (MOKS_CRED_PINECONE_API_KEY)"]
    assert (await client.delete(f"/moks/api/admin/credentials/{r.json()['id']}", headers=h)).status_code == 409
    async with session_scope() as db:  # leave no deployment credential behind for later tests
        await db.execute(text("DELETE FROM stored_credentials WHERE label = 'MOKS_CRED_PINECONE_API_KEY'"))
    await credentials.refresh(force=True)
    monkeypatch.setattr(service, "_pinecone", None)


async def test_importing_twice_reuses_the_stored_copy(client, monkeypatch):
    h = await admin(client)
    var = f"MOKS_CRED_TWICE_{uuid.uuid4().hex[:6].upper()}"
    monkeypatch.setenv(var, "same")
    env_name = f"twice-{uuid.uuid4().hex[:6]}"
    await make_env(env_name, connectors=[{"type": "grafana", "name": "g1", "config": {"base_url": "http://g"}, "credential_ref": {"type": "env", "name": var}}])
    await client.post("/moks/api/admin/credentials/import-environment", headers=h)
    await make_env(f"{env_name}-b", connectors=[{"type": "grafana", "name": "g2", "config": {"base_url": "http://g"}, "credential_ref": {"type": "env", "name": var}}])
    await client.post("/moks/api/admin/credentials/import-environment", headers=h)
    async with session_scope() as db:
        assert (await db.execute(select(StoredCredential).where(StoredCredential.label == var))).scalars().all().__len__() == 1
