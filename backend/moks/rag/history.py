"""Investigation history: finished MOKS investigations as searchable knowledge, so "find similar OOMKilled
incidents" can reach what MOKS itself found before, not only approved postmortems. Off unless
MOKS_RAG_INDEX_INVESTIGATIONS is on (Settings > Knowledge search).

Access is the investigation's own. A history document carries the investigation's environment and namespace, and
knowledge reads a document under the same single-binding rule investigations are read by. That only holds while
every role that may read knowledge may also read investigations, so a sync checks it first and refuses otherwise.
An investigation of every namespace ("*") is never indexed: on a document "*" means unrestricted, which is the
opposite of what it means on an investigation.

What is left out, and removed when it changes: unfinished or failed runs, evaluation runs, anything older than
MOKS_RAG_HISTORY_DAYS, and any run whose conversation or question was deleted. Deleting a conversation or a
question removes its history at once; a sync also removes it, from a complete listing, every time.

A history document is a historical record: what MOKS saw and suspected then, labelled so a reader never takes a
suspected cause for a verified one or an old state for the current one. It starts unreviewed, and a reviewer can
quarantine it like any other document.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..models import Conversation, ConversationMessage, Document, Environment, Investigation, KnowledgeSource
from ..rbac import ROLES, P
from . import service

log = logging.getLogger("moks.rag.history")
SOURCE_NAME = "investigation-history"
MAX_DOCUMENTS = 500  # the newest finished investigations; older ones leave as newer ones arrive
SYNC_MINUTES = 60
GLOB = "*?["


def access_matches() -> bool:
    """Every role that may read knowledge may read investigations, so history cannot reach anyone the investigation could not."""
    return all(P.INVESTIGATION_READ in perms for perms in ROLES.values() if P.KNOWLEDGE_READ in perms)


def path_of(investigation_id: uuid.UUID) -> str:
    return f"investigations/{investigation_id}.md"


async def source(db: AsyncSession, create: bool = True) -> KnowledgeSource | None:
    src = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name == SOURCE_NAME))).scalar_one_or_none()
    if src is None and create:
        src = KnowledgeSource(name=SOURCE_NAME, type="investigation", config={}, owner="MOKS", authority=-1, sync_interval_minutes=SYNC_MINUTES,
                              classification="internal")
        db.add(src)
        await db.flush()
    return src


def _lines(items: list, render) -> list[str]:
    return [f"- {render(x)}" for x in items if x][:20]


def render(inv: Investigation, env_name: str) -> str:
    rep = inv.report or {}
    when = inv.created_at.strftime("%Y-%m-%d %H:%M UTC") if inv.created_at else "unknown time"
    out = [f"# Investigation: {' '.join(inv.question.split())[:300]}", "",
           f"Environment {env_name}, namespace {inv.namespace}, {when}. A past MOKS investigation, kept as a historical record: it says what was seen "
           f"and suspected then, not what is true now. Suspected causes were not verified unless marked verified."]
    if rep.get("summary"):
        out += ["", "## Summary", " ".join(str(rep["summary"]).split())[:2000]]
    findings = rep.get("findings") or []
    if findings:
        out += ["", "## Findings"] + _lines(findings, lambda f: f"[{f.get('severity', 'info')}] {f.get('kind', '')}: {' '.join(str(f.get('title', '')).split())[:300]}"
                                            + (f" ({', '.join(map(str, (f.get('resources') or [])[:4]))})" if f.get("resources") else ""))
    causes = rep.get("suspected_causes") or []
    if causes:
        out += ["", "## Suspected causes"] + _lines(causes, lambda c: f"{' '.join(str(c.get('cause', '')).split())[:500]} (confidence {c.get('confidence', 'low')}"
                                                     + (", verified" if c.get("verified") else ", not verified") + ")")
    verified = rep.get("verified_causes") or []
    if verified:
        out += ["", "## Verified causes"] + _lines(verified, lambda v: " ".join(str(v.get("cause") or v.get("check") or v).split())[:500])
    facts = rep.get("observed_facts") or []
    if facts:
        out += ["", "## Observed facts"] + _lines(facts, lambda f: " ".join(str(f.get("statement", "")).split())[:500])
    steps = rep.get("next_steps") or []
    if steps:
        out += ["", "## Next steps"] + _lines(steps, lambda s: " ".join(str(s).split())[:500])
    return "\n".join(out) + "\n"


def _eligible(now: datetime):
    """Finished, real, recent, scoped to one namespace, and still visible where it was asked."""
    days = max(1, min(get_settings().rag_history_days, 3650))
    deleted_question = exists().where(ConversationMessage.investigation_id == Investigation.id, ConversationMessage.role == "user", ConversationMessage.deleted_at.is_not(None))
    return (select(Investigation, Environment.name).join(Environment, Environment.id == Investigation.environment_id)
            .outerjoin(Conversation, Conversation.id == Investigation.conversation_id)
            .where(Investigation.status == "completed", Investigation.report.is_not(None), Investigation.trigger != "eval",
                   Investigation.created_at >= now - timedelta(days=days), Investigation.namespace != "*",
                   *[~Investigation.namespace.contains(ch) for ch in GLOB],
                   or_(Investigation.conversation_id.is_(None), and_(Conversation.id.is_not(None), Conversation.deleted_at.is_(None))), ~deleted_question)
            .order_by(Investigation.created_at.desc()).limit(MAX_DOCUMENTS))


async def sync(db: AsyncSession, src: KnowledgeSource) -> dict[str, Any]:
    """One complete pass: what is eligible is written (unchanged ones are skipped by their hash), and everything else
    the source holds is removed. Switched off, the pass removes everything."""
    counts = dict.fromkeys(("added", "updated", "unchanged", "deleted", "excluded", "failed"), 0)
    enabled = get_settings().rag_index_investigations
    if enabled and not access_matches():
        raise ConnectorError("config", "a role can read knowledge but not investigations, so investigation history would widen access; it is not indexed")
    existing = {d.path: d for d in (await db.execute(select(Document).where(Document.source_id == src.id))).scalars()}
    wanted: set[str] = set()
    base = get_settings().public_url.rstrip("/")
    if enabled:
        for inv, env_name in (await db.execute(_eligible(datetime.now(timezone.utc)))).all():
            path = path_of(inv.id)
            wanted.add(path)
            before = existing.get(path)
            doc = await service.index_document(db, src, path, render(inv, env_name), revision=None, url=f"{base}/investigations/{inv.id}",
                                               extra={"environment": env_name, "namespace": inv.namespace, "title": f"Investigation: {' '.join(inv.question.split())[:200]}",
                                                      "owner": "MOKS", "doc_type": "investigation"})
            counts["excluded" if doc.status == "excluded" else "unchanged" if before is not None and before.content_hash == doc.content_hash and before.status == "active"
                   else "updated" if before is not None else "added"] += 1
    for path, doc in existing.items():
        if path not in wanted and doc.status != "deleted":
            await service.remove_document(db, doc)
            counts["deleted"] += 1
    await service.mark_indexed_now(db, src, "ok" if enabled else "disabled", None if enabled else "investigation history is switched off (MOKS_RAG_INDEX_INVESTIGATIONS)")
    await db.commit()
    return {**counts, "listing": "complete", "content": "complete", "complete": True, "discovered": len(wanted), "processed": len(wanted), "remaining": 0, "enabled": enabled}


async def forget(db: AsyncSession, *, conversation_id: uuid.UUID | None = None, investigation_id: uuid.UUID | None = None) -> int:
    """Removes the history of a conversation or of one question when it is deleted, in the same transaction, so it
    stops being searchable at commit rather than at the next sync."""
    src = await source(db, create=False)
    if src is None:
        return 0
    ids: list[uuid.UUID] = [investigation_id] if investigation_id else []
    if conversation_id:
        ids += (await db.execute(select(Investigation.id).where(Investigation.conversation_id == conversation_id))).scalars().all()
    if not ids:
        return 0
    docs = (await db.execute(select(Document).where(Document.source_id == src.id, Document.path.in_([path_of(i) for i in ids]), Document.status != "deleted"))).scalars().all()
    for doc in docs:
        await service.remove_document(db, doc)
    return len(docs)
