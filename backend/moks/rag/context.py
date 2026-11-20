"""Context assembly: retrieved passages into the block a model reads, within a budget.

Search decides what may be read and in what order. This decides how it is laid out, and changes neither:

  1. drop repeats: the same chunk twice, or the same words from two documents in one scope (the first is kept)
  2. keep current guidance ahead of superseded or deprecated documents, unless history is what was asked for
  3. group passages by document, in the order the documents first appear, and within one document in the
     order of the text, joining chunks that overlap or touch, so a procedure reads as one piece
  4. label each block: document type, freshness, what replaced it, a possible conflict, a historical record
  5. stop at the budget: passages past it are reported as dropped, never cut silently

Passages are untrusted text. The caller frames them (orchestrator/prompts.untrusted); nothing here lets a
passage's words change structure, labels or order. Token counts are estimates at 4 characters a token.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .chunking import CHARS_PER_TOKEN
from .doctypes import HISTORICAL

NEAR_DUPLICATE = 0.85
RETIRED = ("superseded", "deprecated")
TOUCHING = 8  # characters between two chunks that still read as one: only whitespace lies between them
CONTEXT_CHARS = 600  # room a neighbouring chunk adds to the span it joins
_WORD = re.compile(r"[a-z0-9]{3,}")


def _shingles(text: str) -> set[str]:
    words = _WORD.findall(text.lower())
    return {" ".join(words[i:i + 3]) for i in range(max(len(words) - 2, 1))}


def _similar(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= NEAR_DUPLICATE


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    return text[:cut].rstrip() + "\n[...]", True


def _scope(p: dict) -> tuple:
    s = p.get("scope") or {}
    return (s.get("environment"), s.get("namespace"))


def _join(parts: list[dict]) -> list[dict]:
    """Chunks of one document in text order; those that overlap or touch become one span. Offsets refer to the same
    stored text, so the overlap is removed exactly. Context chunks (search's neighbours and sections) join a span
    without an id of their own. Passages without offsets stay as they are."""
    with_offsets = sorted((p for p in parts if (p.get("citation") or {}).get("offsets")), key=lambda p: p["citation"]["offsets"][0])
    spans: list[dict] = []
    for p in with_offsets:
        a, b = p["citation"]["offsets"]
        last = spans[-1] if spans else None
        if last and a <= last["end"] + TOUCHING:
            if b > last["end"]:
                gap = a - last["end"]  # negative: they overlap; positive: whitespace the chunks were trimmed of
                last["text"] += p["text"][-gap:] if gap < 0 else ("\n\n" if gap > 1 else "\n" if gap == 1 else "") + p["text"]
                last["end"] = b
                last["lines"][1] = max(last["lines"][1] or 0, (p["citation"].get("lines") or [0, 0])[1] or 0)
        else:
            last = {"start": a, "end": b, "text": p["text"], "lines": list(p["citation"].get("lines") or [None, None]), "ids": [], "headings": [], "context": 0, "for": None}
            spans.append(last)
        if p.get("_context"):
            last["context"] += 1
            last["for"] = last["for"] or p["_for"]
        else:
            last["ids"].append(p["_id"])
            last["headings"].append(p.get("heading") or "")
    for p in parts:
        if not (p.get("citation") or {}).get("offsets") and not p.get("_context"):
            spans.append({"start": None, "end": None, "text": p["text"], "lines": list((p.get("citation") or {}).get("lines") or [None, None]), "ids": [p["_id"]],
                          "headings": [p.get("heading") or ""], "context": 0, "for": None})
    for span in spans:  # context that joined no hit still says whose context it is
        span["headings"] = span["headings"] or [""]
    return spans


def assemble(passages: list[dict], *, total_chars: int = 9000, passage_chars: int = 1200, history: bool = False,
             id_of: Callable[[dict], str] | None = None) -> dict[str, Any]:
    """`passages` are search results (or passages shaped like them), best first. `history` keeps superseded and
    historical documents in rank order, for questions about what happened before. `id_of` names a passage for
    citation (an investigation's E3.2); by default its rank, 1-based."""
    id_of = id_of or (lambda p: str(p.get("_rank")))
    dropped: list[dict] = []
    kept: list[dict] = []
    seen_chunks: set = set()
    seen_text: dict[tuple, list[tuple[set[str], str, str]]] = {}
    for rank, raw in enumerate(passages, 1):
        p = dict(raw)
        p["_rank"] = rank
        p["_id"] = id_of(p)
        if p.get("chunk_id") in seen_chunks:
            dropped.append({"id": p["_id"], "reason": "the same passage was already included"})
            continue
        seen_chunks.add(p.get("chunk_id"))
        sh = _shingles(p.get("text") or "")
        twins = seen_text.setdefault(_scope(p), [])
        twin = next((pid for other, pid, doc in twins if doc != p.get("document_id") and _similar(sh, other)), None)
        if twin:
            dropped.append({"id": p["_id"], "reason": f"the same text as {twin}, from another document", "duplicate_of": twin})
            continue
        twins.append((sh, p["_id"], p.get("document_id")))
        kept.append(p)
    if not history:  # current guidance first; what replaced a document is named on it
        kept = [p for p in kept if (p.get("freshness") or {}).get("state") not in RETIRED] + [p for p in kept if (p.get("freshness") or {}).get("state") in RETIRED]
    order: list[str] = []
    by_doc: dict[str, list[dict]] = {}
    for p in kept:
        key = p.get("document_id") or p["_id"]
        if key not in by_doc:
            order.append(key)
        by_doc.setdefault(key, []).append(p)
    taken = {p.get("chunk_id") for p in kept}
    for p in kept:  # what search attached around a hit: the same document, so the same access decision
        for c in p.get("context") or []:
            if c.get("offsets") and c.get("chunk_id") not in taken:
                taken.add(c.get("chunk_id"))
                by_doc[p.get("document_id") or p["_id"]].append({"text": c["text"], "heading": c.get("heading"), "citation": {"offsets": c["offsets"], "lines": c.get("lines")},
                                                                  "_context": True, "_for": p["_id"]})
    blocks: list[dict] = []
    used = 0
    for key in order:
        parts = by_doc[key]
        first = parts[0]
        fr = first.get("freshness") or {}
        cit = first.get("citation") or {}
        labels = ([first["label"]] if first.get("label") else []) + [first.get("doc_type") or "document"]
        if fr.get("state") and fr["state"] != "current":
            labels.append(fr["state"] + (f": {fr['reason']}" if fr.get("reason") else ""))
        if first.get("doc_type") in HISTORICAL or fr.get("historical"):
            labels.append("historical record: what happened then, not how things work now")
        if fr.get("possible_conflict"):
            labels.append("may conflict with " + ", ".join(fr.get("conflict_with") or []) if fr.get("conflict_with") else "may conflict with another document")
        if fr.get("trust") == "unreviewed":
            labels.append("unreviewed")
        for span in _join(parts):
            text, clipped = _clip(span["text"], passage_chars * max(1, len(span["ids"])) + CONTEXT_CHARS * span["context"] if span["ids"] else passage_chars // 2)
            cost = len(text) + 200  # the header and frame around it
            if used + cost > total_chars:
                dropped += [{"id": i, "reason": "past the context budget"} for i in span["ids"]]
                continue
            used += cost
            heading = next((h for h in span["headings"] if h), "")
            blocks.append({"ids": span["ids"], "context_for": None if span["ids"] else span["for"], "context_chunks": span["context"], "document_id": first.get("document_id"), "title": first.get("title"), "doc_type": first.get("doc_type"), "heading": heading,
                           "path": cit.get("path"), "repository": cit.get("repository") or cit.get("source"), "revision": cit.get("revision"), "lines": span["lines"],
                           "url": cit.get("url"), "labels": labels, "text": text, "clipped": clipped, "freshness": fr.get("state") or "current"})
    shown = {b["document_id"] for b in blocks}
    conflicts = sorted({tuple(sorted({(by_doc[k][0].get("citation") or {}).get("path") or "", other})) for k in order if by_doc[k][0].get("document_id") in shown
                        for other in ((by_doc[k][0].get("freshness") or {}).get("conflict_with") or [])})
    return {"blocks": blocks, "dropped": dropped, "chars": used, "tokens": used // CHARS_PER_TOKEN, "conflicts": [list(c) for c in conflicts],
            "budget": {"total_chars": total_chars, "passage_chars": passage_chars}, "tokens_basis": "estimated at 4 characters a token"}


def render(assembled: dict, frame: Callable[[str, str, int], str], passage_chars: int = 1200) -> str:
    """One block per document span: who it is, where it is, what it is labelled, then the framed text."""
    out = []
    for b in assembled["blocks"]:
        where = f"{b.get('repository') or ''}:{b.get('path')}" + (f"@{str(b['revision'])[:12]}" if b.get("revision") else "")
        lines = b.get("lines") or [None, None]
        ids = " ".join(f"[{i}]" for i in b["ids"]) or f"(more of the document cited as [{b.get('context_for')}])"
        head = f"{ids} {'; '.join(b['labels'])}; {where}" + (f" lines {lines[0]}-{lines[1]}" if lines[0] else "")
        if b.get("heading"):
            head += f"\nSection: {b['heading']}"
        out.append(head + "\n" + frame(f"document {b.get('path')}", b["text"], passage_chars + 20))
    return "\n\n".join(out)


def baseline(passages: list[dict], *, total_chars: int = 9000, passage_chars: int = 1200) -> dict[str, Any]:
    """How context was laid out before assembly existed: results in rank order, each clipped, until the budget is
    spent. Kept so an evaluation can compare the two on the same retrieval."""
    blocks, used, dropped = [], 0, []
    for rank, p in enumerate(passages, 1):
        text = (p.get("text") or "")[:passage_chars]
        if used + len(text) + 200 > total_chars:
            dropped.append({"id": str(rank), "reason": "past the context budget"})
            continue
        used += len(text) + 200
        blocks.append({"ids": [str(rank)], "document_id": p.get("document_id"), "doc_type": p.get("doc_type"), "text": text, "path": (p.get("citation") or {}).get("path"),
                       "lines": (p.get("citation") or {}).get("lines"), "labels": [], "freshness": (p.get("freshness") or {}).get("state") or "current"})
    return {"blocks": blocks, "dropped": dropped, "chars": used, "tokens": used // CHARS_PER_TOKEN, "conflicts": []}
