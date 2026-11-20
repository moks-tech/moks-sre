"""Administration: connectors, LLM providers, users and role bindings, audit log.

Credentials are never accepted or returned here: rows store credential *references*
(env var names under MOKS_CRED_* or files under MOKS_CREDENTIALS_DIR) that point to Kubernetes
Secrets mounted into the pods.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import needs
from ..auth.routes import EMAIL, hasher
from ..auth.sessions import AuthContext, client_ip, revoke_all_for_user
from ..connectors import registry
from ..connectors.base import describe_credential_ref
from ..db import get_db
from ..llm import budget, catalog
from ..llm.providers import LLMError, provider_for
from ..models import AuditEvent, Connector, EmbeddingIndex, Environment, LLMConfig, RoleBinding, User
from ..rag import embeddings
from ..rbac import ROLES, P
from .common import flush_unique

router = APIRouter(prefix="/admin", tags=["admin"])

CONNECTOR_TYPES = ["kubernetes", "prometheus", "loki", "grafana", "cloudwatch", "git", "aws_cost", "cost_model", "aws_secrets", "postgres", "redis"]


class CredentialRef(BaseModel):
    """Where a credential comes from. A pasted value is stored first (POST /admin/credentials) and referenced by id."""
    type: Literal["none", "stored", "env", "file"] = "none"
    id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    name: str | None = Field(default=None, pattern=r"^MOKS_CRED_[A-Z0-9_]{1,80}$")
    path: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._/-]{1,200}$")

    @field_validator("path")
    @classmethod
    def _no_traversal(cls, v: str | None) -> str | None:
        if v and (".." in v.split("/") or v.startswith("/")):
            raise ValueError("path must be relative to MOKS_CREDENTIALS_DIR")
        return v

    @model_validator(mode="after")
    def _complete(self) -> "CredentialRef":
        if self.type == "stored" and not self.id:
            raise ValueError("a stored credential reference needs its id")
        return self


def _check_no_secrets(config: dict[str, Any]) -> None:
    for k in config:
        if any(w in k.lower() for w in ("password", "secret", "token", "apikey", "api_key", "private")):
            raise HTTPException(422, f"config key {k!r} looks like a credential; use credential_ref instead")


# ---- connectors --------------------------------------------------------------------------------

class ConnectorIn(BaseModel):
    environment: str
    type: Literal["kubernetes", "prometheus", "loki", "grafana", "cloudwatch", "git", "aws_cost", "cost_model", "aws_secrets", "postgres", "redis"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)


def connector_out(c: Connector, env_name: str) -> dict[str, Any]:
    return {"id": str(c.id), "environment": env_name, "type": c.type, "name": c.name, "enabled": c.enabled, "config": c.config,
            "credential": describe_credential_ref(c.credential_ref), "credential_ref": c.credential_ref,
            "implemented": c.type in registry.CONNECTOR_TYPES,
            "last_test_at": c.last_test_at.isoformat() if c.last_test_at else None, "last_test_ok": c.last_test_ok,
            "last_test_message": c.last_test_message}


@router.get("/connectors")
async def list_connectors(ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    rows = (await db.execute(select(Connector, Environment.name).join(Environment).order_by(Environment.name, Connector.type))).all()
    return [connector_out(c, n) for c, n in rows]


async def _env_id(db: AsyncSession, name: str) -> uuid.UUID:
    env = (await db.execute(select(Environment).where(Environment.name == name))).scalar_one_or_none()
    if env is None:
        raise HTTPException(404, f"Unknown environment {name!r}")
    return env.id


@router.post("/connectors", status_code=201)
async def create_connector(body: ConnectorIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _check_no_secrets(body.config)
    c = Connector(environment_id=await _env_id(db, body.environment), type=body.type, name=body.name, enabled=body.enabled,
                  config=body.config, credential_ref=body.credential_ref.model_dump(exclude_none=True))
    db.add(c)
    await flush_unique(db, f"A connector named {body.name!r} already exists in {body.environment}; edit it or choose another name")
    await audit.record(db, action="connector.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(c.id), environment=body.environment, details=body.model_dump())
    return connector_out(c, body.environment)


@router.put("/connectors/{cid}")
async def update_connector(cid: uuid.UUID, body: ConnectorIn, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)),
                           db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _check_no_secrets(body.config)
    c = await db.get(Connector, cid)
    if c is None:
        raise HTTPException(404, "Unknown connector")
    before = {"config": c.config, "credential_ref": c.credential_ref, "enabled": c.enabled}
    c.environment_id = await _env_id(db, body.environment)
    c.type, c.name, c.enabled, c.config = body.type, body.name, body.enabled, body.config
    c.credential_ref = body.credential_ref.model_dump(exclude_none=True)
    c.last_test_ok = None
    await flush_unique(db, f"A connector named {body.name!r} already exists in {body.environment}; edit it or choose another name")
    await audit.record(db, action="connector.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(c.id), environment=body.environment, details={"before": before, "after": body.model_dump()})
    return connector_out(c, body.environment)


@router.delete("/connectors/{cid}")
async def delete_connector(cid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    c = await db.get(Connector, cid)
    if c is None:
        raise HTTPException(404, "Unknown connector")
    await db.delete(c)
    await audit.record(db, action="connector.delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(cid), details={"type": c.type, "name": c.name})
    return {"status": "deleted"}


@router.post("/connectors/{cid}/test")
async def test_connector(cid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(Connector, cid)
    if c is None:
        raise HTTPException(404, "Unknown connector")
    ok, msg = await registry.test(c)
    c.last_test_at, c.last_test_ok, c.last_test_message = datetime.now(timezone.utc), ok, msg[:1000]
    await audit.record(db, action="connector.test", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="connector",
                       resource_id=str(cid), outcome="success" if ok else "failure", details={"message": msg[:500]})
    return {"ok": ok, "message": msg}


@router.get("/datastores/discover")
async def discover_datastores(environment: str, ctx: AuthContext = Depends(needs(P.ADMIN_CONNECTORS)),
                              db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """PostgreSQL and Redis candidates from the environment's Kubernetes connector, for the add-cluster form.
    Reads Services and CloudNativePG Clusters within the environment's namespace scope; never Secrets."""
    from .. import datastores
    from ..connectors.base import ConnectorError
    from ..scope import Scope

    env = (await db.execute(select(Environment).where(Environment.name == environment))).scalar_one_or_none()
    if env is None:
        raise HTTPException(404, f"Unknown environment {environment!r}")
    connectors, _ = await registry.for_environment(db, env.id)
    k8s = connectors.get("kubernetes")
    if k8s is None:
        raise HTTPException(409, "Discovery needs a kubernetes connector in this environment")
    scope = Scope(env.name, ("*",), tuple(env.allowed_namespaces or ["*"]), tuple(env.denied_namespaces or []))
    try:
        found = await datastores.discover(k8s[1], scope)
    except ConnectorError as exc:
        raise HTTPException(502, exc.message) from None
    configured = {(c.type, (c.config or {}).get("host")) for c in (await db.execute(select(Connector).where(Connector.environment_id == env.id))).scalars()}
    for kind in ("postgres", "redis"):
        for cand in found[kind]:
            cand["configured"] = (kind, cand["config"]["host"]) in configured
    return {"environment": env.name, **found}


# ---- LLM ---------------------------------------------------------------------------------------

Providers = Literal["anthropic", "openai", "azure_openai", "gemini", "bedrock", "ollama", "huggingface", "sentence_transformers"]


class EmbeddingOptionsIn(BaseModel):
    """What changes an embedding model's vectors beyond its name (moks/rag/embeddings.py). Any change here is a new
    model key: an index built before it no longer compares, so the active one cannot be edited this way."""
    query_prefix: str = Field(default="", max_length=200)
    document_prefix: str = Field(default="", max_length=200)
    max_input_chars: int | None = Field(default=None, ge=64, le=200_000)
    revision: str = Field(default="", max_length=80, pattern=r"^[A-Za-z0-9._/-]*$")


class LLMIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    purpose: Literal["chat", "embedding"] = "chat"
    provider: Providers
    model: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    region: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_output_tokens: int = Field(default=2000, ge=100, le=16000)
    daily_budget_usd: Decimal = Field(default=Decimal("5"), ge=0, le=10000)
    input_price_per_mtok: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    output_price_per_mtok: Decimal = Field(default=Decimal("0"), ge=0, le=1000)
    embedding_dimensions: int | None = Field(default=None, ge=8, le=8192)
    embedding_options: EmbeddingOptionsIn = Field(default_factory=EmbeddingOptionsIn)

    @model_validator(mode="after")
    def _purpose_fits(self) -> "LLMIn":
        from ..llm.providers import EMBEDDING_ONLY

        if self.provider in EMBEDDING_ONLY and self.purpose != "embedding":
            raise ValueError(f"{self.provider} serves embedding models only")
        return self

    def row(self) -> dict[str, Any]:
        from ..rag.embeddings import clean_options

        data = self.model_dump(exclude={"credential_ref", "embedding_options"})
        data["embedding_options"] = clean_options(self.embedding_options.model_dump()) if self.purpose == "embedding" else {}
        return data


class ModelsIn(BaseModel):
    """The connection part of the provider form, so models can be listed before anything is saved."""
    provider: Providers
    endpoint: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    region: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    fresh: bool = False


def llm_out(c: LLMConfig, spent: Decimal | None = None) -> dict[str, Any]:
    return {"id": str(c.id), "name": c.name, "purpose": c.purpose, "provider": c.provider, "model": c.model, "endpoint": c.endpoint,
            "region": c.region, "credential": describe_credential_ref(c.credential_ref), "credential_ref": c.credential_ref,
            "timeout_seconds": c.timeout_seconds, "max_output_tokens": c.max_output_tokens, "daily_budget_usd": str(c.daily_budget_usd),
            "input_price_per_mtok": str(c.input_price_per_mtok), "output_price_per_mtok": str(c.output_price_per_mtok),
            "embedding_dimensions": c.embedding_dimensions, "embedding_options": c.embedding_options or {}, "is_active": c.is_active,
            "model_key": embeddings.model_key(c) if c.purpose == "embedding" else None,
            "spent_last_24h_usd": str(spent) if spent is not None else None}


@router.get("/llm")
async def list_llm(ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    from datetime import timedelta

    from ..models import LLMUsage

    rows = (await db.execute(select(LLMConfig).order_by(LLMConfig.purpose, LLMConfig.name))).scalars().all()
    since = datetime.now(timezone.utc) - timedelta(days=1)
    spent = dict((await db.execute(select(LLMUsage.llm_config_id, func.sum(LLMUsage.cost_usd)).where(LLMUsage.created_at >= since)
                                   .group_by(LLMUsage.llm_config_id))).all())
    return [llm_out(r, spent.get(r.id, Decimal(0))) for r in rows]


@router.post("/llm", status_code=201)
async def create_llm(body: LLMIn, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = LLMConfig(**{**body.row(), "credential_ref": body.credential_ref.model_dump(exclude_none=True)})
    db.add(c)
    await flush_unique(db, f"An LLM provider named {body.name!r} already exists; edit it or choose another name")
    await audit.record(db, action="llm.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config",
                       resource_id=str(c.id), details=body.model_dump(mode="json"))
    return llm_out(c)


@router.get("/llm/embedding-profiles")
async def embedding_profiles(ctx: AuthContext = Depends(needs(P.ADMIN_LLM))) -> list[dict[str, Any]]:
    """Suggested prefixes for known embedding models, to fill in the form. Applied only when someone saves them."""
    return embeddings.profiles()


@router.post("/llm/models")
async def llm_models(body: ModelsIn, ctx: AuthContext = Depends(needs(P.ADMIN_LLM))) -> dict[str, Any]:
    """Models the provider says this credential can use, asked of the provider itself (moks/llm/catalog.py).
    A provider that cannot be asked answers 200 with `error`, so the form falls back to typing a name."""
    cfg = LLMConfig(provider=body.provider, model="", endpoint=body.endpoint or None, region=body.region if body.provider == "bedrock" else None,
                    credential_ref=body.credential_ref.model_dump(exclude_none=True), timeout_seconds=int(catalog.TIMEOUT))
    return await catalog.list_models(cfg, fresh=body.fresh)


@router.put("/llm/{lid}")
async def update_llm(lid: uuid.UUID, body: LLMIn, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    if c.purpose == "embedding":
        before = embeddings.model_key(c)
        after = embeddings.model_key(SimpleNamespace(**{**body.row(), "provider": body.provider}))
        used = (await db.execute(select(func.count()).select_from(EmbeddingIndex).where(EmbeddingIndex.llm_config_id == c.id,
                                                                                     EmbeddingIndex.status.in_(("active", "building", "retired"))))).scalar_one()
        if before != after and (c.is_active or used):
            raise HTTPException(409, "This embedding configuration made vectors that an index still holds, and the change would make different ones. "
                                     "Add a new configuration with these settings and rebuild the index with it; the current one keeps serving until the switch.")
    for k, v in body.row().items():
        setattr(c, k, v)
    c.credential_ref = body.credential_ref.model_dump(exclude_none=True)
    await flush_unique(db, f"An LLM provider named {body.name!r} already exists; edit it or choose another name")
    await audit.record(db, action="llm.update", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config",
                       resource_id=str(c.id), details=body.model_dump(mode="json"))
    return llm_out(c)


@router.delete("/llm/{lid}")
async def delete_llm(lid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    """Usage history stays: each row keeps its provider, model and cost. Only the link to this config goes."""
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    if c.is_active:
        raise HTTPException(409, "Deactivate this provider before deleting it")
    await audit.record(db, action="llm.delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config", resource_id=str(c.id),
                       decision="allow", details={"name": c.name, "provider": c.provider, "model": c.model})
    await db.delete(c)
    return {"status": "deleted"}


@router.post("/llm/{lid}/activate")
async def activate_llm(lid: uuid.UUID, active: bool = True, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)),
                       db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    if active:
        for other in (await db.execute(select(LLMConfig).where(LLMConfig.purpose == c.purpose, LLMConfig.is_active.is_(True)))).scalars():
            other.is_active = False
        await db.flush()
    c.is_active = active
    await audit.record(db, action="llm.activate" if active else "llm.deactivate", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="llm_config", resource_id=str(c.id))
    return llm_out(c)


@router.post("/llm/{lid}/test")
async def test_llm(lid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    c = await db.get(LLMConfig, lid)
    if c is None:
        raise HTTPException(404, "Unknown LLM configuration")
    extra: dict[str, Any] = {}
    try:
        if c.purpose == "embedding":
            provider = provider_for(c)
            embedder = embeddings.Embedder(c, provider)
            vector = await embedder.query("connection test", user_id=ctx.user.id, purpose="connection_test", wait_seconds=30)
            dims = len(vector)
            extra = {"provider": c.provider, "model": c.model, "dimensions": dims, "model_key": embedder.key}
            if c.embedding_dimensions and c.embedding_dimensions != dims:
                raise LLMError("bad_response", f"{c.provider}/{c.model} returned {dims} dimensions but {c.embedding_dimensions} are configured")
            served = await provider.info() if hasattr(provider, "info") else None
            if served is not None:  # a TEI-style server says which model and revision it runs; it must be the configured one
                extra["served"] = {"model_id": served.get("model_id"), "revision": served.get("model_sha")}
                if served.get("model_id") and served["model_id"] != c.model:
                    raise LLMError("bad_response", f"the server runs {served['model_id']}, not {c.model}")
                wanted = (c.embedding_options or {}).get("revision")
                if wanted and served.get("model_sha") and served["model_sha"] != wanted:
                    raise LLMError("bad_response", f"the server runs revision {served['model_sha']}, not {wanted}")
            ok, msg = True, f"{c.provider}/{c.model} returned a {dims}-dimension embedding"
        else:
            resp = await budget.complete(provider_for(c), "Reply with the JSON object {\"ok\": true} and nothing else.",
                                         [{"role": "user", "content": "connection test"}], json_mode=True, max_tokens=c.max_output_tokens,
                                         user_id=ctx.user.id, purpose="connection_test")
            ok, msg = True, f"{c.provider}/{c.model} answered ({resp.tokens_in}+{resp.tokens_out} tokens): {resp.text[:80]}"
    except LLMError as exc:
        ok, msg = False, f"{exc.kind}: {exc.message}"
    await audit.record(db, action="llm.test", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="llm_config",
                       resource_id=str(c.id), outcome="success" if ok else "failure", details={"message": msg[:300], **extra})
    return {"ok": ok, "message": msg, **extra}


# ---- users and role bindings ------------------------------------------------------------------

class UserIn(BaseModel):
    email: str = Field(pattern=EMAIL, max_length=320)
    display_name: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, min_length=12, max_length=512)


class BindingIn(BaseModel):
    role: Literal["viewer", "investigator", "operator", "approver", "knowledge_reviewer", "knowledge_admin", "index_operator", "skill_publisher", "ml_reviewer", "ml_admin", "secrets_viewer", "secrets_operator", "secrets_approver", "admin"]
    environment: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,100}$")
    namespace: str = Field(default="*", pattern=r"^[a-z0-9*?\-]{1,63}$")


async def user_out(db: AsyncSession, u: User) -> dict[str, Any]:
    bs = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == u.id).order_by(RoleBinding.created_at))).scalars().all()
    return {"id": str(u.id), "email": u.email, "display_name": u.display_name, "auth_provider": u.auth_provider, "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "bindings": [{"id": str(b.id), "role": b.role, "environment": b.environment, "namespace": b.namespace, "source": b.source} for b in bs]}


@router.get("/users")
async def list_users(ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> list[dict[str, Any]]:
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return [await user_out(db, u) for u in users]


@router.post("/users", status_code=201)
async def create_user(body: UserIn, request: Request, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)),
                      db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    from ..config import get_settings

    mode = get_settings().auth_mode
    if mode == "oidc":
        raise HTTPException(422, "With OIDC, users are created at their first sign-in; grant roles afterwards")
    if (await db.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none():
        raise HTTPException(409, "User exists")
    if mode == "local" and not body.password:
        raise HTTPException(422, "A password of at least 12 characters is required in local mode")
    u = User(email=body.email.lower(), display_name=body.display_name, auth_provider=mode,
             password_hash=hasher.hash(body.password) if mode == "local" and body.password else None)
    db.add(u)
    await db.flush()
    await audit.record(db, action="user.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="user",
                       resource_id=str(u.id), details={"email": u.email}, ip=client_ip(request))
    return await user_out(db, u)


@router.post("/users/{uid}/active")
async def set_active(uid: uuid.UUID, active: bool, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    u = await db.get(User, uid)
    if u is None:
        raise HTTPException(404, "Unknown user")
    if u.id == ctx.user.id and not active:
        raise HTTPException(409, "You cannot disable your own account")
    u.is_active = active
    if not active:
        await revoke_all_for_user(db, u.id)
    await audit.record(db, action="user.enable" if active else "user.disable", actor_id=ctx.user.id, actor_email=ctx.user.email,
                       resource_type="user", resource_id=str(u.id))
    return await user_out(db, u)


@router.post("/users/{uid}/bindings", status_code=201)
async def add_binding(uid: uuid.UUID, body: BindingIn, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    u = await db.get(User, uid)
    if u is None:
        raise HTTPException(404, "Unknown user")
    if body.role not in ROLES:
        raise HTTPException(422, "Unknown role")
    exists = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == uid, RoleBinding.role == body.role,
                                                         RoleBinding.environment == body.environment,
                                                         RoleBinding.namespace == body.namespace))).scalar_one_or_none()
    if not exists:
        db.add(RoleBinding(user_id=uid, created_by=ctx.user.id, **body.model_dump()))
        await audit.record(db, action="rbac.grant", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="user",
                           resource_id=str(uid), environment=body.environment, namespace=body.namespace, details=body.model_dump())
    await db.flush()
    return await user_out(db, u)


@router.delete("/users/{uid}/bindings/{bid}")
async def remove_binding(uid: uuid.UUID, bid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_USERS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    b = await db.get(RoleBinding, bid)
    if b is None or b.user_id != uid:
        raise HTTPException(404, "Unknown binding")
    if uid == ctx.user.id and b.role == "admin" and b.environment == "*" and b.namespace == "*":
        others = (await db.execute(select(func.count()).select_from(RoleBinding).where(
            RoleBinding.role == "admin", RoleBinding.environment == "*", RoleBinding.namespace == "*", RoleBinding.user_id != uid))).scalar_one()
        if not others:
            raise HTTPException(409, "You are the last administrator")
    await db.delete(b)
    await audit.record(db, action="rbac.revoke", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="user",
                       resource_id=str(uid), environment=b.environment, namespace=b.namespace, details={"role": b.role})
    await db.flush()
    u = await db.get(User, uid)
    return await user_out(db, u)


# ---- audit -------------------------------------------------------------------------------------

@router.get("/audit")
async def list_audit(action: str | None = None, actor: str | None = None, decision: str | None = None, environment: str | None = None,
                     namespace: str | None = None, outcome: str | None = None, resource: str | None = None, q_text: str | None = None,
                     since: str | None = None, until: str | None = None,
                     before_id: int | None = None, limit: int = 100, ctx: AuthContext = Depends(needs(P.AUDIT_READ)),
                     db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    from datetime import datetime as _dt

    from sqlalchemy import String, cast, or_

    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(max(limit, 1), 500))
    if namespace:
        q = q.where(AuditEvent.namespace == namespace)
    if outcome:
        q = q.where(AuditEvent.outcome == outcome)
    if resource:
        q = q.where(or_(AuditEvent.resource_type.ilike(f"%{resource}%"), AuditEvent.resource_id.ilike(f"%{resource}%")))
    if q_text:
        q = q.where(cast(AuditEvent.details, String).ilike(f"%{q_text[:200]}%"))
    for value, op in ((since, "ge"), (until, "le")):
        if value:
            try:
                t = _dt.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(422, f"invalid time {value!r}") from None
            q = q.where(AuditEvent.ts >= t if op == "ge" else AuditEvent.ts <= t)
    if action:
        q = q.where(AuditEvent.action.like(action.replace("*", "%")))
    if actor:
        q = q.where(AuditEvent.actor_email.ilike(f"%{actor}%"))
    if decision:
        q = q.where(AuditEvent.decision == decision)
    if environment and environment != "all":
        q = q.where(AuditEvent.environment == environment)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    rows = (await db.execute(q)).scalars().all()
    return {"events": [{
        "id": r.id, "ts": r.ts.isoformat(), "actor_email": r.actor_email, "action": r.action, "resource_type": r.resource_type,
        "resource_id": r.resource_id, "environment": r.environment, "namespace": r.namespace, "decision": r.decision,
        "outcome": r.outcome, "details": r.details, "ip": r.ip, "chain_seq": r.chain_seq, "hash": r.hash,
    } for r in rows]}


@router.get("/audit/facets")
async def audit_facets(ctx: AuthContext = Depends(needs(P.AUDIT_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Distinct values for the audit filter dropdowns (most recent 20k records)."""
    from sqlalchemy import text as _text

    rows = {}
    for col in ("action", "environment", "namespace", "outcome", "resource_type", "actor_email"):
        rows[col] = [r[0] for r in (await db.execute(_text(
            f"SELECT DISTINCT {col} FROM (SELECT {col} FROM audit_events ORDER BY id DESC LIMIT 20000) t WHERE {col} IS NOT NULL ORDER BY 1 LIMIT 200"))).all()]
    return rows


@router.get("/backups")
async def backups(ctx: AuthContext = Depends(needs(P.AUDIT_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    from .. import backup

    return await backup.status(db)


@router.get("/audit/verify")
async def verify_audit(ctx: AuthContext = Depends(needs(P.AUDIT_READ)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await audit.verify_chain(db)
