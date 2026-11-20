"""Root-cause hypotheses: a library of failure patterns matched against signals, plus what agents propose.

A pattern names the evidence a cause needs (required groups, any one signal in a group satisfies it), what adds to it,
what speaks against it, which signals are the cause and which the symptom (for temporal correlation), the probes
that collect what is missing, how a person verifies it and the remediation to consider. Patterns are data: a new
failure mode is a new entry, not new code. A hypothesis is generated when the evidence satisfies at least one required
group; that is a candidate to score and test, never a conclusion.

An agent may propose a hypothesis outside the library. It must cite evidence the ledger holds; it is scored with
the same factors, from its citations only, and cannot reach "strong" without a library pattern's required evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .signals import Signal


@dataclass(frozen=True)
class Req:
    label: str
    any_of: tuple[str, ...]
    probes: tuple[str, ...] = ()  # collect.PROBES keys that can supply it


@dataclass(frozen=True)
class Rem:
    action: str
    risk: str
    rollback: str
    action_kind: str | None = None  # an allowlisted action (moks/actions/catalog.py), proposed through an action plan, never run here


@dataclass(frozen=True)
class Pattern:
    key: str
    title: str
    category: str
    statement: str  # {service} is the target's service
    required: tuple[Req, ...]
    supporting: tuple[str, ...] = ()
    contradicted_by: tuple[str, ...] = ()  # present signals that speak against it; a required signal observed absent always does
    causes: tuple[str, ...] = ()           # signals that stand for the cause or the change
    symptoms: tuple[str, ...] = ()         # signals that stand for the effect
    topic: str | None = None               # signals.TOPICS key: runbooks, past incidents and commits about the same thing
    symptom_of: tuple[str, ...] = ()       # patterns this one is usually a consequence of (scoring.explain_away)
    verification: tuple[str, ...] = ()
    remediation: tuple[Rem, ...] = ()


SYMPTOM = Req("a user-facing symptom (latency, errors or a firing alert)", ("metric.latency_increase", "metric.error_rate_increase", "alert.firing", "log.http_5xx", "trace.error"),
              ("service_signals", "alerts"))

PATTERNS: tuple[Pattern, ...] = (
    Pattern("connection_pool_exhaustion", "Database connection pool exhaustion", "data_store",
            "{service} cannot get database connections fast enough: its connection pool, or the database's connection limit, is exhausted",
            (SYMPTOM, Req("connection saturation", ("log.db_pool_exhausted", "metric.db_connections_saturated", "metric.db_connections_increase", "ml.anomaly:db_connections"),
                          ("service_signals", "pool_logs"))),
            supporting=("log.db_timeout", "metric.db_connections_increase", "metric.cpu_increase", "deploy.rollout", "git.topic:connection_pool", "kb.topic:connection_pool",
                        "history.cause:connection_pool", "ml.anomaly", "trace.error", "k8s.probe_failed"),
            contradicted_by=("log.dns_failure",),
            causes=("deploy.rollout", "deploy.image_change", "git.topic:connection_pool", "metric.traffic_surge"),
            symptoms=("metric.latency_increase", "metric.error_rate_increase", "metric.db_connections_saturated", "log.db_pool_exhausted", "alert.firing"),
            topic="connection_pool",
            verification=("Compare the pool size the running revision configures with the previous revision (environment variables and config, describe_pod).",
                          "Check the database's active and idle connections against max_connections for this service's user.",
                          "Look for connections held across slow queries: query latency and lock waits at the onset."),
            remediation=(Rem("Roll back to the previous revision if the deployment changed the pool or query behaviour", "low; reverts other changes in the release", "roll forward again",
                             "rollback_deployment"),
                         Rem("Raise the pool size or the database's connection limit after confirming the database has headroom", "medium; more connections can overload the database",
                             "restore the previous pool size", "apply_config_patch"))),
    Pattern("deployment_regression", "Regression introduced by a recent deployment", "deployment",
            "a change rolled out to {service} shortly before the problem began introduced it",
            (Req("a symptom", ("metric.error_rate_increase", "metric.latency_increase", "k8s.crashloop", "log.exception", "alert.firing", "k8s.restarts"), ("service_signals", "pod_metrics")),
             Req("a rollout or image change in the window", ("deploy.rollout", "deploy.image_change"), ("rollout_history",))),
            supporting=("git.commit", "deploy.commit_resolved", "deploy.image_change", "ml.anomaly", "kb.topic:deployment", "history.cause:deployment"),
            causes=("deploy.rollout", "deploy.image_change", "git.commit"),
            symptoms=("metric.error_rate_increase", "metric.latency_increase", "k8s.crashloop", "log.exception", "alert.firing", "k8s.restarts"),
            topic="deployment",
            verification=("Diff the running revision against the previous one: image, configuration and the commits between them.",
                          "Check whether the symptom started within minutes of the rollout and on the new pods only."),
            remediation=(Rem("Roll back to the previous revision", "low; reverts every change in the release", "roll forward to the current revision", "rollback_deployment"),)),
    Pattern("oom_memory_limit", "Containers killed for exceeding their memory limit", "resource",
            "{service}'s containers exceed their memory limit and are OOMKilled",
            (Req("an OOM kill", ("k8s.oomkilled", "log.oom", "k8s.oomkilled_earlier"), ("describe_pod", "previous_logs")),),
            supporting=("metric.memory_near_limit", "k8s.restarts", "k8s.crashloop", "git.topic:memory", "kb.topic:memory", "history.cause:memory", "ml.anomaly:memory_utilization",
                        "config.no_memory_limit"),
            contradicted_by=(),
            causes=("deploy.rollout", "metric.traffic_surge", "git.topic:memory"),
            symptoms=("k8s.oomkilled", "k8s.restarts", "metric.memory_near_limit"),
            topic="memory",
            verification=("Compare the container's peak working set with its limit over several restarts.", "Check whether memory grows steadily (a leak) or jumps with traffic."),
            remediation=(Rem("Raise the memory limit to the observed peak plus headroom", "low; uses more node memory", "restore the previous limit", "patch_resources"),)),
    Pattern("crashloop_startup_failure", "Application fails at start-up", "application",
            "{service} exits during start-up, so its pods crash-loop",
            (Req("crash-looping or restarting pods", ("k8s.crashloop", "k8s.restarts"), ("pod_list",)),
             Req("a start-up error in its logs", ("log.config_error", "log.exception", "log.connection_refused", "log.dns_failure", "log.tls_error"), ("previous_logs",))),
            supporting=("deploy.rollout", "config.missing_ref", "k8s.config_error"),
            contradicted_by=("k8s.oomkilled",),
            causes=("deploy.rollout", "deploy.image_change"),
            symptoms=("k8s.crashloop", "k8s.restarts"),
            topic="config",
            symptom_of=("missing_config_reference", "dependency_failure", "dns_failure", "oom_memory_limit"),
            verification=("Read the previous container's logs in full: the last lines before exit name the failure.",),
            remediation=(Rem("Fix the configuration or dependency the start-up error names, then restart", "low", "revert the configuration change", "apply_config_patch"),
                         Rem("Roll back if the failure started with a rollout", "low", "roll forward", "rollback_deployment"))),
    Pattern("missing_config_reference", "Missing ConfigMap, Secret or required setting", "configuration",
            "{service} references configuration that does not exist or is not set",
            (Req("a configuration error", ("k8s.config_error", "config.missing_ref", "log.config_error"), ("config_audit", "describe_pod")),),
            supporting=("k8s.crashloop", "deploy.rollout"),
            causes=("deploy.rollout",),
            symptoms=("k8s.config_error", "k8s.crashloop"),
            topic="config",
            verification=("List the ConfigMaps and Secrets the pod spec references and check each exists in the namespace (names only).",),
            remediation=(Rem("Create the missing reference or correct its name in the manifest", "low", "remove the new object", "open_pull_request"),)),
    Pattern("image_pull_failure", "Image cannot be pulled", "deployment",
            "{service}'s image cannot be pulled: the tag, registry or pull credentials are wrong",
            (Req("an image pull error", ("k8s.image_pull",), ("describe_pod", "events")),),
            supporting=("deploy.image_change", "deploy.rollout"),
            causes=("deploy.image_change",),
            symptoms=("k8s.image_pull",),
            topic="image",
            verification=("Check the exact image reference in the pod spec and whether that tag exists in the registry.",),
            remediation=(Rem("Roll back to the last image that pulled", "low", "roll forward once the image exists", "rollback_deployment"),)),
    Pattern("cpu_saturation", "CPU saturation or throttling", "resource",
            "{service} is CPU-bound: throttling or saturated CPU slows its requests",
            (Req("CPU throttling (the CPU limit binding)", ("metric.cpu_throttling", "ml.anomaly:cpu_throttling"), ("pod_metrics",)),
             Req("a symptom", ("metric.latency_increase", "alert.firing", "k8s.probe_failed", "metric.error_rate_increase"), ("service_signals",))),
            supporting=("metric.cpu_increase", "metric.traffic_surge", "deploy.rollout", "kb.topic:cpu", "history.cause:cpu"),
            causes=("metric.cpu_throttling", "metric.cpu_increase", "metric.traffic_surge", "deploy.rollout"),
            symptoms=("metric.latency_increase", "k8s.probe_failed", "alert.firing"),
            topic="cpu",
            symptom_of=("traffic_surge",),
            verification=("Compare CPU usage with the CPU limit per pod at the onset.", "Check whether request volume rose at the same time."),
            remediation=(Rem("Raise the CPU limit or remove it, keeping a request", "low", "restore the previous limit", "patch_resources"),
                         Rem("Scale out within policy", "low; costs more", "scale back", "scale_deployment"))),
    Pattern("dependency_failure", "A dependency is failing or unreachable", "dependency",
            "{service} fails because a service it calls is unavailable or slow",
            (Req("errors reaching a dependency", ("log.connection_refused", "log.upstream_timeout", "trace.dependency_error", "k8s.service_no_ready_endpoints"), ("dependency_logs", "services")),
             Req("a symptom", ("metric.error_rate_increase", "metric.latency_increase", "log.exception", "alert.firing", "log.http_5xx"), ("service_signals",))),
            supporting=("trace.cross_service", "trace.error", "log.db_timeout", "kb.topic:dependency", "history.cause:dependency"),
            contradicted_by=("log.db_pool_exhausted", "metric.db_connections_saturated"),  # the service itself cannot get connections: its own pool, not the dependency
            causes=("log.connection_refused", "log.upstream_timeout", "k8s.service_no_ready_endpoints", "trace.dependency_error"),
            symptoms=("metric.error_rate_increase", "metric.latency_increase", "alert.firing"),
            topic="dependency",
            verification=("Check the dependency's own health and endpoints at the onset.", "Follow one failing request's trace id across services."),
            remediation=(Rem("Restore the dependency first; then restart callers stuck in back-off if they do not recover", "low", "none needed", "restart_deployment"),)),
    Pattern("dns_failure", "DNS resolution failure", "network",
            "{service} cannot resolve the names of services it calls",
            (Req("DNS errors or unhealthy CoreDNS", ("log.dns_failure", "dns.unhealthy"), ("dns",)),),
            supporting=("log.connection_refused", "kb.topic:dns", "history.cause:dns"),
            causes=("dns.unhealthy",),
            symptoms=("log.dns_failure",),
            topic="dns",
            verification=("Check CoreDNS pods and the kube-dns endpoints, then resolve the failing name from a diagnostic pod.",),
            remediation=(Rem("Restore CoreDNS capacity or fix the name the service uses", "medium; cluster-wide", "revert the DNS change"),)),
    Pattern("node_pressure", "Node pressure evicting or starving pods", "platform",
            "the nodes running {service} are under resource pressure and evict or starve its pods",
            (Req("node pressure or evictions", ("node.pressure", "k8s.evicted"), ("nodes",)),),
            supporting=("k8s.restarts", "metric.memory_near_limit", "kb.topic:node"),
            causes=("node.pressure",),
            symptoms=("k8s.evicted", "k8s.restarts"),
            topic="node",
            verification=("Check node conditions and allocatable capacity against requests on the affected nodes.",),
            remediation=(Rem("Add node capacity or move workloads off the pressured nodes", "medium", "none needed"),)),
    Pattern("scheduling_capacity", "Pods cannot be scheduled", "platform",
            "{service}'s pods cannot be scheduled: requests, affinity or taints exceed what the nodes offer",
            (Req("unschedulable pods", ("k8s.unschedulable", "k8s.pending"), ("events",)),),
            supporting=("node.pressure", "kb.topic:scheduling"),
            causes=("deploy.rollout",),
            symptoms=("k8s.unschedulable",),
            topic="scheduling",
            verification=("Read the FailedScheduling message: it names the constraint that no node meets.",),
            remediation=(Rem("Lower the requests, relax affinity or add capacity", "low", "restore the previous requests", "patch_resources"),)),
    Pattern("readiness_failure", "Pods fail their readiness or liveness probes", "application",
            "{service}'s pods fail their probes, so they are taken out of the Service or restarted",
            (Req("probe failures or unready pods", ("k8s.probe_failed", "k8s.not_ready"), ("events",)),),
            supporting=("metric.latency_increase", "metric.cpu_throttling", "k8s.restarts", "kb.topic:probe"),
            causes=("metric.cpu_throttling", "deploy.rollout"),
            symptoms=("k8s.probe_failed", "k8s.not_ready"),
            topic="probe",
            symptom_of=("cpu_saturation", "connection_pool_exhaustion", "dependency_failure", "oom_memory_limit", "traffic_surge", "dns_failure"),
            verification=("Compare probe timeouts with the endpoint's response time at the onset.",),
            remediation=(Rem("Fix what makes the endpoint slow, or loosen the probe's timeout", "low", "restore the probe", "apply_config_patch"),)),
    Pattern("traffic_surge", "Traffic surge beyond capacity", "capacity",
            "a surge in requests to {service} exceeded what it was sized for",
            (Req("a rise in request rate", ("metric.traffic_surge", "ml.anomaly:request_rate"), ("service_signals",)),
             Req("a symptom", ("metric.latency_increase", "metric.error_rate_increase", "metric.cpu_increase", "metric.cpu_throttling"), ("service_signals", "pod_metrics"))),
            supporting=("ml.anomaly", "kb.topic:traffic", "history.cause:traffic"),
            contradicted_by=("metric.traffic_drop",),
            causes=("metric.traffic_surge",),
            symptoms=("metric.latency_increase", "metric.error_rate_increase", "metric.cpu_throttling"),
            topic="traffic",
            verification=("Find where the extra requests come from (client, route) and whether they are legitimate.",),
            remediation=(Rem("Scale out within policy", "low; costs more", "scale back", "scale_deployment"),)),
)
BY_KEY = {p.key: p for p in PATTERNS}


@dataclass
class Group:
    label: str
    present: list[Signal] = field(default_factory=list)
    absent: list[Signal] = field(default_factory=list)
    probes: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return bool(self.present)

    @property
    def observed_absent(self) -> bool:
        return not self.present and bool(self.absent)


@dataclass
class Hypothesis:
    id: str
    pattern: str            # a PATTERNS key, or "agent" for one an agent proposed outside the library
    title: str
    category: str
    statement: str
    groups: list[Group]
    supporting: list[Signal]
    contradicting: list[Signal]
    origin: str = "rules"   # rules | agent | rules+agent
    agent_refs: list[str] = field(default_factory=list)  # what an agent cited for it
    agent_contradicting: list[str] = field(default_factory=list)  # what the reviewer cited against it
    review: dict[str, Any] = field(default_factory=dict)
    score: dict[str, Any] = field(default_factory=dict)
    explained_by: dict[str, Any] | None = None  # set by scoring.explain_away

    @property
    def refs(self) -> list[str]:
        out: list[str] = []
        for s in [*(x for g in self.groups for x in g.present), *self.supporting]:
            if s.ref not in out:
                out.append(s.ref)
        for r in self.agent_refs:
            if r not in out:
                out.append(r)
        return out

    @property
    def against(self) -> list[str]:
        return list(dict.fromkeys([s.ref for s in self.contradicting] + self.agent_contradicting))

    @property
    def missing(self) -> list[Group]:
        return [g for g in self.groups if not g.satisfied]


def _index(signals: list[Signal]) -> tuple[dict[str, list[Signal]], dict[str, list[Signal]]]:
    present: dict[str, list[Signal]] = {}
    absent: dict[str, list[Signal]] = {}
    for s in signals:
        (present if s.present else absent).setdefault(s.key, []).append(s)
    return present, absent


def _match(p: Pattern, present: dict[str, list[Signal]], absent: dict[str, list[Signal]]) -> tuple[list[Group], list[Signal], list[Signal]]:
    groups = [Group(r.label, [s for k in r.any_of for s in present.get(k, [])], [s for k in r.any_of for s in absent.get(k, [])], r.probes) for r in p.required]
    used = {id(s) for g in groups for s in g.present}
    supporting = [s for k in p.supporting for s in present.get(k, []) if id(s) not in used]
    contradicting = [s for k in p.contradicted_by for s in present.get(k, [])]
    contradicting += [s for g in groups if g.observed_absent for s in g.absent]  # absent support is not a contradiction; an absent requirement is
    return groups, list(dict.fromkeys(supporting)), list(dict.fromkeys(contradicting))


def generate(signals: list[Signal], service: str) -> list[Hypothesis]:
    """Every library pattern that at least one required group of the evidence satisfies."""
    present, absent = _index(signals)
    out: list[Hypothesis] = []
    for p in PATTERNS:
        groups, supporting, contradicting = _match(p, present, absent)
        if not any(g.satisfied for g in groups):
            continue
        out.append(Hypothesis(id=f"H{len(out) + 1}", pattern=p.key, title=p.title, category=p.category, statement=p.statement.format(service=service or "the service"),
                              groups=groups, supporting=supporting, contradicting=contradicting))
    return out


def rematch(h: Hypothesis, signals: list[Signal], service: str) -> Hypothesis:
    """The same hypothesis against a larger ledger (after more evidence was collected)."""
    p = BY_KEY.get(h.pattern)
    if p is None:
        return h
    present, absent = _index(signals)
    h.groups, h.supporting, h.contradicting = _match(p, present, absent)
    return h


def from_agent(raw: dict[str, Any], known: list[Hypothesis], valid: dict[str, Any], service: str) -> tuple[Hypothesis | None, list[str]]:
    """An agent's proposal, held to the ledger: citations it invented are dropped and returned; a proposal left with
    no valid citation is not a hypothesis. A proposal of a library pattern merges into that pattern's hypothesis."""
    refs = [str(r) for r in raw.get("evidence") or [] if isinstance(r, (str, int))]
    rejected = [r for r in refs if r not in valid]
    refs = [r for r in refs if r in valid and valid[r].status in ("ok", "partial")]
    if not refs:
        return None, rejected
    key = str(raw.get("pattern") or "")
    existing = next((h for h in known if h.pattern == key), None)
    if existing is not None:
        existing.agent_refs = list(dict.fromkeys([*existing.agent_refs, *refs]))
        existing.origin = "rules+agent" if existing.origin == "rules" else existing.origin
        return existing, rejected
    title = " ".join(str(raw.get("title") or "").split())[:140]
    statement = " ".join(str(raw.get("statement") or title).split())[:600]
    if not title:
        return None, rejected
    if key in BY_KEY:
        p = BY_KEY[key]
        h = Hypothesis(id=f"H{len(known) + 1}", pattern=key, title=p.title, category=p.category, statement=p.statement.format(service=service or "the service"),
                       groups=[Group(r.label, probes=r.probes) for r in p.required], supporting=[], contradicting=[], origin="agent", agent_refs=refs)
    else:
        h = Hypothesis(id=f"H{len(known) + 1}", pattern="agent", title=title, category=str(raw.get("category") or "other")[:40], statement=statement,
                       groups=[], supporting=[], contradicting=[], origin="agent", agent_refs=refs)
    return h, rejected
