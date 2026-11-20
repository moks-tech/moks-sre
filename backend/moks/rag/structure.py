"""Structure-aware splitting for operational documents, by document type (rag/doctypes.py).

Each segmenter only decides where sections start, what they are called and where a chunk may end. The window,
size and overlap rules are chunking.py's, so every character still lands in a chunk and a citation's offsets
and lines still point at exactly the stored text. Like the rest of chunking these are line rules, not parsers:
a file they cannot read the shape of is split the way it was before types existed.

  alert rules    one section per alerting or recording rule, headed by the rule and its group
  manifests      one section per YAML document, headed "Kind namespace/name"; breaks at keys and list items
  OpenAPI        one section per operation ("GET /pets") and per schema
  dashboards     rendered to text first: one section per panel, with its queries, unit and thresholds
  logs           a chunk ends only where an event starts, so a stack trace stays with its event
  runbooks,      Markdown headings, and a chunk ends between steps or timeline entries, not inside one;
  postmortems    each section is labelled (root cause, timeline, mitigation, ...)

Each piece also gets `meta`: what it is about (resource, alert, operation, panel, section, steps), read from its
own heading and text, so it describes the chunk and never grants or denies anything.
"""
from __future__ import annotations

import json
import re
from typing import Any

import yaml

LIST_ITEM = re.compile(r"^\s{0,3}(?:\d{1,3}[.)]|[-*+]|\|)\s")
STEP = re.compile(r"^\s{0,3}(\d{1,3})[.)]\s")
METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_KEY_OR_ITEM = re.compile(r"^(\s*)(?:-\s+\S|[A-Za-z0-9_.\"'/{}$-]+\s*:)")
_GROUP = re.compile(r"^\s*-\s+name:\s*[\"']?([^\"'#\n]+?)[\"']?\s*$")
_RULE = re.compile(r"^\s*-\s+(alert|record):\s*[\"']?([^\"'#\s]+)")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?|[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}|\b[IWEF]\d{4} \d{2}:\d{2}:\d{2}")
_EVENT = re.compile(r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}|[IWEF]\d{4} \d{2}:\d{2}|\{\"|(?:level|lvl|ts|time)=|(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b)")
_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL|PANIC)\b|\"level\":\s*\"(\w+)\"|level=(\w+)", re.I)
_TF_BLOCK = re.compile(r'(?m)^(resource|data|module)\s+"([^"]+)"(?:\s+"([^"]+)")?')

# A heading's words name the part of a runbook or a postmortem it opens. The first rule that matches wins.
SECTIONS = (
    ("root_cause", r"root[ -]?causes?|contributing factors?|why it happened|five whys|5 whys|\bcauses?\b"),
    ("timeline", r"timeline|chronology|sequence of events"),
    ("impact", r"impact|blast radius|customer|user[- ]facing"),
    ("detection", r"detection|how (?:we|it was) (?:found|noticed|detected)|alerting"),
    ("resolution", r"resolution|resolved|recovery|how it was fixed"),
    ("action_items", r"action items?|follow[- ]?ups?|corrective|preventive|next steps"),
    ("lessons", r"lessons|what went (?:well|wrong)|learnings|retrospective"),
    ("summary", r"summary|overview|tl;?dr|executive|description"),
    ("symptoms", r"symptoms?|signs|what you see|alerts?\b"),
    ("diagnosis", r"diagnos\w*|investigat\w*|triage|troubleshoot\w*|checks?\b|verify the problem"),
    ("mitigation", r"mitigat\w*|remediat\w*|fix\w*|rollback|roll back|restart|procedure|steps|workaround|recover\w*"),
    ("verification", r"verif\w*|validat\w*|confirm\w*|after the fix"),
    ("escalation", r"escalat\w*|contacts?|owners?|on-?call"),
    ("findings", r"findings|observed|evidence"),
    ("prerequisites", r"prerequisites?|before you (?:start|begin)|requirements"),
)
_SECTION_RULES = [(name, re.compile(rf"(?i)\b(?:{rx})")) for name, rx in SECTIONS]
SECTIONED = {"runbook", "postmortem", "investigation", "documentation", "readme", "web_page"}


def section_of(heading: str) -> str | None:
    """The part of a runbook or postmortem a heading opens, from its last segment ("Payments > Root cause")."""
    last = heading.rsplit(" > ", 1)[-1] if heading else ""
    return next((name for name, rx in _SECTION_RULES if rx.search(last)), None) if last else None


# ---- rendering ---------------------------------------------------------------------------------------


def render(doc_type: str, path: str, text: str) -> str:
    """The text MOKS stores and splits. Most documents are kept as they are; a dashboard is mostly layout and
    styling, so its panels and queries are written out, and a JSON API description is written as YAML."""
    if doc_type == "dashboard":
        try:
            return render_dashboard(json.loads(text))
        except (ValueError, TypeError, AttributeError):
            return text
    if doc_type == "openapi" and path.lower().endswith(".json"):
        try:
            return yaml.safe_dump(json.loads(text), sort_keys=False, allow_unicode=True, default_flow_style=False, width=160)
        except (ValueError, TypeError, yaml.YAMLError):
            return text
    return text


def _s(v: Any, limit: int = 400) -> str:
    return " ".join(str(v).split())[:limit] if v not in (None, "", [], {}) else ""


def _datasource(v: Any) -> str:
    if isinstance(v, dict):
        return _s(v.get("type") or v.get("uid"), 80)
    return _s(v, 80)


def _panel(p: dict, row: str = "") -> list[str]:
    title = _s(p.get("title"), 200) or "untitled"
    out = [f"## Panel: {title} ({_s(p.get('type'), 40) or 'panel'})"]
    if row:
        out.append(f"Row: {row}")
    if _s(p.get("description")):
        out.append(_s(p.get("description"), 1000))
    if _datasource(p.get("datasource")):
        out.append(f"Datasource: {_datasource(p.get('datasource'))}")
    for t in p.get("targets") or []:
        if not isinstance(t, dict):
            continue
        query = t.get("expr") or t.get("query") or t.get("rawSql") or t.get("expression") or t.get("target")
        if query:
            ref = _s(t.get("refId"), 10)
            legend = _s(t.get("legendFormat"), 120)
            out.append(f"Query{' ' + ref if ref else ''}: {_s(query, 2000)}" + (f" (legend: {legend})" if legend else ""))
    defaults = ((p.get("fieldConfig") or {}).get("defaults") or {}) if isinstance(p.get("fieldConfig"), dict) else {}
    if _s(defaults.get("unit")):
        out.append(f"Unit: {_s(defaults.get('unit'), 40)}")
    steps = ((defaults.get("thresholds") or {}).get("steps") or []) if isinstance(defaults.get("thresholds"), dict) else []
    marks = [f"{_s(s.get('color'), 20)} from {s.get('value')}" for s in steps if isinstance(s, dict) and s.get("value") is not None]
    if marks:
        out.append("Thresholds: " + ", ".join(marks[:6]))
    alert = p.get("alert")
    if isinstance(alert, dict) and alert.get("name"):
        out.append(f"Alert: {_s(alert.get('name'), 200)}" + (f" ({_s(alert.get('message'), 300)})" if alert.get("message") else ""))
    return out


def render_dashboard(data: dict) -> str:
    d = data["dashboard"] if isinstance(data.get("dashboard"), dict) else data
    out = [f"# Dashboard: {_s(d.get('title'), 200) or 'untitled'}"]
    for key, label in (("uid", "UID"), ("description", "Description")):
        if _s(d.get(key)):
            out.append(f"{label}: {_s(d.get(key), 1000)}")
    if d.get("tags"):
        out.append("Tags: " + ", ".join(_s(t, 40) for t in d["tags"][:20]))
    variables = [v for v in ((d.get("templating") or {}).get("list") or []) if isinstance(v, dict)]
    if variables:
        out += ["", "## Variables"]
        for v in variables[:30]:
            q = v.get("query")
            q = q.get("query") if isinstance(q, dict) else q
            out.append(f"- {_s(v.get('name'), 60)} ({_s(v.get('type'), 20)})" + (f": {_s(q, 300)}" if _s(q) else ""))
    panels = d.get("panels") or []
    for row in d.get("rows") or []:  # the schema before 2018: panels sit inside rows
        if isinstance(row, dict):
            panels.append({"type": "row", "title": row.get("title"), "panels": row.get("panels") or []})
    for p in panels:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":  # a row is a label for the panels in it, not a section worth a chunk of its own
            for child in p.get("panels") or []:
                if isinstance(child, dict):
                    out += [""] + _panel(child, _s(p.get("title"), 200))
        else:
            out += [""] + _panel(p)
    return "\n".join(out) + "\n"


# ---- line marks ----------------------------------------------------------------------------------------
# A mark per line: (heading of a section that starts here, or None; may a chunk end before this line; skip it).


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _yaml_documents(lines: list[str]) -> list[tuple[int, int]]:
    """[start, end) line ranges of the YAML documents, separators left out."""
    out, start = [], 0
    for i, raw in enumerate(lines):
        if raw.rstrip("\r\n").strip() == "---":
            if i > start:
                out.append((start, i))
            start = i + 1
    if len(lines) > start:
        out.append((start, len(lines)))
    return out


def _resource_heading(lines: list[str], a: int, b: int) -> str:
    kind = name = namespace = ""
    in_metadata = False
    for raw in lines[a:b]:
        bare = raw.rstrip("\r\n")
        if not bare.strip() or bare.lstrip().startswith("#"):
            continue
        if not bare[:1].isspace():
            in_metadata = bare.startswith("metadata:")
            if bare.startswith("kind:"):
                kind = bare.split(":", 1)[1].strip().strip("\"'")
            continue
        if in_metadata and _indent(bare) == 2:
            key, _, value = bare.strip().partition(":")
            if key == "name" and not name:
                name = value.strip().strip("\"'")
            elif key == "namespace" and not namespace:
                namespace = value.strip().strip("\"'")
    where = f"{namespace}/{name}" if namespace and name else name
    return " ".join(x for x in (kind, where) if x)[:480]


def _manifest_marks(lines: list[str]) -> list[tuple[str | None, bool, bool]]:
    marks: list[tuple[str | None, bool, bool]] = [(None, False, True)] * len(lines)
    for a, b in _yaml_documents(lines):
        heading = _resource_heading(lines, a, b)
        first = True
        for i in range(a, b):
            bare = lines[i].rstrip("\r\n")
            m = _KEY_OR_ITEM.match(bare)
            boundary = not bare.strip() or bool(m and len(m.group(1)) <= 6 and not bare.lstrip().startswith("#"))
            marks[i] = (heading if first else None, boundary, False)
            first = False
    return marks


def _rule_marks(lines: list[str]) -> list[tuple[str | None, bool, bool]]:
    """A section per rule. The lines that open a group (its name, interval and `rules:`) join its first rule."""
    marks: list[tuple[str | None, bool, bool]] = []
    docs = _yaml_documents(lines)
    preamble = _resource_heading(lines, *docs[0]) if docs else ""
    group, pending = "", None
    opens: dict[int, str] = {}
    for i, raw in enumerate(lines):
        bare = raw.rstrip("\r\n")
        g = _GROUP.match(bare)
        if g and any("rules:" in lines[j] for j in range(i + 1, min(i + 4, len(lines)))):
            group, pending = g.group(1).strip(), i
            continue
        r = _RULE.match(bare)
        if r:
            opens[pending if pending is not None else i] = f"{r.group(1)} {r.group(2)}" + (f" (group {group})" if group else "")
            pending = None
    if opens and not preamble and min(opens) > 0:
        opens[0] = opens.pop(min(opens))  # "groups:" alone is not worth a chunk: it joins the first rule
    for i, raw in enumerate(lines):
        bare = raw.rstrip("\r\n")
        if bare.strip() == "---":
            marks.append((None, False, True))
            continue
        heading = opens.get(i)
        if i == 0 and heading is None and preamble:
            heading = preamble
        boundary = not bare.strip() or heading is not None or bool(re.match(r"^\s*(?:labels|annotations|expr|for):", bare))
        marks.append((heading, boundary, False))
    return marks


def _openapi_marks(lines: list[str]) -> list[tuple[str | None, bool, bool]]:
    """A section per operation under `paths:` and per schema under `components:`; other top-level keys are their own."""
    opens: dict[int, str] = {}
    top, path_indent, method_indent, comp_indent, schema_indent = "", None, None, None, None
    path, pending, comp, carry = "", None, "", None
    title = ""
    for i, raw in enumerate(lines):
        bare = raw.rstrip("\r\n")
        if not bare.strip() or bare.lstrip().startswith("#"):
            continue
        ind = _indent(bare)
        key = bare.strip().split(":", 1)[0].strip("\"'")
        if ind == 0:
            top, path_indent, method_indent, comp_indent, schema_indent, pending = key, None, None, None, None, None
            if key in ("paths", "components", "definitions"):
                carry = i  # "paths:" alone is not worth a chunk: it joins the first operation or schema
            elif key not in ("openapi", "swagger", "info", "servers", "host", "basePath", "schemes"):
                opens[i], carry = key, None
            continue
        if top == "info" and key == "title" and not title:
            title = bare.split(":", 1)[1].strip().strip("\"'")
        if top == "paths":
            if path_indent is None:
                path_indent = ind
            if ind == path_indent:
                path, pending, method_indent = key, i, None
            elif ind > path_indent and key.lower() in METHODS and (method_indent is None or ind == method_indent):
                method_indent = ind
                at = pending if pending is not None else i
                opens[carry if carry is not None else at] = f"{key.upper()} {path}"
                pending = carry = None
        elif top == "components":  # components > schemas > Pet: one section per named thing
            if comp_indent is None:
                comp_indent = ind
            if ind == comp_indent:
                comp, schema_indent = key, None
                continue
            if schema_indent is None:
                schema_indent = ind
            if ind == schema_indent:
                opens[carry if carry is not None else i] = f"{comp[:-1] if comp.endswith('s') else comp} {key}"
                carry = None
        elif top == "definitions":  # Swagger 2
            if schema_indent is None:
                schema_indent = ind
            if ind == schema_indent:
                opens[carry if carry is not None else i] = f"schema {key}"
                carry = None
    marks: list[tuple[str | None, bool, bool]] = []
    for i, raw in enumerate(lines):
        bare = raw.rstrip("\r\n")
        heading = opens.get(i) or (f"API {title}".strip() if i == 0 else None)
        m = _KEY_OR_ITEM.match(bare)
        marks.append((heading, not bare.strip() or bool(m and len(m.group(1)) <= 8), False))
    return marks


def _log_marks(lines: list[str]) -> list[tuple[str | None, bool, bool]]:
    return [(None, bool(_EVENT.match(raw)), False) for raw in lines]


MARKS = {"k8s_manifest": _manifest_marks, "helm_template": _manifest_marks, "kustomization": _manifest_marks, "alert_rules": _rule_marks,
         "openapi": _openapi_marks, "log": _log_marks}
LIST_AWARE = {"runbook", "postmortem", "investigation", "dashboard"}  # Markdown whose lists are steps or events


def chunk(doc_type: str, path: str, text: str, size: int, overlap: int):
    """Pieces for a typed document, or None to use the format's own splitting."""
    from . import chunking as ch

    if doc_type in MARKS:
        lines = re.findall(r"[^\n]*\n|[^\n]+$", text)
        marks = MARKS[doc_type](lines)
        if not any(h for h, _, _ in marks) and doc_type != "log":
            return None  # nothing recognisable: the format's rules split it as before
        spans, sections = _spans(lines, marks, size)
    elif doc_type in LIST_AWARE:
        spans, sections = ch._spans(text, "markdown", size, lists=True)
    else:
        return None
    out = []
    for heading, a, b in sections:
        out.extend(ch._windows(text, spans[a:b], heading, size, overlap, anywhere=doc_type == "log"))
    return out


def _spans(lines: list[str], marks: list[tuple[str | None, bool, bool]], size: int):
    from . import chunking as ch

    spans: list = []
    sections: list[tuple[str, int, int]] = []
    heading, section_start, offset = "", 0, 0
    for number, (raw, (opens, boundary, skip)) in enumerate(zip(lines, marks), 1):
        if skip:
            if len(spans) > section_start:
                sections.append((heading, section_start, len(spans)))
            section_start = len(spans)
            offset += len(raw)
            continue
        if opens is not None:
            if len(spans) > section_start:
                sections.append((heading, section_start, len(spans)))
            heading, section_start = opens[:480], len(spans)
        for start, end in ch._split_line(raw, offset, size):
            spans.append(ch._Span(number, start, end, False, boundary and start == offset))
        offset += len(raw)
    sections.append((heading, section_start, len(spans)))
    return spans, [s for s in sections if s[2] > s[1]]


# ---- what a piece is about -------------------------------------------------------------------------


def annotate(doc_type: str | None, heading: str, text: str) -> tuple[str, dict]:
    """(heading, meta) for one piece. A log piece is headed by the time it covers, which its section cannot know."""
    meta: dict[str, Any] = {}
    if doc_type in ("k8s_manifest", "helm_template", "kustomization") and heading:
        kind, _, where = heading.partition(" ")
        namespace, _, name = where.rpartition("/")
        meta["resource"] = {k: v for k, v in (("kind", kind), ("namespace", namespace), ("name", name)) if v}
    elif doc_type == "alert_rules" and heading:
        m = re.match(r"(alert|record) (\S+)(?: \(group ([^)]+)\))?", heading)
        if m:
            meta[m.group(1)] = m.group(2)
            if m.group(3):
                meta["group"] = m.group(3)
            sev = re.search(r"(?m)^\s*severity:\s*[\"']?(\w+)", text)
            if sev:
                meta["severity"] = sev.group(1).lower()
            meta["has_runbook_url"] = bool(re.search(r"(?m)^\s*runbook_url:", text))
    elif doc_type == "openapi" and heading:
        m = re.match(r"([A-Z]+) (\S+)$", heading)
        if m and m.group(1).lower() in METHODS:
            meta["operation"] = heading
            op = re.search(r"(?m)^\s*operationId:\s*[\"']?([\w.-]+)", text)
            if op:
                meta["operation_id"] = op.group(1)
        elif heading.startswith("schema "):
            meta["schema"] = heading[7:]
    elif doc_type == "dashboard" and heading:
        parts = heading.split(" > ")
        meta["dashboard"] = parts[0].removeprefix("Dashboard: ")
        panel = next((p for p in reversed(parts) if p.startswith("Panel: ")), None)
        if panel:
            meta["panel"] = re.sub(r" \([^)]*\)$", "", panel.removeprefix("Panel: "))
    elif doc_type == "terraform":
        blocks = [f"{k}.{a}" if k == "module" else f"{a}.{b}" if k == "resource" else f"data.{a}.{b}" for k, a, b in _TF_BLOCK.findall(f"{heading}\n{text}")]
        if blocks:
            meta["resources"] = list(dict.fromkeys(blocks))[:12]
    elif doc_type == "log":
        stamps = _TS.findall(text)
        levels = sorted({(m.group(1) or m.group(2) or m.group(3) or "").upper().replace("WARNING", "WARN") for m in _LEVEL.finditer(text)} - {""})
        if stamps:
            meta["first_event"], meta["last_event"] = stamps[0], stamps[-1]
            heading = f"log events {stamps[0]}" + (f" to {stamps[-1]}" if stamps[-1] != stamps[0] else "")
        if levels:
            meta["levels"] = levels
    if doc_type in SECTIONED:
        section = section_of(heading)
        if section:
            meta["section"] = section
    if doc_type in ("runbook", "postmortem", "investigation"):
        steps = [int(m.group(1)) for m in (STEP.match(ln) for ln in text.splitlines()) if m]
        if len(steps) >= 2:
            meta["steps"] = [steps[0], steps[-1]]
    return heading, meta
