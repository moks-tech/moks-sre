"""Chunking for operational content. Markdown splits on headings and keeps fenced code together when
it fits, YAML on documents and top-level keys, code on blank lines. These are line-based heuristics,
not parsers: nothing here understands a language. A document with a known type (rag/doctypes.py) is split by
rag/structure.py instead, when structured chunking is on: alert rules per rule, manifests per resource, and so on.

Every character of the document lands in a chunk. A line too long for one chunk is split across
several, preferably at whitespace or punctuation. Offsets and line numbers refer to the stored
document, which has been redacted, so they locate the passage in what MOKS holds; redaction replaces
values within a line, so line numbers also match the source.

Bump PIPELINE_VERSION when chunking, redaction, extraction or stored metadata change: documents
processed by an older version are read again even if their content did not change.
"""
from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass, field

from ..config import get_settings

PIPELINE_VERSION = 4  # 4: document types, per-chunk metadata, structure-aware alert rules, manifests, OpenAPI, dashboards, logs, steps
# Code has its own version, so changing how code is split re-reads code only, not every runbook and manifest.
CODE_PIPELINE_VERSION = 5  # 5: document types and Terraform resources per chunk; 4: code splits at top-level definitions
# What a document read with structured chunking switched off goes through: the versions before it existed, so
# switching it either way re-reads what the other setting produced.
LEGACY_PIPELINE_VERSION, LEGACY_CODE_PIPELINE_VERSION = 3, 4
_structured: ContextVar[bool | None] = ContextVar("moks_rag_structured", default=None)  # an evaluation compares both
CHARS_PER_TOKEN = 4  # an estimate: no tokenizer is shipped, and models differ
MAX_CHUNKS = 3000

CODE_EXT = {".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".rb", ".rs", ".sh", ".bash", ".tf", ".hcl", ".sql",
            ".c", ".h", ".cpp", ".cs", ".php", ".scala", ".swift", ".mjs", ".cjs", ".groovy", ".dockerfile"}
MARKDOWN_EXT = {".md", ".markdown", ".mdx", ".rst", ".adoc", ".txt"}
YAML_EXT = {".yaml", ".yml"}
DATA_EXT = {".json", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env.example"}
_JS = re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function\*?\s+[A-Za-z_$]|class\s+[A-Za-z_$]|(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=|(?:interface|type|enum)\s+[A-Za-z_$])")
DEFINITION = {  # a top-level definition, at column 0: where a reader of the file would say a new part starts
    ".py": re.compile(r"^(?:async\s+def|def|class)\s+[A-Za-z_]"), ".go": re.compile(r"^(?:func|type)\s"),
    ".js": _JS, ".jsx": _JS, ".mjs": _JS, ".cjs": _JS, ".ts": _JS, ".tsx": _JS,
    ".sh": re.compile(r"^(?:function\s+[A-Za-z_][\w-]*|[A-Za-z_][\w-]*\s*\(\))"), ".bash": re.compile(r"^(?:function\s+[A-Za-z_][\w-]*|[A-Za-z_][\w-]*\s*\(\))"),
    ".tf": re.compile(r"^(?:resource|data|module|variable|output|provider|locals|terraform)\b"), ".hcl": re.compile(r"^[A-Za-z_][\w-]*(?:\s+\"[^\"]*\")*\s*\{"),
    "dockerfile": re.compile(r"(?i)^FROM\s"),
}
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
SAFE_BREAK = re.compile(r"[\s,;:)\]}>]")
LIST_START = re.compile(r"^\s{0,3}(?:\d{1,3}[.)]|[-*+]|\|)\s")  # a step, a bullet or a table row


@dataclass
class ChunkPiece:
    heading: str
    text: str
    start_line: int
    end_line: int
    start_offset: int = 0
    end_offset: int = 0
    meta: dict = field(default_factory=dict)  # what this piece is about: resource, alert, operation, section, steps

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(" ".join(self.text.split()).lower().encode()).hexdigest()[:16]


@dataclass
class _Span:
    """A whole line, or one part of a line too long for a chunk."""
    line: int  # 1-based
    start: int
    end: int
    fenced: bool = False
    boundary: bool = False  # a good place to start a new chunk


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def structured() -> bool:
    override = _structured.get()
    return get_settings().rag_structured_chunking if override is None else override


def use_structured(on: bool | None):
    """For an evaluation that compares both ways of splitting in one run. Returns a token for reset_structured."""
    return _structured.set(on)


def reset_structured(token) -> None:
    _structured.reset(token)


def limits() -> tuple[int, int]:
    s = get_settings()
    size = max(s.rag_chunk_tokens, 50) * CHARS_PER_TOKEN
    return size, min(max(s.rag_chunk_overlap_tokens, 0) * CHARS_PER_TOKEN, size // 3)


def kind_of(path: str) -> str:
    p = path.lower()
    if p.startswith(("http://", "https://")):  # web pages are stored as Markdown-like text, whatever the URL ends in
        return "markdown"
    name = p.rsplit("/", 1)[-1]
    if name in ("dockerfile", "makefile", "jenkinsfile") or name.startswith("dockerfile."):
        return "code"
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in MARKDOWN_EXT:
        return "markdown"
    if ext in YAML_EXT:
        return "yaml"
    if ext in CODE_EXT:
        return "code"
    if ext in DATA_EXT:
        return "data"
    return "text"


def version_for(path: str) -> int:
    if not structured():
        return LEGACY_CODE_PIPELINE_VERSION if kind_of(path) == "code" else LEGACY_PIPELINE_VERSION
    return CODE_PIPELINE_VERSION if kind_of(path) == "code" else PIPELINE_VERSION


def current_versions() -> tuple[int, int]:
    """(documents, code): what a document read now goes through."""
    return (PIPELINE_VERSION, CODE_PIPELINE_VERSION) if structured() else (LEGACY_PIPELINE_VERSION, LEGACY_CODE_PIPELINE_VERSION)


def _definitions(path: str) -> re.Pattern | None:
    name = path.lower().rsplit("/", 1)[-1]
    if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return DEFINITION["dockerfile"]
    return DEFINITION.get("." + name.rsplit(".", 1)[-1]) if "." in name else None


def title_of(path: str, text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else path.rsplit("/", 1)[-1])[:300]


def lifecycle_of(text: str) -> tuple[str, str | None]:
    """(current | deprecated | superseded, replacement) from Markdown front matter, when a document states it."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text[:4000], re.S)
    if not m:
        return "current", None
    head = m.group(1)
    by = re.search(r"(?im)^superseded[_-]by:\s*[\"']?([^\"'\n]+)", head)
    if by:
        return "superseded", by.group(1).strip()[:900]
    return ("deprecated" if re.search(r"(?im)^(status|state):\s*[\"']?(deprecated|obsolete|archived)\b", head) else "current"), None


def chunk(path: str, text: str, doc_type: str | None = None) -> list[ChunkPiece]:
    """Pieces of the stored text. With a document type and structured chunking on, each piece also says what it is about."""
    if not text.strip():
        return []
    k = kind_of(path)
    size, overlap = limits()
    out: list[ChunkPiece] | None = None
    if doc_type and structured():
        from . import structure

        out = structure.chunk(doc_type, path, text, size, overlap)
    if out is None:
        spans, sections = _spans(text, k, size, _definitions(path) if k == "code" else None)
        out = []
        for heading, a, b in sections:
            out.extend(_windows(text, spans[a:b], heading, size, overlap))
    if doc_type and structured():
        from . import structure

        for p in out:
            p.heading, p.meta = structure.annotate(doc_type, p.heading, p.text)
            p.heading = p.heading[:480]
    return out


def _spans(text: str, kind: str, size: int, definitions: re.Pattern | None = None, lists: bool = False) -> tuple[list[_Span], list[tuple[str, int, int]]]:
    """Lines split to fit, and the sections they fall in. In code, a top-level definition opens a section once the
    current one holds a third of a chunk; smaller ones share a section, and its heading names each of them. With
    `lists`, a Markdown chunk ends between list items (steps, timeline entries), not inside one."""
    spans: list[_Span] = []
    sections: list[tuple[str, int, int]] = []
    stack: list[tuple[int, str]] = []
    heading, section_start, offset, fenced = "", 0, 0, False
    section_chars = 0
    # Only newlines end a line. str.splitlines also breaks on form feeds and Unicode separators, which no editor or Git host counts.
    lines = re.findall(r"[^\n]*\n|[^\n]+$", text)
    for number, raw in enumerate(lines, 1):
        bare = raw.rstrip("\r\n")
        opens = kind == "markdown" and FENCE.match(bare)
        if kind == "markdown" and not fenced and not opens:
            m = HEADING.match(bare)
            if m:
                if len(spans) > section_start:
                    sections.append((heading, section_start, len(spans)))
                level = len(m.group(1))
                stack = [(lv, t) for lv, t in stack if lv < level] + [(level, m.group(2).strip())]
                heading, section_start = " > ".join(t for _, t in stack)[:480], len(spans)
        if kind == "yaml" and bare.strip() == "---":
            if len(spans) > section_start:
                sections.append((heading, section_start, len(spans)))
            heading, section_start = "", len(spans)
            offset += len(raw)
            continue
        if kind == "yaml" and bare.startswith("kind:"):
            heading = bare.split(":", 1)[1].strip()
        elif kind == "yaml" and re.match(r"^\s{2}name:", bare) and heading and " " not in heading:
            heading = f"{heading} {bare.split(':', 1)[1].strip()}"[:480]
        if definitions is not None and definitions.match(bare):
            label = bare.strip().rstrip("{:").strip()[:120]
            if section_chars >= size // 3 and len(spans) > section_start:
                sections.append((heading, section_start, len(spans)))
                heading, section_start, section_chars = label, len(spans), 0
            else:
                heading = f"{heading}; {label}"[:480] if heading else label
        section_chars += len(raw)
        if kind == "code":
            boundary = not bare.strip() or bool(definitions is not None and definitions.match(bare))
        elif kind in ("yaml", "data"):
            boundary = not bare.strip() or (not bare[:1].isspace() and bare[:1] not in "}]")
        elif lists:
            following = lines[number] if number < len(lines) else ""
            continued = bool(following.strip()) and following[:1].isspace() and not LIST_START.match(following)
            boundary = (not bare.strip() and not continued) or bool(LIST_START.match(bare))
        else:
            boundary = not bare.strip()
        for start, end in _split_line(raw, offset, size):
            spans.append(_Span(number, start, end, fenced or bool(opens), boundary and start == offset))
        if opens:
            fenced = not fenced
        offset += len(raw)
    sections.append((heading, section_start, len(spans)))
    return spans, [s for s in sections if s[2] > s[1]]


def _split_line(raw: str, offset: int, size: int) -> list[tuple[int, int]]:
    if len(raw) <= size:
        return [(offset, offset + len(raw))]
    out, i = [], 0
    while i < len(raw):
        end = min(i + size, len(raw))
        if end < len(raw):  # prefer a break in the last fifth, so identifiers stay whole when they can
            for j in range(end, i + size * 4 // 5, -1):
                if SAFE_BREAK.match(raw[j - 1]):
                    end = j
                    break
        out.append((offset + i, offset + end))
        i = end
    return out


def _windows(text: str, spans: list[_Span], heading: str, size: int, overlap: int, anywhere: bool = False) -> list[ChunkPiece]:
    out: list[ChunkPiece] = []
    start, n = 0, len(spans)
    while start < n:
        end, used = start, 0
        while end < n and (end == start or used + spans[end].end - spans[end].start <= size):
            used += spans[end].end - spans[end].start
            end += 1
        if end < n:
            end = _better_end(spans, start, end, anywhere)
        piece = _piece(text, spans[start:end], heading)
        if piece:
            out.append(piece)
        if end >= n:
            break
        back, carried = end, 0
        while back - 1 > start and carried + spans[back - 1].end - spans[back - 1].start <= overlap:
            back -= 1
            carried += spans[back].end - spans[back].start
        start = back
    return out


def _better_end(spans: list[_Span], start: int, end: int, anywhere: bool = False) -> int:
    """Back off within the second half of the window: leave a code fence whole, else stop at a boundary. With
    `anywhere`, any boundary after the first line will do: for a log, a short chunk beats an event cut in two."""
    half = start if anywhere else start + (end - start) // 2
    if spans[end].fenced and spans[end - 1].fenced:
        for j in range(end - 1, half, -1):
            if not spans[j].fenced:
                return j
    for j in range(end, half, -1):
        if spans[j].boundary:
            return j
    return end


def _piece(text: str, spans: list[_Span], heading: str) -> ChunkPiece | None:
    a, b = spans[0].start, spans[-1].end
    raw = text[a:b]
    body = raw.strip()
    if not body:
        return None
    skipped = raw[:len(raw) - len(raw.lstrip())]
    a += len(skipped)
    b = a + len(body)
    start = spans[0].line + skipped.count("\n")  # a citation points at the text, not at the blank lines before it
    return ChunkPiece(heading, body, start, start + body.count("\n"), a, b)
