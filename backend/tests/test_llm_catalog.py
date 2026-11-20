"""Model lists for the LLM provider form: asked of each provider with the configured credential, classified by
purpose, priced where the provider says, cached, and never echoing the key."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from moks.llm import catalog
from moks.models import LLMConfig

from .conftest import login, make_user

KEY = "sk-test-not-a-real-key-123"


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    """Records each request and answers from `routes` by path."""
    seen: list[httpx.Request] = []
    routes: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        for path, answer in routes.items():
            if req.url.path.endswith(path):
                if callable(answer):
                    return answer(req)
                return httpx.Response(200, json=answer)
        return httpx.Response(404, json={"error": "not found"})

    monkeypatch.setattr(catalog, "_transport", httpx.MockTransport(handle))
    monkeypatch.setenv("MOKS_CRED_TEST_LLM", KEY)
    catalog._cache.clear()
    yield routes, seen
    catalog._cache.clear()


def cfg(provider: str, endpoint: str | None = None, cred: str | None = "MOKS_CRED_TEST_LLM", region: str | None = None) -> LLMConfig:
    return LLMConfig(provider=provider, model="", endpoint=endpoint, region=region, credential_ref={"type": "env", "name": cred} if cred else {"type": "none"}, timeout_seconds=15)


async def test_openai_lists_chat_models_first_and_labels_the_rest(provider):
    routes, seen = provider
    routes["/v1/models"] = {"data": [{"id": "whisper-1"}, {"id": "text-embedding-3-small"}, {"id": "gpt-zeta"}, {"id": "gpt-alpha"}, {"id": "gpt-alpha"}]}
    out = await catalog.list_models(cfg("openai"))
    assert out["error"] is None
    assert [(m["id"], m["purpose"]) for m in out["models"]] == [("gpt-alpha", "chat"), ("gpt-zeta", "chat"), ("text-embedding-3-small", "embedding"), ("whisper-1", "other")]
    assert seen[0].url == httpx.URL("https://api.openai.com/v1/models") and seen[0].headers["authorization"] == f"Bearer {KEY}"


async def test_openai_compatible_servers_and_their_prices(provider):
    routes, _ = provider
    routes["/api/v1/models"] = {"data": [{"id": "vendor/model-a", "name": "Vendor: Model A", "context_length": 200000, "pricing": {"prompt": "0.000003", "completion": "0.000015"}}]}
    out = await catalog.list_models(cfg("openai", "https://openrouter.ai/api/v1"))
    assert out["models"] == [{"id": "vendor/model-a", "label": "Vendor: Model A", "purpose": "chat", "context": 200000, "input_price_per_mtok": "3", "output_price_per_mtok": "15"}]
    routes.clear()
    routes["/v1/models"] = [{"id": "together/model"}]  # Together answers with a bare list
    assert (await catalog.list_models(cfg("openai", "https://api.together.xyz/v1")))["models"][0]["id"] == "together/model"


async def test_anthropic_gemini_and_ollama(provider):
    routes, seen = provider
    routes["/v1/models"] = {"data": [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"}]}
    out = await catalog.list_models(cfg("anthropic"))
    assert out["models"] == [{"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "purpose": "chat"}]
    assert seen[-1].headers["x-api-key"] == KEY and seen[-1].headers["anthropic-version"]

    pages = iter([{"models": [{"name": "models/gemini-pro-x", "displayName": "Gemini Pro X", "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000000}],
                   "nextPageToken": "p2"},
                  {"models": [{"name": "models/text-embedding-x", "supportedGenerationMethods": ["embedContent"]}]}])
    routes.clear()
    routes["/v1beta/models"] = lambda req: httpx.Response(200, json=next(pages))
    out = await catalog.list_models(cfg("gemini"))
    assert [(m["id"], m["purpose"]) for m in out["models"]] == [("gemini-pro-x", "chat"), ("text-embedding-x", "embedding")]
    assert seen[-1].url.params["pageToken"] == "p2" and seen[-1].headers["x-goog-api-key"] == KEY

    routes.clear()
    routes["/api/tags"] = {"models": [{"name": "bge-m3:latest"}, {"name": "llama3.2:3b"}, {"name": "nomic-embed-text"}]}
    out = await catalog.list_models(cfg("ollama", "http://ollama.ollama:11434", cred=None))
    assert {m["id"]: m["purpose"] for m in out["models"]} == {"llama3.2:3b": "chat", "bge-m3:latest": "embedding", "nomic-embed-text": "embedding"}


async def test_failures_are_explained_not_raised(provider):
    routes, _ = provider
    out = await catalog.list_models(cfg("openai", cred="MOKS_CRED_NOT_SET_ANYWHERE"))
    assert out["models"] == [] and "MOKS_CRED_NOT_SET_ANYWHERE is not set" in out["error"]
    routes["/v1/models"] = lambda req: httpx.Response(401, json={"error": "bad key"})
    out = await catalog.list_models(cfg("anthropic"))
    assert "rejected the credentials" in out["error"] and KEY not in json.dumps(out)
    out = await catalog.list_models(cfg("azure_openai", "https://res.openai.azure.com"))
    assert out["models"] == [] and "deployment name" in out["note"]


async def test_answers_are_cached_until_asked_fresh(provider):
    routes, seen = provider
    routes["/v1/models"] = {"data": [{"id": "gpt-alpha"}]}
    await catalog.list_models(cfg("openai"))
    await catalog.list_models(cfg("openai"))
    assert len(seen) == 1
    await catalog.list_models(cfg("openai"), fresh=True)
    assert len(seen) == 2


async def test_bedrock_lists_models_and_inference_profiles(monkeypatch):
    class Pages:
        def paginate(self, **kw):
            assert kw == {"typeEquals": "SYSTEM_DEFINED"}
            return [{"inferenceProfileSummaries": [{"inferenceProfileId": "us.vendor.model-v1:0", "inferenceProfileName": "US Model", "status": "ACTIVE"}]}]

    class Client:
        def list_foundation_models(self):
            return {"modelSummaries": [
                {"modelId": "amazon.titan-embed-text-v2:0", "modelName": "Titan Embeddings", "providerName": "Amazon", "outputModalities": ["EMBEDDING"]},
                {"modelId": "vendor.model-v1:0", "modelName": "Model", "providerName": "Vendor", "outputModalities": ["TEXT"], "modelLifecycle": {"status": "LEGACY"}}]}

        def get_paginator(self, name):
            assert name == "list_inference_profiles"
            return Pages()

    import boto3

    monkeypatch.setattr(boto3, "client", lambda service, **kw: Client() if service == "bedrock" and kw["region_name"] == "eu-west-1" else None)
    out = await catalog.list_models(cfg("bedrock", cred=None, region="eu-west-1"))
    assert [(m["id"], m["purpose"]) for m in out["models"]] == [("us.vendor.model-v1:0", "chat"), ("vendor.model-v1:0", "chat"), ("amazon.titan-embed-text-v2:0", "embedding")]
    assert out["models"][1]["label"].endswith("(legacy)")


async def test_api_lists_for_llm_admins_only(client, provider):
    routes, _ = provider
    routes["/api/tags"] = {"models": [{"name": "llama3.2:3b"}]}
    admin = f"a-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(admin, ["admin:*:*"])
    h = await login(client, admin)
    body = {"provider": "ollama", "endpoint": "http://ollama.ollama:11434", "credential_ref": {"type": "env", "name": "MOKS_CRED_TEST_LLM"}}
    r = await client.post("/moks/api/admin/llm/models", json=body, headers=h)
    assert r.status_code == 200 and r.json()["models"][0]["id"] == "llama3.2:3b" and KEY not in r.text
    assert (await client.post("/moks/api/admin/llm/models", json={**body, "credential_ref": {"type": "env", "name": "OTHER_SECRET"}}, headers=h)).status_code == 422  # only MOKS_CRED_*
    viewer = f"v-{uuid.uuid4().hex[:6]}@example.com"
    await make_user(viewer, ["viewer:*:*"])
    hv = await login(client, viewer)
    assert (await client.post("/moks/api/admin/llm/models", json=body, headers=hv)).status_code == 403
