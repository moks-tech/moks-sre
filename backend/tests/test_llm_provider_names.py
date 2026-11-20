"""Every provider the LLM form offers can be saved, and only a real duplicate name is reported as one."""
from __future__ import annotations

import typing
import uuid

from sqlalchemy import text

from moks.api.admin import LLMIn, ModelsIn
from moks.db import session_scope
from moks.llm.providers import PROVIDERS

from .conftest import login, make_user


def offered(model) -> set[str]:
    return set(typing.get_args(model.model_fields["provider"].annotation))


async def test_the_database_accepts_every_provider_the_api_offers():
    async with session_scope() as db:
        check = (await db.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                        "WHERE conrelid = 'llm_configs'::regclass AND conname = 'llm_configs_provider_check'"))).scalar_one()
    assert offered(LLMIn) == offered(ModelsIn) == set(PROVIDERS)
    for name in PROVIDERS:
        assert f"'{name}'" in check, f"llm_configs_provider_check does not allow {name}"


async def test_anthropic_and_azure_providers_save_and_a_real_duplicate_is_a_conflict(client):
    admin = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(admin, ["admin:*:*"])
    h = await login(client, admin)
    name = f"claude-chat-{uuid.uuid4().hex[:6]}"
    body = {"name": name, "purpose": "chat", "provider": "anthropic", "model": "claude-sonnet-4-5",
            "credential_ref": {"type": "env", "name": "MOKS_CRED_ANTHROPIC_API_KEY"}}
    r = await client.post("/moks/api/admin/llm", json=body, headers=h)
    assert r.status_code == 201, r.text
    azure = {**body, "name": f"azure-{uuid.uuid4().hex[:6]}", "provider": "azure_openai", "endpoint": "https://x.openai.azure.com"}
    assert (await client.post("/moks/api/admin/llm", json=azure, headers=h)).status_code == 201
    again = await client.post("/moks/api/admin/llm", json=body, headers=h)
    assert again.status_code == 409 and "already exists" in again.json()["detail"]
    listed = [p["name"] for p in (await client.get("/moks/api/admin/llm", headers=h)).json()]
    assert name in listed
