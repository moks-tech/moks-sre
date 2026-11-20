"""What a question asks for, read without a model. Everything here only shapes ranking: access is decided
by the grants passed to search, which nothing here can see or change, so no rewrite can widen a scope.

Nothing here rewrites the question either. normalize_query fixes only what a person did not mean to type (invisible
characters, typographic quotes and dashes, runs of whitespace); case, punctuation inside names and every identifier
stay as typed, because "OOMKilled", "aws_subnet.private[0]" and "{app=\"api\"}" lose their meaning when changed.
Entities, the query type and expansion terms are extra signals beside the question, never replacements for it.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

ERROR = re.compile(r"\b(?:[A-Z]{2,}_[A-Z0-9_]+|[A-Z][a-z]+(?:[A-Z][a-z]+)*(?:Error|Exception|BackOff|Killed|Failed)|(?:HTTP\s*)?[45]\d{2}\b|E[A-Z]{3,})")
RESOURCE = re.compile(r"(?i)\b(pods?|deployments?|services?|ingress(?:es)?|nodes?|statefulsets?|daemonsets?|pvcs?|configmaps?|secrets?|namespaces?|jobs?|cronjobs?)\b")
TIME = re.compile(r"(?i)\b(today|yesterday|last (?:night|week|hour|\d+ (?:minutes?|hours?|days?))|since \S+|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})\b")
CLAUSE = re.compile(r"(?i)\?\s+|;\s+|\s+and also\s+|\s+as well as\s+")
MAX_PARTS = 3
MIN_WORDS = 3
MAX_ENTITIES = 8
MAX_EXPANSIONS = 8
EXPANSIONS = Path(__file__).with_name("query_expansions.yaml")

# ---- normalization ----------------------------------------------------------------------------

_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤﻿]")
_TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
                             "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-", " ": " ", " ": " "})


def normalize_query(query: str) -> str:
    """Compatibility forms (full-width letters, ligatures), invisible characters, typographic quotes and dashes, and
    whitespace. A word-processor dash inside moks–api becomes the hyphen the name really has; nothing else changes."""
    q = unicodedata.normalize("NFKC", query).translate(_TYPOGRAPHY)
    q = _INVISIBLE.sub("", q)
    q = "".join(ch if ch.isprintable() else " " for ch in q)
    return " ".join(q.split())


# ---- entities ---------------------------------------------------------------------------------

K8S_KINDS = "pods?|po|deployments?|deploy|services?|svc|statefulsets?|sts|daemonsets?|ds|jobs?|cronjobs?|cj|ingress(?:es)?|ing|nodes?|no|pvc|pv|configmaps?|cm|secrets?|namespaces?|ns|hpa|replicasets?|rs"
K8S_STATUS = ("CrashLoopBackOff|ImagePullBackOff|ErrImagePull|OOMKilled|Evicted|ContainerCreating|CreateContainerConfigError|CreateContainerError|RunContainerError|"
              "InvalidImageName|FailedScheduling|FailedMount|FailedAttachVolume|NodeNotReady|NotReady|DiskPressure|MemoryPressure|PIDPressure|PodEvictionFailure|"
              "Terminating|BackOff|Unhealthy|DeadlineExceeded")
AWS_SERVICES = ("EKS|EC2|ECS|ECR|RDS|Aurora|S3|IAM|STS|KMS|ALB|NLB|ELB|VPC|NAT|Route ?53|CloudWatch|CloudTrail|DynamoDB|Lambda|SQS|SNS|EBS|EFS|"
                "Secrets Manager|SSM|Fargate|CloudFront|API Gateway|Kinesis|MSK|ElastiCache|OpenSearch|IRSA")
TECHNOLOGIES = ("kubernetes|k8s|kubectl|helm|argo ?cd|istio|envoy|terraform|opentofu|prometheus|promql|alertmanager|grafana|loki|logql|promtail|alloy|"
                "opentelemetry|otel|otlp|jaeger|tempo|coredns|cert-manager|karpenter|cluster-autoscaler|metrics-server|postgres(?:ql)?|redis|kafka|nginx|docker|containerd")

ENTITY_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(rx)) for name, rx in (
    ("arn", r"\barn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{0,12}:[^\s\"',)]+"),
    ("aws_resource_id", r"\b(?:i|vpc|subnet|sg|ami|eni|vol|snap|igw|nat|rtb|acl|tgw|vpce|lt|eipalloc)-[0-9a-f]{8,17}\b"),
    ("aws_region", r"\b(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|southeast|northwest|southwest)-\d\b"),
    ("aws_service", rf"\b(?:{AWS_SERVICES})\b"),
    ("s3_uri", r"\bs3://[a-z0-9.\-]{3,63}(?:/[^\s\"']*)?"),
    ("k8s_object", rf"(?i)\b(?:{K8S_KINDS})/[a-z0-9][a-z0-9.-]{{0,252}}"),
    ("k8s_status", rf"\b(?:{K8S_STATUS})\b"),
    ("k8s_namespace", r"(?:^|\s)(?:-n|--namespace)[ =]([a-z0-9][a-z0-9-]{0,62})\b|\bnamespace[ :=]+\"?([a-z0-9][a-z0-9-]{0,62})\b"),
    ("pod_name", r"\b[a-z0-9]+(?:-[a-z0-9]+)*-[a-f0-9]{8,10}-[a-z0-9]{5}\b"),
    ("terraform_address", r"\b(?:module\.[a-z0-9_-]+(?:\[[^\]\s]+\])?\.)*(?:data\.)?(?:aws|google|azurerm|kubernetes|helm|null|random|tls|cloudflare|github|vault|local)_[a-z0-9_]+\.[a-z0-9_-]+(?:\[[^\]\s]+\])?"),
    ("terraform_module", r"\bmodule\.[a-z0-9_-]+(?:\[[^\]\s]+\])?"),
    ("metric", r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:total|seconds|bytes|bucket|count|sum|ratio|info|created|percent|milliseconds|series|pending)\b"),
    ("promql_function", r"\b(?:rate|irate|increase|histogram_quantile|topk|bottomk|absent|predict_linear|deriv|delta|label_replace|count_over_time|rate_over_time|bytes_over_time|quantile_over_time|avg_over_time|sum_over_time|max_over_time)(?=\s*\()"),
    ("label_selector", r"\{[^{}]*?[a-zA-Z_][a-zA-Z0-9_]*\s*(?:=~|!~|!=|=)\s*\"[^\"]*\"[^{}]*\}"),
    ("logql_stage", r"\|[=~]\s*\"[^\"]*\"|!=\s*\"[^\"]*\"|\|\s*(?:json|logfmt|pattern|regexp|line_format|label_format|unwrap|decolorize)\b"),
    ("otel_attribute", r"\b(?:service|deployment|k8s|http|db|rpc|messaging|net|network|server|client|url|user_agent|exception|process|host|container|cloud|telemetry|otel)\.[a-z_]+(?:\.[a-z_]+)*\b"),
    ("trace_id", r"\b[0-9a-f]{32}\b"),
    ("http_status", r"\b(?:HTTP|status(?: code)?)\s*[1-5]\d{2}\b|\b[45]\d{2}(?=\s+(?:error|errors|Bad|bad|Gateway|gateway|Service|Not|Forbidden|Unauthorized|Too|Internal|Gone|Conflict|timeout))|\b[1-5]xx\b"),
    ("exit_code", r"\bexit(?:\s+code)?\s*[:=]?\s*\d{1,3}\b"),
    ("env_var", r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),
    ("exception", r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception|Failure|Fault)\b"),
    ("file_path", r"(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.(?:ya?ml|json|tf|tfvars|py|go|sh|conf|toml|ini|md|hcl|tpl|js|ts)\b|\b[\w-]+\.(?:tf|tfvars|ya?ml|py|hcl)\b"),
    ("ip_address", r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"),
    ("version", r"\bv\d+(?:\.\d+){1,2}(?:-[\w.]+)?\b|\b\d+\.\d+\.\d+(?:-[\w.]+)?\b"),
    ("service", r"\b(?:app|service|job|container|service\.name|k8s\.deployment\.name)\s*=~?\s*\"?([a-z0-9][a-z0-9_.-]*)\"?|\bthe ([a-z0-9]+(?:-[a-z0-9]+)+) service\b"),
    ("technology", rf"(?i)\b(?:{TECHNOLOGIES})\b"),
))
# Types whose values are worth matching exactly, as identifiers; the rest describe the question.
EXACT_TYPES = ("arn", "aws_resource_id", "s3_uri", "k8s_object", "pod_name", "terraform_address", "metric", "trace_id", "env_var", "exception", "k8s_status", "file_path")


def entities(query: str) -> dict[str, list[str]]:
    """Typed names in the question, in the order they appear. Bounded per type."""
    out: dict[str, list[str]] = {}
    for name, rx in ENTITY_PATTERNS:
        found = []
        for m in rx.finditer(query):
            groups = [g for g in m.groups() if g] if m.groups() else []
            value = (groups[0] if groups else m.group(0)).strip()
            if value and value not in found:
                found.append(value)
        if found:
            out[name] = found[:MAX_ENTITIES]
    if "terraform_address" in out and "terraform_module" in out:  # a module prefix of a full address says nothing more
        out["terraform_module"] = [m for m in out["terraform_module"] if not any(a.startswith(m + ".") for a in out["terraform_address"])]
        if not out["terraform_module"]:
            del out["terraform_module"]
    return out


def exact_terms(found: dict[str, list[str]]) -> list[str]:
    """Entity values to match as typed, beside the identifiers the search already extracts."""
    return list(dict.fromkeys(v for t in EXACT_TYPES for v in found.get(t, []) if len(v) >= 4))


# ---- query type -------------------------------------------------------------------------------

QUERY_TYPES = ("incident_history", "code", "identifier", "how_to", "conceptual", "troubleshooting", "general")
_HISTORY = re.compile(r"(?i)\b(last time|before|previous(?:ly)?|history|historical|post-?mortem|incident report|happened|again|recurr\w*|similar incident|INC-\d+|when did we)\b")
_CODE = re.compile(r"(?i)\b(script|snippet|function|class|sample code|example code|code (?:for|that|to)|manifest|dockerfile|workflow|formatter|python|boto3|bash|golang|regex|example yaml|yaml for|helm values|backend config|terraform s3 backend)\b|\bdef \w|\bimport \w|\.(?:py|tf|ya?ml|sh)\b")
_HOW = re.compile(r"(?i)^(?:how (?:do|can|to|should|does)|steps|configure|set ?up|enable|create|add|install|migrate|upgrade|rotate|roll ?back|renew|promote|drain|scale|stop|find|manage|keep|block|jump|mute)\b|\bhow (?:do|can) (?:i|we)\b|\bhow to\b|\b(?:promql|logql|query) (?:for|to)\b|\bwhat inputs\b")
_CONCEPT = re.compile(r"(?i)^(?:what (?:is|are|does)|explain|difference between|when should (?:i|we) use|should \S+ be)\b|\bvs\.?\b|\bversus\b|\bdifference between\b")
_TROUBLE = re.compile(r"(?i)\b(why|error|errors|fail\w*|broken|not working|doesn'?t|does not|can'?t|cannot|won'?t|never|stuck|crash\w*|restart\w*|dying|slow|latency|timeouts?|timed out|time out|denied|refused|missing|disappeared|down|high|spike\w*|drop\w*|leak\w*|oom|exhausted|full|unavailable|hangs?|keeps?|empty|no data|not \w+ing|returns? [45]\d\d|gateway timeout)\b")
_PLAIN = re.compile(r"[A-Za-z]{3,}")
_STOP = set("the and for are was why how what when with from that this does did has have not can you our into any all its show get my me is it of to in on at be an".split())


def classify(query: str, found: dict[str, list[str]] | None = None, identifiers: list[str] | None = None) -> dict:
    """A query type and the rule that chose it. Rules, not a model, so the reason can be shown and tested."""
    found = entities(query) if found is None else found
    ids = set(identifiers or []) | set(exact_terms(found))
    if _HISTORY.search(query):
        return {"type": "incident_history", "rule": "mentions earlier incidents"}
    if _CODE.search(query):
        return {"type": "code", "rule": "asks for code or configuration"}
    plain = [w for w in _PLAIN.findall(query) if w.lower() not in _STOP and not any(w in i for i in ids)]
    if ids and len(plain) <= 2:
        return {"type": "identifier", "rule": "mostly identifiers"}
    if _HOW.search(query):
        return {"type": "how_to", "rule": "asks how to do something"}
    if _CONCEPT.search(query):
        return {"type": "conceptual", "rule": "asks what something is"}
    if _TROUBLE.search(query) or found.get("k8s_status") or found.get("exception") or found.get("http_status"):
        return {"type": "troubleshooting", "rule": "describes a symptom"}
    return {"type": "general", "rule": "no rule matched"}


# ---- expansion --------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _expansions() -> list[tuple[re.Pattern, list[str]]]:
    raw = (yaml.safe_load(EXPANSIONS.read_text()) or {}).get("expansions") or {}
    return [(re.compile(rf"(?i)(?<![\w-]){re.escape(k)}(?![\w-])"), [str(t) for t in v]) for k, v in raw.items()]


def expansion_terms(query: str) -> list[str]:
    """Related SRE terms for one more keyword ranking: "restarting" also finds CrashLoopBackOff. Only terms the
    question does not already contain; the question, its identifiers and its vector are untouched."""
    lower = query.lower()
    out: list[str] = []
    for rx, terms in _expansions():
        if rx.search(query):
            out += [t for t in terms if t.lower() not in lower and t not in out]
    return out[:MAX_EXPANSIONS]


# ---- the whole reading ------------------------------------------------------------------------

def understand(query: str, identifiers: list[str], expansion: bool = False) -> dict:
    found = entities(query)
    out = {"identifiers": identifiers, "errors": list(dict.fromkeys(ERROR.findall(query)))[:6],
           "resource_types": sorted({m.lower().rstrip("s") for m in RESOURCE.findall(query)})[:6], "time_references": TIME.findall(query)[:4],
           "entities": found, "query_type": classify(query, found, identifiers)}
    if expansion:
        out["expansions"] = expansion_terms(query)
    return out


def services(found: dict[str, list[str]]) -> set[str]:
    return {s.lower() for s in found.get("service", [])}


def parts(query: str) -> list[str]:
    """Separate questions in one sentence. A whole query averages them out, and each deserves its own ranking."""
    found = [p.strip(" ?") for p in CLAUSE.split(query)]
    found = [p for p in found if len(p.split()) >= MIN_WORDS]
    return found[:MAX_PARTS] if len(found) > 1 else []
