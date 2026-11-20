"""Credentials an administrator pastes into MOKS (docs/CREDENTIALS.md).

A credential reference is one of:

    {"type": "stored", "id": "<uuid>"}      a value typed into Settings, kept encrypted in PostgreSQL (this module)
    {"type": "env", "name": "MOKS_CRED_X"}  an environment variable, from a Kubernetes Secret
    {"type": "file", "path": "prom/token"}  a file under MOKS_CREDENTIALS_DIR

Stored values are encrypted with AES-256-GCM. The key is derived with HKDF from MOKS_CREDENTIALS_KEY, or from
MOKS_SESSION_SECRET when that is not set, so a deployment needs no new secret. Each row records which key sealed it
(key_id), and the row id is the associated data, so a ciphertext copied onto another row does not open.
MOKS_CREDENTIALS_KEY_PREVIOUS and the session-derived key still open older rows after a key change; reencrypt()
moves them to the current key.

Values leave this module only through reveal(), which connectors and providers call when they connect. They are not
returned by the API, logged, audited, traced or put in prompts. Each process keeps the encrypted rows in memory and
refreshes them from the database every few seconds, on API requests and when it claims work; that is what lets
connectors resolve a credential synchronously.

At API start-up import_on_start() copies the MOKS_CRED_* values that references name into stored credentials, so
an existing deployment moves off its Kubernetes Secret without a manual step.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings

log = logging.getLogger("moks.credentials")

# Credentials the deployment itself uses, not a connector or provider. Each is found by its variable name: a stored
# credential with that label, else the environment variable.
DEPLOYMENT = {"MOKS_CRED_PINECONE_API_KEY": "Pinecone API key, for the pinecone knowledge backend"}

MAX_BYTES = 65_536          # a kubeconfig or a PEM bundle fits; anything larger is not a credential
REFRESH_SECONDS = 2.0       # the most often one process asks the database whether anything changed
INFO = b"moks-credentials-v1"


class CredentialUnavailable(Exception):
    """The stored credential cannot be used; the message names it, never its value."""


# ---- keys -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    id: str
    source: str  # the setting it comes from, for the Settings page
    aead: AESGCM


def _derive(root: str, source: str) -> Key:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(root.encode())
    return Key(hashlib.sha256(b"moks-credentials-key-id:" + raw).hexdigest()[:16], source, AESGCM(raw))


def keys() -> list[Key]:
    """Every key that may open a row, the one new rows are sealed with first."""
    s = get_settings()
    out: list[Key] = []
    if s.credentials_key:
        out.append(_derive(s.credentials_key, "MOKS_CREDENTIALS_KEY"))
    if s.credentials_key_previous:
        out.append(_derive(s.credentials_key_previous, "MOKS_CREDENTIALS_KEY_PREVIOUS"))
    out.append(_derive(s.session_secret, "MOKS_SESSION_SECRET"))
    seen: set[str] = set()
    return [k for k in out if not (k.id in seen or seen.add(k.id))]


def current_key() -> Key:
    return keys()[0]


def _aad(cid: uuid.UUID) -> bytes:
    return f"moks-credential:{cid}".encode()


def seal(cid: uuid.UUID, value: str) -> tuple[str, bytes, bytes]:
    key = current_key()
    nonce = os.urandom(12)
    return key.id, nonce, key.aead.encrypt(nonce, value.encode(), _aad(cid))


def _open(cid: uuid.UUID, key_id: str, nonce: bytes, ciphertext: bytes) -> str:
    for key in keys():
        if key.id == key_id:
            try:
                return key.aead.decrypt(nonce, ciphertext, _aad(cid)).decode()
            except InvalidTag:
                break
    raise CredentialUnavailable(f"stored credential {cid} was saved under an encryption key this deployment does not have; enter it again in Settings > Credentials")


def check_value(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("Enter the value.")
    if len(value.encode()) > MAX_BYTES:
        raise ValueError(f"A credential is at most {MAX_BYTES // 1024} KiB.")
    if "\x00" in value:
        raise ValueError("The value contains a NUL character.")
    return value


# ---- the in-process copy ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    label: str
    key_id: str
    nonce: bytes
    ciphertext: bytes
    version: int


_rows: dict[uuid.UUID, _Row] = {}
_stamp: tuple[Any, ...] | None = None
_checked = 0.0
_lock = asyncio.Lock()


def _ref_id(ref: dict[str, Any] | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str((ref or {}).get("id")))
    except (TypeError, ValueError):
        return None


def reveal(ref: dict[str, Any]) -> str:
    """The value of a stored reference. Raises CredentialUnavailable with a message fit for the person configuring it."""
    cid = _ref_id(ref)
    if cid is None:
        raise CredentialUnavailable("a stored credential reference needs an id")
    row = _rows.get(cid)
    if row is None:
        raise CredentialUnavailable(f"stored credential {cid} does not exist (deleted, or saved a moment ago and not loaded here yet)")
    return _open(cid, row.key_id, row.nonce, row.ciphertext)


def named(name: str) -> uuid.UUID | None:
    """The stored credential labelled with this variable name (the most recently changed, if several)."""
    found = [(row.version, cid) for cid, row in _rows.items() if row.label == name]
    return max(found, key=lambda f: f[0])[1] if found else None


def deployment_value(name: str) -> str | None:
    """A deployment credential (DEPLOYMENT): the stored value labelled `name`, else the environment variable."""
    cid = named(name)
    if cid is not None:
        return reveal({"type": "stored", "id": str(cid)}).strip()
    value = os.environ.get(name)
    return value.strip() if value else None


def version(ref: dict[str, Any] | None) -> int | None:
    """Changes whenever the value does, so a cached client built with the old value is rebuilt."""
    cid = _ref_id(ref)
    row = _rows.get(cid) if cid else None
    return row.version if row else None


def known(ref: dict[str, Any] | None) -> bool:
    cid = _ref_id(ref)
    return cid is not None and cid in _rows


async def refresh(db: AsyncSession | None = None, force: bool = False) -> None:
    """Reloads the encrypted rows when the table changed. Cheap and throttled; never raises: if the table cannot be
    read, this process keeps what it had."""
    global _checked
    if not force and time.monotonic() - _checked < REFRESH_SECONDS:
        return
    async with _lock:
        if not force and time.monotonic() - _checked < REFRESH_SECONDS:
            return
        try:
            if db is not None:
                await _load(db)
            else:
                from .db import session_scope

                async with session_scope() as own:
                    await _load(own)
        except Exception as exc:
            log.warning("stored credentials unreadable", extra={"event": "stored_credentials_unreadable", "errorType": type(exc).__name__})
        finally:
            _checked = time.monotonic()


async def _load(db: AsyncSession) -> None:
    global _stamp, _rows
    stamp = tuple((await db.execute(text("SELECT count(*), max(updated_at), sum(version) FROM stored_credentials"))).one())
    if stamp == _stamp:
        return
    rows = (await db.execute(text("SELECT id, label, key_id, nonce, ciphertext, version FROM stored_credentials"))).all()
    _rows = {r[0]: _Row(r[1], r[2], bytes(r[3]), bytes(r[4]), r[5]) for r in rows}
    _stamp = stamp


def forget() -> None:
    """Tests: drop the in-process copy."""
    global _stamp, _rows, _checked
    _rows, _stamp, _checked = {}, None, 0.0


# ---- writes (API only) ----------------------------------------------------------------------------------------------


async def store(db: AsyncSession, *, label: str, value: str, actor: uuid.UUID | None) -> uuid.UUID:
    from .models import StoredCredential

    check_value(value)
    cid = uuid.uuid4()
    key_id, nonce, ct = seal(cid, value)
    db.add(StoredCredential(id=cid, label=label[:200], key_id=key_id, nonce=nonce, ciphertext=ct, version=1, created_by=actor, updated_by=actor))
    await db.flush()
    await refresh(db, force=True)
    return cid


async def replace(db: AsyncSession, cid: uuid.UUID, *, value: str, actor: uuid.UUID | None, label: str | None = None) -> None:
    from .models import StoredCredential

    check_value(value)
    row = await db.get(StoredCredential, cid)
    if row is None:
        raise KeyError(cid)
    row.key_id, row.nonce, row.ciphertext = seal(cid, value)
    row.version += 1
    row.updated_by, row.updated_at = actor, datetime.now(timezone.utc)
    if label:
        row.label = label[:200]
    await db.flush()
    await refresh(db, force=True)


async def reencrypt(db: AsyncSession) -> dict[str, int]:
    """Moves every row that opens with an older key onto the current one. A row no key opens is left as it is."""
    from .models import StoredCredential

    cur = current_key()
    moved = unreadable = 0
    for row in (await db.execute(select(StoredCredential).where(StoredCredential.key_id != cur.id))).scalars():
        try:
            value = _open(row.id, row.key_id, bytes(row.nonce), bytes(row.ciphertext))
        except CredentialUnavailable:
            unreadable += 1
            continue
        row.key_id, row.nonce, row.ciphertext = seal(row.id, value)
        row.version += 1
        moved += 1
    await db.flush()
    await refresh(db, force=True)
    return {"reencrypted": moved, "unreadable": unreadable}


# ---- where references live ------------------------------------------------------------------------------------------


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every credential reference inside a JSON value, with where it is."""
    if isinstance(value, dict):
        if value.get("type") in ("stored", "env", "file") and set(value) <= {"type", "id", "name", "path"}:
            yield path, value
            return
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


async def holders(db: AsyncSession) -> list[tuple[Any, str, str, list[tuple[str, dict[str, Any]]]]]:
    """(row, kind, label, [(path, ref)]) for every row that holds credential references: connectors (including
    config.credentials and config.executor_credential_ref), LLM providers, knowledge sources and backup storage."""
    from .models import BackupStore, Connector, Environment, KnowledgeSource, LLMConfig

    envs = dict((await db.execute(select(Environment.id, Environment.name))).all())
    out: list[tuple[Any, str, str, list[tuple[str, dict[str, Any]]]]] = []
    for c in (await db.execute(select(Connector))).scalars():
        refs = list(_walk(c.credential_ref, "credential_ref")) + list(_walk(c.config or {}, "config"))
        out.append((c, "connector", f"Connector {envs.get(c.environment_id, '?')}/{c.name}", refs))
    for llm in (await db.execute(select(LLMConfig))).scalars():
        out.append((llm, "llm", f"LLM provider {llm.name}", list(_walk(llm.credential_ref, "credential_ref"))))
    for k in (await db.execute(select(KnowledgeSource))).scalars():
        out.append((k, "knowledge", f"Knowledge source {k.name}", list(_walk(k.credential_ref, "credential_ref"))))
    for b in (await db.execute(select(BackupStore))).scalars():
        out.append((b, "backup", "Backup storage", list(_walk(b.credentials or {}, "credentials"))))
    return out


def executor_only(row: Any, kind: str, path: str) -> bool:
    """References the executor resolves from its own Secret, kept from the API on purpose (docs/SECRETS_SYNC.md):
    secret sync's AWS keys and a connector's executor credential. The API not having their variable is expected, so
    they are never reported as waiting for a value, and a value is never pasted over them from Settings."""
    return kind == "connector" and (path.startswith("config.executor_credential_ref")
                                    or (getattr(row, "type", None) == "aws_secrets" and path.startswith("config.credentials")))


async def usage(db: AsyncSession) -> dict[uuid.UUID, list[str]]:
    used: dict[uuid.UUID, list[str]] = {}
    for name in DEPLOYMENT:
        cid = named(name)
        if cid:
            used.setdefault(cid, []).append(f"Deployment ({name})")
    for _, _, label, refs in await holders(db):
        for _, ref in refs:
            cid = _ref_id(ref) if ref.get("type") == "stored" else None
            if cid:
                used.setdefault(cid, []).append(label)
    return used


def _set_path(root: Any, path: str, ref: dict[str, Any]) -> Any:
    """Replaces the reference at `path` (as _walk names it) inside a copy of root; returns the copy."""
    import copy
    import re

    out = copy.deepcopy(root)
    parts = [p for p in re.split(r"\.|\[(\d+)\]", path) if p]
    if not parts:
        return ref
    node = out
    for p in parts[:-1]:
        node = node[int(p)] if isinstance(node, list) else node[p]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = ref
    else:
        node[last] = ref
    return out


async def import_environment(db: AsyncSession, actor: uuid.UUID | None, given: dict[str, str] | None = None) -> dict[str, Any]:
    """Stores the value of every MOKS_CRED_* variable a reference names and points the reference at the stored copy.
    One stored credential per variable, shared by every reference to it. Variables this process does not have are
    listed and left as they are.

    With `given` ({variable: value}), only those variables, with values an administrator pasted: how a reference to a
    variable the deployment never had gets its value. A stored copy with that label takes the new value."""
    from .models import StoredCredential

    stored: dict[str, uuid.UUID] = {}
    existing = {r.label: r.id for r in (await db.execute(select(StoredCredential).order_by(StoredCredential.updated_at))).scalars()}
    missing: set[str] = set()
    changed: list[str] = []

    async def copy_of(name: str, value: str) -> uuid.UUID:
        if name not in stored:
            cid = existing.get(name)
            if cid is None:
                cid = await store(db, label=name, value=value, actor=actor)
            elif given is not None:
                await replace(db, cid, value=value, actor=actor)
            stored[name] = cid
        return stored[name]

    for row, kind, label, refs in await holders(db):
        for path, ref in refs:
            if ref.get("type") != "env":
                continue
            name = str(ref.get("name") or "")
            if given is not None and (name not in given or executor_only(row, kind, path)):
                continue
            value = given[name] if given is not None else os.environ.get(name) if name.startswith("MOKS_CRED_") else None
            if not value or not value.strip():
                if not executor_only(row, kind, path):
                    missing.add(name)
                continue
            new = {"type": "stored", "id": str(await copy_of(name, value))}
            column = path.split(".", 1)[0].split("[", 1)[0]
            inner = path[len(column):].lstrip(".")
            setattr(row, column, _set_path(getattr(row, column), inner, new))
            changed.append(f"{label}: {path}")
    for name in DEPLOYMENT if given is None else ():
        value = os.environ.get(name)
        if value and value.strip():
            await copy_of(name, value)
    await db.flush()
    return {"stored": sorted(stored), "references": changed, "missing": sorted(n for n in missing if n)}


IMPORT_LOCK = "credentials:import-environment"


async def import_on_start() -> dict[str, Any] | None:
    """API start-up (credentials_import_on_start): import_environment without anyone clicking Import, so connectors,
    providers, knowledge sources and backup storage stop reading the Kubernetes Secret on the first deploy. One
    replica does it at a time (advisory lock) and a second run finds nothing to do. References to variables this
    process does not have stay as they are; their forms ask for the value. Never raises."""
    from . import audit
    from .db import session_scope

    if not get_settings().credentials_import_on_start:
        return None
    try:
        async with session_scope() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": IMPORT_LOCK})
            out = await import_environment(db, None)
            if out["references"] or out["stored"]:
                await audit.record(db, action="credential.import_environment", resource_type="stored_credential", decision="allow",
                                   details={"automatic": True, "variables": out["stored"], "references": len(out["references"]), "missing": out["missing"]})
        if out["references"] or out["missing"]:
            log.info("credentials imported from the environment", extra={"event": "credentials_imported", "references": len(out["references"]),
                                                                         "missing": out["missing"]})
        return out
    except Exception as exc:
        log.warning("credential import at start-up failed", extra={"event": "credentials_import_failed", "errorType": type(exc).__name__})
        return None


async def describe(db: AsyncSession) -> dict[str, Any]:
    from .models import StoredCredential, User

    rows = (await db.execute(select(StoredCredential).order_by(StoredCredential.label))).scalars().all()
    used = await usage(db)
    ids = {u for r in rows for u in (r.created_by, r.updated_by) if u}
    emails = dict((await db.execute(select(User.id, User.email).where(User.id.in_(ids)))).all()) if ids else {}
    cur = current_key()

    def state(r: StoredCredential) -> str:
        try:
            _open(r.id, r.key_id, bytes(r.nonce), bytes(r.ciphertext))  # checked, then dropped
        except CredentialUnavailable:
            return "unreadable"
        return "current" if r.key_id == cur.id else "older_key"
    env_refs: dict[str, list[str]] = {}
    for row, kind, label, refs in await holders(db):
        for path, ref in refs:
            if ref.get("type") == "env" and ref.get("name"):
                if executor_only(row, kind, path) and not (os.environ.get(str(ref["name"])) or "").strip():
                    continue  # in the executor's Secret, as intended
                env_refs.setdefault(str(ref["name"]), []).append(label)
    return {
        "key": {"source": cur.source, "id": cur.id},
        "credentials": [{"id": str(r.id), "label": r.label, "used_by": used.get(r.id, []),
                         "state": state(r),
                         "created_at": r.created_at.isoformat() if r.created_at else None, "created_by": emails.get(r.created_by),
                         "updated_at": r.updated_at.isoformat() if r.updated_at else None, "updated_by": emails.get(r.updated_by)} for r in rows],
        "environment_references": [{"name": n, "set_here": bool((os.environ.get(n) or "").strip()), "used_by": sorted(set(env_refs[n]))}
                                   for n in sorted(env_refs)],
        "deployment": [{"name": n, "help": h, "source": "stored" if named(n) else "environment" if (os.environ.get(n) or "").strip() else "unset",
                        "id": str(named(n)) if named(n) else None} for n, h in DEPLOYMENT.items()],
        "count": int((await db.execute(select(func.count()).select_from(StoredCredential))).scalar_one()),
    }

