"""Settings (docs/SETTINGS.md): the curated runtime values an administrator may change, and the deployment's
configuration, read-only. Secrets are never returned. Every change is audited."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import runtime_settings as rs
from ..auth.deps import needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..rbac import P

router = APIRouter(prefix="/admin/settings", tags=["admin"])


@router.get("")
async def overview(ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    return {"runtime": await rs.describe(db), "groups": rs.GROUPS, "deployment": rs.deployment(), "apply_seconds": rs.APPLY_SECONDS}


class ValueIn(BaseModel):
    value: Any = None
    reason: str = Field(min_length=3, max_length=500)


class ResetIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _spec(key: str) -> rs.Spec:
    spec = rs.SPECS.get(key)
    if spec is None:
        raise HTTPException(404, "That setting cannot be changed here. Deployment values outside Settings are changed in Helm.")
    return spec


async def _one(db: AsyncSession, key: str) -> dict[str, Any]:
    return next(x for x in await rs.describe(db) if x["key"] == key)


@router.put("/{key}")
async def save(key: str, body: ValueIn, ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _spec(key)
    try:
        await rs.save(db, key, body.value, body.reason, ctx.user)
    except rs.Invalid as exc:
        raise HTTPException(422, str(exc)) from None
    return await _one(db, key)


@router.post("/{key}/reset")
async def reset(key: str, body: ResetIn, ctx: AuthContext = Depends(needs(P.ADMIN_SETTINGS)), db: AsyncSession = Depends(get_db, scope="function")) -> dict[str, Any]:
    _spec(key)
    await rs.reset(db, key, body.reason, ctx.user)
    return await _one(db, key)
