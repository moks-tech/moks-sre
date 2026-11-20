"""What a question asks for, read without a model. Everything here only shapes ranking: access is decided
by the grants passed to search, which nothing here can see or change, so no rewrite can widen a scope."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

ERROR = re.compile(r"\b(?:[A-Z]{2,}_[A-Z0-9_]+|[A-Z][a-z]+(?:[A-Z][a-z]+)*(?:Error|Exception|BackOff|Killed|Failed)|(?:HTTP\s*)?[45]\d{2}\b|E[A-Z]{3,})")
RESOURCE = re.compile(r"(?i)\b(pods?|deployments?|services?|ingress(?:es)?|nodes?|statefulsets?|daemonsets?|pvcs?|configmaps?|secrets?|namespaces?|jobs?|cronjobs?)\b")
TIME = re.compile(r"(?i)\b(today|yesterday|last (?:night|week|hour|\d+ (?:minutes?|hours?|days?))|since \S+|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})\b")
CLAUSE = re.compile(r"(?i)\?\s+|;\s+|\s+and also\s+|\s+as well as\s+")
MAX_PARTS = 3
MIN_WORDS = 3


def understand(query: str, identifiers: list[str]) -> dict:
    return {"identifiers": identifiers, "errors": list(dict.fromkeys(ERROR.findall(query)))[:6],
            "resource_types": sorted({m.lower().rstrip("s") for m in RESOURCE.findall(query)})[:6], "time_references": TIME.findall(query)[:4]}


def parts(query: str) -> list[str]:
    """Separate questions in one sentence. A whole query averages them out, and each deserves its own ranking."""
    found = [p.strip(" ?") for p in CLAUSE.split(query)]
    found = [p for p in found if len(p.split()) >= MIN_WORDS]
    return found[:MAX_PARTS] if len(found) > 1 else []


# ---- intent, time and names ------------------------------------------------------------------------------
# What kind of answer a question wants, by rules. Several can match ("why is payments failing after the deploy" is
# troubleshooting and change); the first is the primary one. They shape ranking by small visible signals only.
INTENTS: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(rx, re.I)) for name, rx in (
    ("similar_incidents", r"\b(?:similar|previous|past|earlier|prior|other|same)\b[\w\s-]{0,40}\b(?:incidents?|outages?|postmortems?|failures?|issues?|problems?)\b"
                          r"|\bpost-?mortems?\b|\bhas this happened\b|\blast time\b|\b(?:been|was|were|happen\w*|seen|had)\b[\w\s-]{0,30}\bbefore\b"),
    ("dependencies", r"\bdepend(?:s|ent|ents|ency|encies)?\b|\b(?:who|what|which services?)\b[\w\s]{0,20}\b(?:uses?|calls?|talks? to|connects? to)\b|\bconsumers? of\b|\bblast radius\b|\bdownstream\b|\bupstream\b"),
    ("change", r"\bwhat(?:'s| has| have)? changed\b|\bchanges?\b|\brecent(?:ly)? (?:deploy\w*|release\w*|commits?|rollouts?)\b|\brolled out\b|\bdiff\b|\bnew version\b"),
    ("procedure", r"\bhow (?:do|can|to|should|would) (?:i|we|you)?\b|\brunbooks?\b|\bplaybooks?\b|\bprocedures?\b|\bsteps? to\b|\btroubleshoot\w*\b|\bremediat\w*\b|\bmitigat\w*\b"
                  r"|\brecover\w*\b|\brestor\w*\b|\broll ?back\b|\bfail ?over\b|\bdrain\b|\bscale (?:up|down|out)\b"),
    ("alert", r"\balerts?\b|\balerting\b|\bfiring\b|\bprometheus rules?\b|\balertmanager\b|\bburn rate\b|\bslo\b"),
    ("dashboard", r"\bdashboards?\b|\bgrafana\b|\bpanels?\b|\bgraphs?\b|\bpromql\b"),
    ("api", r"\bapi\b|\bendpoints?\b|\bopenapi\b|\bswagger\b|\boperationid\b|\b(?:GET|POST|PUT|PATCH|DELETE) /"),
    ("configuration", r"\bconfig\w*\b|\bvalues\.ya?ml\b|\bhelm values\b|\bsettings?\b|\benv(?:ironment)? var\w*\b|\bterraform\b|\bmanifests?\b|\blimits?\b|\breplicas?\b"),
    ("troubleshooting", r"\bwhy\b|\bfail\w*\b|\berrors?\b|\bcrash\w*\b|\bdown\b|\bbroken\b|\bslow\b|\blatency\b|\btime ?outs?\b|\boom\w*\b|\bbacklog\b|\bpressure\b"
                        r"|\bevict\w*\b|\brestart\w*\b|\bunhealthy\b|\b5\d\d\b|\bnot (?:working|ready|starting)\b"),
))
# What each intent is best answered by: (document types, sections). Preference, never a filter. Sections count only for
# past incidents (a postmortem's root cause over its timeline): for a runbook, a section named "Mitigation" said less than a
# runbook titled with the question (measured on the sre dataset, 2026-11-20).
PREFERS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "similar_incidents": (frozenset({"postmortem", "investigation"}), frozenset({"root_cause", "summary", "impact", "resolution"})),
    "dependencies": (frozenset({"k8s_manifest", "helm_values", "helm_template", "terraform", "documentation", "readme"}), frozenset()),
    "change": (frozenset({"k8s_manifest", "helm_values", "helm_template", "terraform", "ci_pipeline"}), frozenset()),
    "procedure": (frozenset({"runbook", "documentation", "readme"}), frozenset()),
    "alert": (frozenset({"alert_rules", "runbook"}), frozenset()),
    "dashboard": (frozenset({"dashboard"}), frozenset()),
    "api": (frozenset({"openapi", "documentation"}), frozenset()),
    "configuration": (frozenset({"helm_values", "k8s_manifest", "terraform", "config", "helm_template", "kustomization"}), frozenset()),
    "troubleshooting": (frozenset({"runbook", "postmortem", "alert_rules", "documentation"}), frozenset()),
}
RECENT_DAYS = 7  # "recently", or a change question that names no time


def intents(query: str) -> list[str]:
    return [name for name, rx in INTENTS if rx.search(query)]


def time_window(query: str, now: datetime) -> dict | None:
    """When a question asks about, as the earliest moment that counts. Days are UTC."""
    q = query.lower()
    m = re.search(r"\blast (\d{1,3}) ?(minutes?|mins?|hours?|hrs?|days?|weeks?)\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n) if unit.startswith("h") else timedelta(weeks=n) if unit.startswith("w") else timedelta(days=n)
        return {"since": now - delta, "phrase": m.group(0)}
    for phrase, delta in (("last hour", timedelta(hours=1)), ("past hour", timedelta(hours=1)), ("last night", timedelta(hours=18)), ("last week", timedelta(days=7)),
                          ("past week", timedelta(days=7)), ("this week", timedelta(days=7)), ("last month", timedelta(days=30)), ("past day", timedelta(days=1)), ("last day", timedelta(days=1))):
        if phrase in q:
            return {"since": now - delta, "phrase": phrase}
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\btoday\b", q):
        return {"since": midnight, "phrase": "today"}
    if re.search(r"\byesterday\b", q):
        return {"since": midnight - timedelta(days=1), "phrase": "yesterday"}
    m = re.search(r"\bsince (\d{4}-\d{2}-\d{2})\b", q)
    if m:
        try:
            return {"since": datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc), "phrase": m.group(0)}
        except ValueError:
            return None
    if re.search(r"\brecent(?:ly)?\b", q) or "change" in intents(query):
        return {"since": now - timedelta(days=RECENT_DAYS), "phrase": "recently"}
    return None


_SUFFIXES = ("-service", "-svc", "-api", "-app", "-server", "-worker", "service", "svc")
_ENV_ALIASES = {"production": "prod", "prd": "prod", "staging": "stage", "stg": "stage", "development": "dev", "testing": "test", "qa": "test"}


def _stem(name: str) -> str:
    n = name.lower()
    for suffix in _SUFFIXES:
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)].rstrip("-_")
            break
    return n[:-1] if n.endswith("s") and len(n) > 4 else n


def _mentions(query: str, name: str) -> bool:
    """A name appears as a whole word, in any case; "payment-service" also names "payments" and the reverse."""
    if not name or len(name) < 3:
        return False
    q = query.lower()
    if re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", q):
        return True
    stem = _stem(name)
    return len(stem) >= 4 and any(_stem(w) == stem for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", q))


def resolve(query: str, catalog: list[tuple]) -> dict:
    """Services, environments and namespaces a question names, among those the asker can read, and for each named
    service what declares it a dependency (a source's applies_to.dependency_of). Never more than the catalog holds."""
    services: set[str] = set()
    envs: set[str] = set()
    namespaces: set[str] = set()
    for environment, namespace, doc_service, src_service, applies_to in catalog:
        services.update(x for x in (doc_service, src_service) if x)
        services.update(str(x) for x in ((applies_to or {}).get("services") or []) if x)
        if environment and environment != "*":
            envs.add(environment)
        if namespace and namespace != "*":
            namespaces.add(namespace)
    words = {_ENV_ALIASES.get(w, w) for w in re.findall(r"[a-z0-9][a-z0-9_-]*", query.lower())}
    named_envs = {e for e in envs if _mentions(query, e) or _ENV_ALIASES.get(e.lower(), e.lower()) in words}
    named = sorted(s for s in services if _mentions(query, s))[:6]
    dependents: dict[str, set[str]] = {}
    for _e, _n, doc_service, src_service, applies_to in catalog:
        of = [str(x) for x in ((applies_to or {}).get("dependency_of") or []) if x]
        if not of:
            continue
        for about in {x for x in (doc_service, src_service, *((applies_to or {}).get("services") or [])) if x}:
            if about in named:
                dependents.setdefault(about, set()).update(of)
    return {"services": named, "environments": sorted(named_envs)[:4], "namespaces": sorted(n for n in namespaces if _mentions(query, n))[:4],
            "dependents": {k: sorted(v)[:20] for k, v in dependents.items()}}
