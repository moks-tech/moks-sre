"""Model lists for the LLM provider form: what the provider's own API says this credential can use.

Asked of the provider every time (cached for a few minutes), never kept in a table of model names in MOKS, so a new
model shows up the day the provider ships it. The credential is resolved here, as for a call, and never returned.
Each model says what it is for (chat, embedding or other) as far as the provider's answer tells; the form lists the
purpose being configured first and keeps the rest selectable, so a guess never hides a model.
"""
from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ..models import LLMConfig
from .providers import LLMError, Provider

TIMEOUT = 15.0
CACHE_SECONDS = 300
MAX_MODELS = 2000
_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_transport: httpx.AsyncBaseTransport | None = None  # tests answer for the provider

# Ids that are clearly not chat or embedding models: speech, images, moderation, legacy completions.
_OTHER = re.compile(r"whisper|tts|dall-e|gpt-image|image|audio|realtime|transcri|moderation|sora|davinci|babbage|guard|rerank", re.I)
# Embedding families whose names do not say "embed" (bge-m3 on mk4, for one).
_EMBED = re.compile(r"embed|^bge[-:]|^bge$|all-minilm|^e5-|^gte-|nomic-embed|arctic-embed", re.I)


def purpose_of(model_id: str) -> str:
    if _EMBED.search(model_id):
        return "embedding"
    if _OTHER.search(model_id):
        return "other"
    return "chat"


def _per_mtok(per_token: Any) -> str | None:
    """OpenRouter-style prices are USD per token as strings; the form wants USD per million tokens."""
    try:
        v = Decimal(str(per_token)) * 1_000_000
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if v < 0 else format(v.normalize(), "f")


async def _get(url: str, headers: dict[str, str], who: str, params: dict[str, Any] | None = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=_transport) as c:
            r = await c.get(url, headers=headers, params=params)
    except httpx.TimeoutException:
        raise LLMError("timeout", f"{who} did not answer within {TIMEOUT:g}s") from None
    except httpx.TransportError as exc:
        raise LLMError("unavailable", f"cannot reach {who} ({type(exc).__name__})") from None
    if r.status_code in (401, 403):
        raise LLMError("auth", f"{who} rejected the credentials (HTTP {r.status_code})")
    if r.status_code == 404:
        raise LLMError("bad_response", f"{who} has no model list at {url}")
    if r.status_code >= 400:
        raise LLMError("unavailable" if r.status_code >= 500 else "bad_response", f"{who} returned HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise LLMError("bad_response", f"{who} returned non-JSON") from None


async def _openai(cfg: LLMConfig) -> list[dict[str, Any]]:
    from .providers import OpenAIProvider

    p = OpenAIProvider(cfg)
    base = p._base()
    data = await _get(f"{base}/models", p._headers(), base.split("//", 1)[-1].split("/", 1)[0])
    rows = data.get("data", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for m in rows:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        item: dict[str, Any] = {"id": str(m["id"]), "label": str(m.get("name") or m.get("display_name") or m["id"]), "purpose": purpose_of(str(m["id"]))}
        if isinstance(m.get("context_length"), int):
            item["context"] = m["context_length"]
        price = m.get("pricing") if isinstance(m.get("pricing"), dict) else None
        if price:  # OpenRouter publishes them
            item["input_price_per_mtok"], item["output_price_per_mtok"] = _per_mtok(price.get("prompt")), _per_mtok(price.get("completion"))
        out.append(item)
    return out


async def _anthropic(cfg: LLMConfig) -> list[dict[str, Any]]:
    key = Provider(cfg)._key()
    base = (cfg.endpoint or "https://api.anthropic.com").rstrip("/")
    data = await _get(f"{base}/v1/models", {"x-api-key": key or "", "anthropic-version": "2023-06-01"}, "anthropic", {"limit": 1000})
    return [{"id": m["id"], "label": m.get("display_name") or m["id"], "purpose": "chat"} for m in data.get("data", []) if m.get("id")]


async def _gemini(cfg: LLMConfig) -> list[dict[str, Any]]:
    key = Provider(cfg)._key()
    base = (cfg.endpoint or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    out, token = [], None
    for _ in range(5):
        data = await _get(f"{base}/models", {"x-goog-api-key": key or ""}, "gemini", {"pageSize": 1000, **({"pageToken": token} if token else {})})
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods") or []
            mid = str(m.get("name", "")).removeprefix("models/")
            if not mid:
                continue
            purpose = "chat" if "generateContent" in methods else "embedding" if ("embedContent" in methods or "batchEmbedContents" in methods) else "other"
            item: dict[str, Any] = {"id": mid, "label": m.get("displayName") or mid, "purpose": purpose}
            if isinstance(m.get("inputTokenLimit"), int):
                item["context"] = m["inputTokenLimit"]
            out.append(item)
        token = data.get("nextPageToken")
        if not token:
            break
    return out


async def _ollama(cfg: LLMConfig) -> list[dict[str, Any]]:
    if not cfg.endpoint:
        raise LLMError("config", "ollama needs an endpoint, e.g. http://ollama.ollama:11434")
    key = Provider(cfg)._key(required=False)
    data = await _get(f"{cfg.endpoint.rstrip('/')}/api/tags", {"Authorization": f"Bearer {key}"} if key else {}, "ollama")
    return [{"id": m["name"], "label": m["name"], "purpose": purpose_of(m["name"])} for m in data.get("models", []) if m.get("name")]


async def _bedrock(cfg: LLMConfig) -> list[dict[str, Any]]:
    """Foundation models and the system inference profiles (us.…, eu.…) that newer models are called through."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    region = cfg.region or "us-east-1"

    def call() -> list[dict[str, Any]]:
        client = boto3.client("bedrock", region_name=region, config=Config(read_timeout=TIMEOUT, connect_timeout=5, retries={"max_attempts": 2}))
        out: list[dict[str, Any]] = []
        for m in client.list_foundation_models().get("modelSummaries", []):
            outputs = m.get("outputModalities") or []
            purpose = "embedding" if "EMBEDDING" in outputs else "chat" if "TEXT" in outputs else "other"
            legacy = (m.get("modelLifecycle") or {}).get("status") == "LEGACY"
            out.append({"id": m["modelId"], "label": f"{m.get('providerName', '')} {m.get('modelName', m['modelId'])}".strip() + (" (legacy)" if legacy else ""), "purpose": purpose})
        try:
            pages = client.get_paginator("list_inference_profiles").paginate(typeEquals="SYSTEM_DEFINED")
            for page in pages:
                for p in page.get("inferenceProfileSummaries", []):
                    if p.get("status", "ACTIVE") == "ACTIVE":
                        out.append({"id": p["inferenceProfileId"], "label": f"{p.get('inferenceProfileName', p['inferenceProfileId'])} (inference profile)", "purpose": "chat"})
        except ClientError:
            pass  # older identities may list models but not profiles; the models alone still help
        return out

    try:
        return await asyncio.wait_for(asyncio.to_thread(call), TIMEOUT + 5)
    except asyncio.TimeoutError:
        raise LLMError("timeout", f"bedrock did not answer within {TIMEOUT:g}s") from None
    except NoCredentialsError:
        raise LLMError("auth", "no AWS credentials available to MOKS (configure IRSA/Pod Identity or a mounted profile)") from None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError")
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            raise LLMError("auth", f"this AWS identity may not list Bedrock models in {region} (bedrock:ListFoundationModels)") from None
        raise LLMError("unavailable", f"bedrock {code}: {exc.response.get('Error', {}).get('Message', '')[:200]}") from None
    except BotoCoreError as exc:
        raise LLMError("unavailable", f"bedrock: {type(exc).__name__}") from None


async def _huggingface(cfg: LLMConfig) -> list[dict[str, Any]]:
    """A Text Embeddings Inference server runs exactly one model; /info names it and its revision."""
    from .providers import HuggingFaceProvider

    info = await HuggingFaceProvider(cfg, _transport).info()
    if not info.get("model_id"):
        return []
    sha = info.get("model_sha") or ""
    return [{"id": info["model_id"], "label": f"{info['model_id']}" + (f" @ {sha[:12]}" if sha else ""), "purpose": "embedding", "revision": sha or None}]


async def _sentence_transformers(cfg: LLMConfig) -> list[dict[str, Any]]:
    """Nothing to ask: in-process models are whatever is installed. The measured profiles are the suggestions."""
    from ..rag.embeddings import profiles

    return [{"id": p["model"], "label": p["model"] + (" (measured)" if p.get("measured") else ""), "purpose": "embedding"} for p in profiles() if "/" in p["model"] and not p["model"].startswith("openai/")]


LISTERS = {"openai": _openai, "anthropic": _anthropic, "gemini": _gemini, "ollama": _ollama, "bedrock": _bedrock, "huggingface": _huggingface,
           "sentence_transformers": _sentence_transformers}
NOTES = {"azure_openai": "Azure OpenAI is called by deployment name, which an API key cannot list. Type the deployment name from the Azure portal."}


async def list_models(cfg: LLMConfig, *, fresh: bool = False) -> dict[str, Any]:
    """{"models": [...], "error": str | None, "note": str | None}. A provider that cannot be asked is an error to show,
    not an exception: the form still lets someone type a model name."""
    if cfg.provider in NOTES:
        return {"models": [], "error": None, "note": NOTES[cfg.provider]}
    lister = LISTERS.get(cfg.provider)
    if lister is None:
        return {"models": [], "error": f"MOKS cannot list models for {cfg.provider}", "note": None}
    ref = cfg.credential_ref or {}
    from .. import credentials

    key = (cfg.provider, (cfg.endpoint or "").rstrip("/"), cfg.region or "", ref.get("type"), ref.get("name"), ref.get("path"), ref.get("id"),
           credentials.version(ref))  # a replaced key may see different models
    hit = _cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        models = await lister(cfg)
    except LLMError as exc:
        return {"models": [], "error": exc.message, "note": None}
    seen: set[str] = set()
    unique = [m for m in models if not (m["id"] in seen or seen.add(m["id"]))]
    unique.sort(key=lambda m: (m["purpose"] != "chat", m["purpose"] != "embedding", m["label"].lower()))
    out = {"models": unique[:MAX_MODELS], "error": None, "note": f"{len(unique) - MAX_MODELS} more not shown" if len(unique) > MAX_MODELS else None}
    _cache[key] = (time.monotonic(), out)
    return out
