"""Stored credentials (docs/CREDENTIALS.md): an administrator pastes a key or token into a form, and the form saves a
reference to it. Values go in and never come out: no response, log line, audit detail or trace carries one."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, credentials
from ..auth.deps import deny, get_auth, needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import StoredCredential
from ..rbac import P

router = APIRouter(prefix="/admin/credentials", tags=["admin"])

# Whoever may configure something that takes a credential may store one for it.
WRITERS = ((P.ADMIN_CONNECTORS, "*"), (P.ADMIN_LLM, "*"), (P.ADMIN_SETTINGS, "*"), (P.KNOWLEDGE_SOURCE_ADMIN, None))


async def writer(request: Request, ctx: AuthContext = Depends(get_auth)) -> AuthContext:
    if not any(ctx.principal.can(p, scope, scope) for p, scope in WRITERS):
        raise await deny(request, ctx, P.ADMIN_SETTINGS, "*", "*", "Storing a credential needs admin.connectors, admin.llm, admin.settings or knowledge.source_admin")
    return ctx


class CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    value: SecretStr


class ReplaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: SecretStr
    label: str | None = Field(default=None, min_length=1, max_length=200)


def _out(row: StoredCredential) -> dict[str, Any]:
    return {"ref": {"type": "stored", "id": str(row.id)}, "id": str(row.id), "label": row.label, "version": row.version,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@router.get("")
async def list_credentials(ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return await credentials.describe(db)


@router.post("", status_code=201)
async def create(body: CredentialIn, ctx: AuthContext = Depends(writer), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    try:
        cid = await credentials.store(db, label=body.label, value=body.value.get_secret_value(), actor=ctx.user.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await audit.record(db, action="credential.create", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="stored_credential",
                       resource_id=str(cid), decision="allow", details={"label": body.label})
    return _out(await db.get(StoredCredential, cid))


@router.put("/{cid}")
async def replace(cid: uuid.UUID, body: ReplaceIn, ctx: AuthContext = Depends(writer), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    try:
        await credentials.replace(db, cid, value=body.value.get_secret_value(), actor=ctx.user.id, label=body.label)
    except KeyError:
        raise HTTPException(404, "Unknown credential") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    row = await db.get(StoredCredential, cid)
    await audit.record(db, action="credential.replace", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="stored_credential",
                       resource_id=str(cid), decision="allow", details={"label": row.label, "version": row.version})
    return _out(row)


@router.delete("/{cid}")
async def delete(cid: uuid.UUID, ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, str]:
    row = await db.get(StoredCredential, cid)
    if row is None:
        raise HTTPException(404, "Unknown credential")
    used = (await credentials.usage(db)).get(cid)
    if used:
        raise HTTPException(409, f"{row.label} is still used by {', '.join(used[:5])}; point those at another credential first")
    await audit.record(db, action="credential.delete", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="stored_credential",
                       resource_id=str(cid), decision="allow", details={"label": row.label})
    await db.delete(row)
    await db.flush()
    await credentials.refresh(db, force=True)
    return {"status": "deleted"}


@router.post("/import-environment")
async def import_environment(ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    """Copies every MOKS_CRED_* value that a connector, LLM provider, knowledge source or backup storage names into
    stored credentials, and points those references at the copies. The Kubernetes Secret is left as it is."""
    out = await credentials.import_environment(db, ctx.user.id)
    await audit.record(db, action="credential.import_environment", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="stored_credential",
                       decision="allow", details={"variables": out["stored"], "references": len(out["references"]), "missing": out["missing"]})
    return out


@router.post("/reencrypt")
async def reencrypt(ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, int]:
    out = await credentials.reencrypt(db)
    await audit.record(db, action="credential.reencrypt", actor_id=ctx.user.id, actor_email=ctx.user.email, resource_type="stored_credential",
                       decision="allow", details=out)
    return out
