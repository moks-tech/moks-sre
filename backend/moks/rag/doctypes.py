"""What kind of operational document a file is, decided from its path, its source and the first lines of its
text, without a model. A document type chooses how the text is split (rag/structure.py), which questions it
answers best (rag/query.py), and how it is labelled where it is cited. It never decides who may read it.

The rules are deliberately few and visible. A file that matches none keeps the type of its format
(documentation, config, code, text), which is what every document was before types existed.
"""
from __future__ import annotations

import json
import re

# Every type a document can have. The order of TYPES is the order the Explorer lists them in.
TYPES = (
    "runbook", "postmortem", "investigation", "readme", "documentation", "web_page",
    "k8s_manifest", "helm_chart", "helm_values", "helm_template", "kustomization",
    "terraform", "alert_rules", "dashboard", "openapi", "ci_pipeline", "dockerfile",
    "code", "log", "config", "text",
)
HISTORICAL = {"postmortem", "investigation"}  # what happened once, not how things are meant to work now

_SEGMENT = lambda *names: re.compile(r"(?:^|/)(?:" + "|".join(names) + r")(?:/|[-_.]|$)", re.I)  # noqa: E731
RUNBOOK_PATH = _SEGMENT("runbooks?", "playbooks?", "sops?", "on-?call", "operations")
POSTMORTEM_PATH = _SEGMENT("postmortems?", "post-?mortems?", "incidents?", "rcas?", "post-incident", "retrospectives?", "pirs?")
LOG_EXT = re.compile(r"\.log(?:\.\d+)?$|\.out$", re.I)
CODE_EXT = {".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".rb", ".rs", ".c", ".h", ".cpp", ".cs", ".php", ".scala", ".swift", ".groovy", ".sql", ".sh", ".bash"}
MARKDOWN_EXT = {".md", ".markdown", ".mdx", ".rst", ".adoc", ".txt"}
DATA_EXT = {".json", ".toml", ".ini", ".cfg", ".conf", ".properties"}

_K8S_KIND = re.compile(r"(?m)^kind:\s*[\"']?([A-Z][A-Za-z0-9]+)")
_API_VERSION = re.compile(r"(?m)^apiVersion:\s*\S+")
_ALERT_RULE = re.compile(r"(?m)^\s*-\s+(?:alert|record):\s*\S")
_RULE_GROUPS = re.compile(r"(?m)^\s*groups:\s*$")
_OPENAPI = re.compile(r"(?m)^[\"']?(?:openapi|swagger)[\"']?\s*:\s*[\"']?\d")
_TIMESTAMP = re.compile(r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2}|[IWEF]\d{4} \d{2}:\d{2}|\{\"(?:ts|time|timestamp|@timestamp|level)\"|(?:level|lvl|ts|time)=)")
_FRONT_TYPE = re.compile(r"(?im)^(?:type|kind|category):\s*[\"']?(runbook|playbook|postmortem|post-mortem|incident|rca)\b")
_POSTMORTEM_HEADINGS = re.compile(r"(?im)^#{1,4}\s*(?:root[ -]cause|timeline|impact|contributing factors|lessons learned|what went wrong)\b")
_RUNBOOK_HEADINGS = re.compile(r"(?im)^#{1,4}\s*(?:runbook|playbook|procedure|mitigation|remediation|steps|diagnos\w*|troubleshoot\w*)\b")


def _name(path: str) -> str:
    return path.lower().rsplit("/", 1)[-1]


def _ext(path: str) -> str:
    name = _name(path)
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def _json(text: str) -> object | None:
    head = text.lstrip()[:1]
    if head not in ("{", "["):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def is_dashboard(data: object) -> bool:
    """A Grafana dashboard, as exported (plain) or as the API returns it ({"dashboard": {...}})."""
    if isinstance(data, dict) and isinstance(data.get("dashboard"), dict):
        data = data["dashboard"]
    return isinstance(data, dict) and isinstance(data.get("panels") or data.get("rows"), list) and ("schemaVersion" in data or "title" in data)


def is_log(text: str) -> bool:
    """Most non-blank lines open with a timestamp or a level: an application log, syslog, klog, JSON or logfmt."""
    lines = [ln for ln in text.splitlines()[:200] if ln.strip()]
    return len(lines) >= 3 and sum(bool(_TIMESTAMP.match(ln)) for ln in lines) >= 0.6 * len(lines)


def classify(path: str, text: str, source_type: str = "github", declared: str | None = None) -> str:
    """The document type. `declared` (an upload's or a source's own statement) wins when it names a known type."""
    if declared in TYPES:
        return declared
    if source_type == "incident":
        return "postmortem"
    if source_type == "investigation":
        return "investigation"
    p, name, ext = path.lower(), _name(path), _ext(path)
    head = text[:20000]
    if source_type == "web" or p.startswith(("http://", "https://")):
        if POSTMORTEM_PATH.search(p.split("://", 1)[-1]):
            return "postmortem"
        return "runbook" if RUNBOOK_PATH.search(p.split("://", 1)[-1]) else "web_page"
    if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return "dockerfile"
    if p.startswith(".github/workflows/") or "/.github/workflows/" in p or name in (".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", ".drone.yml") or p.startswith(".circleci/"):
        return "ci_pipeline"
    if ext in (".tf", ".hcl", ".tfvars"):
        return "terraform"
    if LOG_EXT.search(name):
        return "log"
    if ext in MARKDOWN_EXT:
        front = _FRONT_TYPE.search(head[:2000])
        if front:
            return "runbook" if front.group(1).lower() in ("runbook", "playbook") else "postmortem"
        if POSTMORTEM_PATH.search(p) or len(_POSTMORTEM_HEADINGS.findall(head)) >= 2:
            return "postmortem"
        if RUNBOOK_PATH.search(p) or "runbook" in name or "playbook" in name:
            return "runbook"
        if name.startswith("readme"):
            return "readme"
        if len(_RUNBOOK_HEADINGS.findall(head)) >= 2:
            return "runbook"
        if ext == ".txt" and is_log(head):
            return "log"
        return "documentation"
    if ext in (".yaml", ".yml"):
        if name == "chart.yaml":
            return "helm_chart"
        if name.startswith("kustomization."):
            return "kustomization"
        if _OPENAPI.search(head[:3000]):
            return "openapi"
        if _ALERT_RULE.search(head) and (_RULE_GROUPS.search(head) or "PrometheusRule" in head):
            return "alert_rules"
        if "/templates/" in f"/{p}" and ("{{" in head or _API_VERSION.search(head)):
            return "helm_template"
        if name.startswith("values") and not _API_VERSION.search(head):
            return "helm_values"
        if _API_VERSION.search(head) and _K8S_KIND.search(head):
            return "k8s_manifest"
        return "config"
    if ext == ".json":
        data = _json(text)
        if data is not None and is_dashboard(data):
            return "dashboard"
        if isinstance(data, dict) and ("openapi" in data or "swagger" in data) and "paths" in data:
            return "openapi"
        return "config"
    if ext in DATA_EXT:
        return "config"
    if ext in CODE_EXT or name in ("makefile",):
        return "code"
    if is_log(head):
        return "log"
    return "text"


def is_rendered(doc_type: str | None, path: str) -> bool:
    """Stored as a rendering of the original, not the original text (a dashboard's panels, a JSON API description as
    YAML). A citation's lines are the rendering's, so a link to the file carries no line anchor."""
    return doc_type == "dashboard" or (doc_type == "openapi" and _ext(path) == ".json")


def resource_types(doc_type: str, text: str) -> list[str]:
    """The kinds of thing a document describes, lowercased so they meet a question's words ("deployment", "pod")."""
    found: list[str] = []
    if doc_type in ("k8s_manifest", "helm_template", "kustomization", "alert_rules"):
        found += [k.lower() for k in _K8S_KIND.findall(text)]
    if doc_type == "terraform":
        found += [m.group(1) for m in re.finditer(r'(?m)^resource\s+"([A-Za-z0-9_]+)"', text)]
    if doc_type == "alert_rules":
        found.append("alert")
    if doc_type == "dashboard":
        found.append("dashboard")
    return sorted(set(found))[:40]
