"""The embedding layer: one interface for every provider, prefixes and truncation from the configuration, unit
vectors, and a model key that changes exactly when the vectors would."""
from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace

import httpx
import pytest

from moks.connectors.base import ConnectorError
from moks.llm import providers
from moks.llm.providers import LLMError, provider_for
from moks.rag import embeddings
from moks.rag.embeddings import EmbeddingSpec, Embedder, clean_options, model_key, normalize, profile_for


def cfg(provider="ollama", model="bge-m3:567m", dims=None, **options):
    return SimpleNamespace(name="e", purpose="embedding", provider=provider, model=model, embedding_dimensions=dims, embedding_options=options,
                           endpoint="http://embedder:8090", region=None, credential_ref={"type": "none"}, timeout_seconds=30)


class Recorder:
    """A provider that remembers what it was sent and returns unnormalised vectors."""

    def __init__(self, size=3):
        self.size, self.sent = size, []

    async def embed(self, texts):
        self.sent.append(list(texts))
        return [[float(len(t) % 7 + 1)] + [2.0] * (self.size - 1) for t in texts]


@pytest.fixture(autouse=True)
def unmetered(monkeypatch):
    async def direct(provider, c, texts, **kwargs):
        return await provider.embed(texts)
    monkeypatch.setattr(embeddings.budget, "embed", direct)


def test_a_configuration_without_options_keeps_the_model_key_every_existing_index_has():
    assert model_key(cfg(dims=1024)) == "ollama/bge-m3:567m/1024"
    assert model_key(cfg()) == "ollama/bge-m3:567m/default"
    assert model_key(None) == "keyword-only"


def test_every_option_that_changes_a_vector_changes_the_key_and_nothing_else_does():
    base = model_key(cfg())
    keys = {model_key(cfg(query_prefix="query: ")), model_key(cfg(document_prefix="passage: ")), model_key(cfg(max_input_chars=2000)), model_key(cfg(revision="abc123"))}
    assert base not in keys and len(keys) == 4
    assert model_key(cfg(query_prefix="query: ", revision="r")) == model_key(cfg(revision="r", query_prefix="query: "))  # order does not matter
    assert model_key(cfg(unknown_option="x")) == base  # a typo cannot fork the key
    assert model_key(cfg(query_prefix="")) == base


def test_clean_options_keeps_types_and_bounds():
    assert clean_options({"query_prefix": "q: ", "max_input_chars": "512", "revision": "", "other": 1}) == {"query_prefix": "q: ", "max_input_chars": 512}
    assert len(clean_options({"query_prefix": "x" * 500})["query_prefix"]) == embeddings.MAX_PREFIX_CHARS


async def test_queries_and_documents_get_their_own_prefixes_and_truncation():
    rec = Recorder()
    e = Embedder(cfg(query_prefix="query: ", document_prefix="passage: ", max_input_chars=5), rec)
    await e.documents(["0123456789", "ab"])
    await e.query("why is my pod restarting")
    assert rec.sent == [["passage: 01234", "passage: ab"], ["query: why i"]]


async def test_every_vector_comes_back_unit_length():
    e = Embedder(cfg(), Recorder(size=4))
    for v in await e.documents(["a", "bbbb"]) + [await e.query("c")]:
        assert math.isclose(sum(x * x for x in v), 1.0, rel_tol=1e-9)


def test_normalize_refuses_zero_and_non_finite_vectors():
    with pytest.raises(LLMError, match="zero or non-finite"):
        normalize([[0.0, 0.0]])
    with pytest.raises(LLMError):
        normalize([[float("nan"), 1.0]])
    with pytest.raises(LLMError):
        normalize([[]])


async def test_a_provider_that_answers_short_or_mixed_is_refused():
    class Short:
        async def embed(self, texts):
            return [[1.0, 0.0]]

    class Mixed:
        async def embed(self, texts):
            return [[1.0, 0.0], [1.0, 0.0, 0.0]]

    with pytest.raises(LLMError, match="asked for 2"):
        await Embedder(cfg(), Short()).documents(["a", "b"])
    with pytest.raises(LLMError, match="different sizes"):
        await Embedder(cfg(), Mixed()).documents(["a", "b"])


async def test_the_probe_reports_the_real_size_and_a_configured_size_must_agree():
    assert await Embedder(cfg(), Recorder(size=5)).probe() == 5
    with pytest.raises(ConnectorError, match="configured for 8 dimensions but returns 5"):
        await Embedder(cfg(dims=8), Recorder(size=5)).probe()


def test_spec_describes_what_produced_the_vectors():
    d = EmbeddingSpec.of(cfg(dims=384, query_prefix="q: ", revision="abc")).describe()
    assert d["key"] == model_key(cfg(dims=384, query_prefix="q: ", revision="abc")) and d["fingerprint"] and d["revision"] == "abc"


def test_profiles_are_suggestions_found_by_any_spelling_of_the_name():
    assert profile_for("BAAI/bge-small-en-v1.5")["query_prefix"].startswith("Represent this sentence")
    assert profile_for("/opt/models/bge-small-en-v1.5")["model"] == "BAAI/bge-small-en-v1.5"
    assert profile_for("bge-m3:567m")["query_prefix"] == ""
    assert profile_for("intfloat/e5-base-v2")["document_prefix"] == "passage: "
    assert profile_for("my-own-model") is None
    for p in embeddings.profiles():
        assert set(p) >= {"model", "match", "query_prefix", "document_prefix", "license", "measured"}
        assert "dimensions" not in p  # sizes come from the model, never from a list


# ---- providers --------------------------------------------------------------------------------

def tei(handler):
    return provider_for(cfg(provider="huggingface", model="BAAI/bge-small-en-v1.5"), httpx.MockTransport(handler))


async def test_huggingface_speaks_the_text_embeddings_inference_api():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.url.path] = json.loads(req.content) if req.content else None
        if req.url.path == "/info":
            return httpx.Response(200, json={"model_id": "BAAI/bge-small-en-v1.5", "model_sha": "5c38ec7c"})
        return httpx.Response(200, json=[[0.1, 0.2], [0.3, 0.4]])

    p = tei(handler)
    assert await p.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["/embed"] == {"inputs": ["a", "b"], "normalize": True, "truncate": True}
    assert (await p.info())["model_sha"] == "5c38ec7c"


async def test_huggingface_refuses_an_answer_that_is_not_a_list_of_vectors():
    p = tei(lambda req: httpx.Response(200, json={"error": "nope"}))
    with pytest.raises(LLMError, match="list of vectors"):
        await p.embed(["a"])
    p = tei(lambda req: httpx.Response(401, json={}))
    with pytest.raises(LLMError) as raised:
        await p.embed(["a"])
    assert raised.value.kind == "auth"


async def test_embedding_only_providers_refuse_chat_and_need_an_endpoint():
    p = provider_for(cfg(provider="huggingface"))
    with pytest.raises(LLMError, match="embedding models only"):
        await p.complete("s", [{"role": "user", "content": "hi"}], json_mode=False, max_tokens=10)
    with pytest.raises(LLMError, match="endpoint"):
        await provider_for(SimpleNamespace(**{**vars(cfg(provider="huggingface")), "endpoint": None})).embed(["a"])


async def test_sentence_transformers_says_what_to_do_when_it_is_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    providers.SentenceTransformersProvider._loaded.clear()
    p = provider_for(cfg(provider="sentence_transformers", model="BAAI/bge-small-en-v1.5"))
    with pytest.raises(LLMError, match="MOKS embedding service") as raised:
        await p.embed(["a"])
    assert raised.value.kind == "config"


async def test_sentence_transformers_loads_once_per_model_and_revision(monkeypatch):
    loads = []

    class Model:
        def encode(self, texts, **kwargs):
            assert kwargs["normalize_embeddings"] is True
            import numpy as np  # noqa: PLC0415 - only where a fake model needs it
            return np.array([[1.0, 0.0]] * len(texts))

    def load(model, revision):
        loads.append((model, revision))
        return Model()

    pytest.importorskip("numpy")
    providers.SentenceTransformersProvider._loaded.clear()
    monkeypatch.setattr(providers.SentenceTransformersProvider, "_load", staticmethod(load))
    p = provider_for(cfg(provider="sentence_transformers", model="m", revision="r1"))
    await p.embed(["a"])
    await p.embed(["b", "c"])
    await provider_for(cfg(provider="sentence_transformers", model="m", revision="r2")).embed(["d"])
    assert loads == [("m", "r1"), ("m", "r2")]
