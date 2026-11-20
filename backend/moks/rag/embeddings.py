"""Embedding models behind one interface.

Everything that turns text into vectors goes through an Embedder: indexing, search, the dimension probe, the
connection test and the evaluations. A configuration (an LLMConfig with purpose "embedding") names a provider and a
model; the provider only knows how to send a list of strings and get vectors back. This module adds what the
provider does not know, from the configuration's `embedding_options`:

- asymmetric inputs. Some models are trained with an instruction before the query ("Represent this sentence for
  searching relevant passages: ") or with "query: " and "passage: " prefixes. Without them those models rank worse.
- truncation. A model reads a fixed number of tokens; cutting the text here makes what was embedded explicit.
- one vector shape. Every vector is L2-normalised here. All three backends rank by cosine, which normalisation does
  not change, so vectors written before this existed still compare with vectors written after it.
- a fingerprint of everything that changes a vector (prefixes, truncation, revision). It is part of the model key,
  so an index can tell whether a query vector compares with what it holds. A configuration without options keeps
  the model key it always had, and with it the index it already has.

Nothing here names a model or a size. embedding_profiles.yaml only fills in the form; a configuration's own options
decide, and the dimensions are what the model returns.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..connectors.base import ConnectorError
from ..llm import budget
from ..llm.providers import LLMError, Provider

OPTION_KEYS = ("query_prefix", "document_prefix", "max_input_chars", "revision")
PROFILES = Path(__file__).with_name("embedding_profiles.yaml")
MAX_PREFIX_CHARS = 200


@dataclass(frozen=True)
class EmbeddingSpec:
    """What decides a vector. Two configurations with the same spec produce vectors that compare."""
    provider: str
    model: str
    dimensions: int | None = None  # configured; None means whatever the model returns
    query_prefix: str = ""
    document_prefix: str = ""
    max_input_chars: int | None = None
    revision: str = ""

    @classmethod
    def of(cls, cfg: Any) -> "EmbeddingSpec":
        opts = clean_options(getattr(cfg, "embedding_options", None) or {})
        return cls(provider=cfg.provider, model=cfg.model, dimensions=cfg.embedding_dimensions, **opts)

    def options(self) -> dict[str, Any]:
        """The options that are set, which is all a fingerprint covers."""
        return {k: v for k, v in ((k, getattr(self, k)) for k in OPTION_KEYS) if v not in ("", None)}

    def fingerprint(self) -> str:
        opts = self.options()
        if not opts:
            return ""
        return hashlib.sha256(json.dumps(opts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]

    def key(self) -> str:
        """The model key an index records. The first three parts are the format every existing index has."""
        base = f"{self.provider}/{self.model}/{self.dimensions or 'default'}"
        fp = self.fingerprint()
        return f"{base}/{fp}" if fp else base

    def describe(self) -> dict[str, Any]:
        """For the index's record and for evaluation results: what produced these vectors."""
        return {**asdict(self), "fingerprint": self.fingerprint(), "key": self.key()}

    def _cut(self, text: str) -> str:
        return text[:self.max_input_chars] if self.max_input_chars else text

    def document_input(self, text: str) -> str:
        return f"{self.document_prefix}{self._cut(text)}"

    def query_input(self, text: str) -> str:
        return f"{self.query_prefix}{self._cut(text)}"


def clean_options(raw: dict[str, Any]) -> dict[str, Any]:
    """Only known options, in their types. Unknown keys are dropped rather than fingerprinted, so a typo cannot
    silently fork the model key."""
    out: dict[str, Any] = {}
    for k in ("query_prefix", "document_prefix", "revision"):
        v = raw.get(k)
        if v not in (None, ""):
            out[k] = str(v)[:MAX_PREFIX_CHARS]
    n = raw.get("max_input_chars")
    if n not in (None, "", 0):
        out["max_input_chars"] = int(n)
    return out


def model_key(cfg: Any) -> str:
    return EmbeddingSpec.of(cfg).key() if cfg else "keyword-only"


def normalize(vectors: list[list[float]]) -> list[list[float]]:
    out = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v))
        if not v or not math.isfinite(norm) or norm == 0:
            raise LLMError("bad_response", "the embedding model returned an empty, zero or non-finite vector")
        out.append([x / norm for x in v])
    return out


class Embedder:
    """One configuration's vectors. Every call is metered through the budget, like a chat call."""

    def __init__(self, cfg: Any, provider: Provider):
        self.cfg = cfg
        self.provider = provider
        self.spec = EmbeddingSpec.of(cfg)

    @property
    def key(self) -> str:
        return self.spec.key()

    async def _call(self, inputs: list[str], **kwargs: Any) -> list[list[float]]:
        vectors = await budget.embed(self.provider, self.cfg, inputs, **kwargs)
        if len(vectors) != len(inputs):
            raise LLMError("bad_response", f"asked for {len(inputs)} embeddings and got {len(vectors)}")
        sizes = {len(v) for v in vectors}
        if len(sizes) > 1:
            raise LLMError("bad_response", f"one request returned vectors of different sizes {sorted(sizes)}")
        return normalize(vectors)

    def document_inputs(self, texts: list[str]) -> list[str]:
        return [self.spec.document_input(t) for t in texts]

    async def documents(self, texts: list[str], *, purpose: str = "indexing", user_id=None) -> list[list[float]]:
        return await self._call(self.document_inputs(texts), purpose=purpose, user_id=user_id)

    async def query(self, text: str, *, user_id=None, purpose: str = "search", wait_seconds: float = 2) -> list[float]:
        return (await self._call([self.spec.query_input(text)], user_id=user_id, purpose=purpose, wait_seconds=wait_seconds))[0]

    async def probe(self, *, user_id=None, purpose: str = "dimension_probe") -> int:
        """The model's real output size. A configured size must agree: a vector field cannot change size."""
        size = len((await self._call([self.spec.document_input("dimension probe")], user_id=user_id, purpose=purpose))[0])
        if self.cfg.embedding_dimensions and self.cfg.embedding_dimensions != size:
            raise ConnectorError("config", f"{self.cfg.name} is configured for {self.cfg.embedding_dimensions} dimensions but returns {size}")
        return size


@lru_cache(maxsize=1)
def profiles() -> list[dict[str, Any]]:
    return (yaml.safe_load(PROFILES.read_text()) or {}).get("profiles") or []


def profile_for(model: str) -> dict[str, Any] | None:
    """Suggested options for a model name, matched on its last path part so "BAAI/bge-small-en-v1.5", a local
    directory and an Ollama tag of the same model all find it. A suggestion only: nothing applies it unasked."""
    name = model.rstrip("/").rsplit("/", 1)[-1].split(":", 1)[0].lower()
    for p in profiles():
        if name in {m.lower() for m in p.get("match") or []}:
            return p
    return None
