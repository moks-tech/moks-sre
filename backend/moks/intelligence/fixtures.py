"""Recorded incident scenarios for the benchmark and the tests: Kubernetes, Prometheus, Loki and Git connectors that
serve a scenario's state through the live connectors' interfaces, so every engine runs its real code path (the
ToolExecutor, the summarizers, the detectors) against it. Knowledge documents, approved past incidents and anomaly
observations are written to the database in the evaluation realm, under the run's own environment name, and removed
afterwards; production data is never read or written.

A scenario (evals/datasets/investigation_benchmark.yaml) names a namespace, workloads with their rollouts, pods,
Services, events, log lines, metric shapes ({base, after, change_minutes_ago} per workload), commits, alerts, anomaly
observations, documents and past incidents, and what a correct investigation concludes.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..connectors.base import ConnectorError
from ..connectors.kubernetes import describe_containers, summarize_pod, summarize_workload


def _ago(now: datetime, minutes: float) -> str:
    return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(*parts: Any, n: int = 9) -> str:
    h = hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()
    return "".join(c if c.isalpha() else "bcdfghjkmn"[int(c)] for c in h)[:n]


class ScenarioKubernetes:
    type = "kubernetes"

    def __init__(self, scenario: dict[str, Any], now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)
        self.ns = scenario["namespace"]
        f = scenario.get("fixture") or {}
        self.f = f
        self.failing = set(scenario.get("fail_tools") or [])
        self.rs: dict[str, list[dict]] = {}
        self.pods: list[dict] = []
        for d in f.get("deployments") or []:
            revs = (f.get("rollouts") or {}).get(d["name"]) or [{"revision": 1, "minutes_ago": 5000, "images": [d.get("image", f"example/{d['name']}:1")]}]
            self.rs[d["name"]] = [{**r, "name": f"{d['name']}-{_hash(d['name'], r['revision'])}", "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{d['name']}/{r['revision']}"))} for r in revs]
        for p in f.get("pods") or []:
            self.pods.append(self._pod(p))

    def _pod(self, p: dict) -> dict:
        dep = p.get("deployment")
        rs = None
        if dep:
            revs = sorted(self.rs.get(dep, []), key=lambda r: -r["revision"])
            rs = next((r for r in revs if r["revision"] == p.get("revision")), revs[0] if revs else None)
        name = p.get("name") or f"{dep}-{rs['name'].rsplit('-', 1)[1] if rs else _hash(dep)}-{_hash(dep, p.get('index', 0), n=5)}"
        cs: dict[str, Any] = {"name": "app", "ready": p.get("ready", True), "restartCount": p.get("restarts", 0), "state": {"running": {}}}
        if p.get("waiting"):
            cs["state"] = {"waiting": {"reason": p["waiting"], "message": p.get("message", "")}}
            cs["ready"] = False
        if p.get("last"):
            cs["lastState"] = {"terminated": {"reason": p["last"], "exitCode": p.get("exit_code", 137 if p["last"] == "OOMKilled" else 1), "finishedAt": _ago(self.now, p.get("last_minutes_ago", 5))}}
        owners = [{"kind": "ReplicaSet", "name": rs["name"], "uid": rs["uid"], "controller": True}] if rs else []
        image = (rs or {}).get("images", [p.get("image", "example/app:1")])[0]
        return {"metadata": {"name": name, "namespace": self.ns, "labels": {"app": dep or name, **(p.get("labels") or {})}, "creationTimestamp": _ago(self.now, p.get("age_minutes", 600)),
                             "ownerReferences": owners},
                "spec": {"nodeName": p.get("node", "node-1"), "containers": [{"name": "app", "image": image, "ports": [{"name": "http", "containerPort": 8080}],
                                                                             "resources": p.get("resources", {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}),
                                                                             "env": p.get("env", []), "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}}}]},
                "status": {"phase": p.get("phase", "Running"), "containerStatuses": [cs], "conditions": []}}

    async def aclose(self) -> None:
        pass

    def _check(self, ns: str) -> None:
        if ns != self.ns:
            raise ConnectorError("not_found", f"namespace {ns} not found")

    def _fail(self, tool: str) -> None:
        if tool in self.failing:
            raise ConnectorError("unavailable", "the API server did not answer")

    async def list_namespaces(self) -> list[str]:
        return [self.ns]

    async def list_pods(self, namespace, label_selector=None):
        self._check(namespace)
        self._fail("k8s_list_pods")
        return [summarize_pod(p) for p in self.pods], False

    async def raw_pods(self, namespace):
        self._check(namespace)
        return self.pods

    async def describe_pod(self, namespace, name):
        self._check(namespace)
        p = next((p for p in self.pods if p["metadata"]["name"] == name), None)
        if p is None:
            raise ConnectorError("not_found", f"pod {name} not found")
        return {"summary": summarize_pod(p), "containers": describe_containers(p), "conditions": [], "node": p["spec"]["nodeName"], "events": []}

    async def pod_logs(self, namespace, pod, container, previous, tail_lines, since_seconds):
        self._check(namespace)
        self._fail("k8s_pod_logs")
        logs = self.f.get("pod_logs") or {}
        key = next((k for k in logs if pod.startswith(k)), "*")
        return "\n".join(str(x) for x in (logs.get(key) or [])[-tail_lines:]) if isinstance(logs.get(key), list) else str(logs.get(key, ""))

    async def events(self, namespace, involved_name=None, involved_kind=None, warnings_only=True, limit=100):
        self._check(namespace)
        out = []
        for e in self.f.get("events") or []:
            obj = e["object"]
            if obj.startswith("Pod/") and not obj[4:] in {p["metadata"]["name"] for p in self.pods}:
                obj = next((f"Pod/{p['metadata']['name']}" for p in self.pods if p["metadata"]["name"].startswith(obj[4:])), obj)
            out.append({"type": e.get("type", "Warning"), "reason": e["reason"], "object": obj, "message": e.get("message", ""), "count": e.get("count", 1),
                        "firstSeen": _ago(self.now, e.get("minutes_ago", 2) + 1), "lastSeen": _ago(self.now, e.get("minutes_ago", 2))})
        return [e for e in out if not involved_name or e["object"].endswith("/" + involved_name)][:limit]

    async def workloads(self, namespace):
        self._check(namespace)
        out = []
        for d in self.f.get("deployments") or []:
            revs = sorted(self.rs[d["name"]], key=lambda r: -r["revision"])
            out.append(summarize_workload("Deployment", {
                "metadata": {"name": d["name"], "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, d["name"])), "generation": 2,
                             "annotations": {"deployment.kubernetes.io/revision": str(revs[0]["revision"])}},
                "spec": {"replicas": d.get("replicas", 1), "selector": {"matchLabels": {"app": d["name"]}}, "template": {"spec": {"containers": [{"image": revs[0]["images"][0]}]}}},
                "status": {"readyReplicas": d.get("ready", d.get("replicas", 1)), "observedGeneration": 2}}))
        return out

    async def rollout_history(self, namespace, deployment):
        self._check(namespace)
        if deployment not in self.rs:
            raise ConnectorError("not_found", f"deployment {deployment} not found")
        return {"deployment": {"name": deployment}, "revisions": [{"revision": r["revision"], "created": _ago(self.now, r["minutes_ago"]), "images": r["images"],
                                                                   "replicaSet": r["name"], "uid": r["uid"]} for r in sorted(self.rs[deployment], key=lambda r: -r["revision"])]}

    async def deployment_meta(self, namespace, name):
        self._check(namespace)
        revs = sorted(self.rs.get(name) or [], key=lambda r: -r["revision"])
        if not revs:
            raise ConnectorError("not_found", f"deployment {name} not found")
        return {"name": name, "namespace": namespace, "resourceVersion": "1", "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, name)), "generation": 2,
                "annotations": (self.f.get("annotations") or {}).get(name, {}), "labels": {"app": name}, "images": revs[0]["images"], "replicas": 1,
                "summary": {"revision": str(revs[0]["revision"])}}

    async def services(self, namespace):
        self._check(namespace)
        out = []
        for s in self.f.get("services") or []:
            matching = [p for p in self.pods if all(p["metadata"]["labels"].get(k) == v for k, v in s["selector"].items())]
            ready = sum(1 for p in matching if p["status"]["containerStatuses"][0]["ready"])
            out.append({"name": s["name"], "type": "ClusterIP", "selector": s["selector"], "ports": [{"name": "http", "port": 80, "targetPort": "http"}],
                        "endpoints": {"ready": ready, "notReady": len(matching) - ready}, "matchingPods": [p["metadata"]["name"] for p in matching],
                        "matchingPodCount": len(matching), "podContainerPorts": [("http", 8080)] if matching else [], "nearMissPods": []})
        return out

    async def ingresses(self, namespace):
        return [{"name": i["name"], "class": None, "annotations": {}, "rules": [{"host": i.get("host"), "path": i.get("path", "/"), "pathType": "Prefix", "service": i["service"], "port": 80}],
                 "tls": []} for i in self.f.get("ingresses") or []]  # the live connector's shape: one flat rule per host and path

    async def nodes(self):
        return self.f.get("nodes") or [{"name": "node-1", "ready": True, "pressure": [], "unschedulable": False, "taints": [], "allocatable": {"cpu": "4", "memory": "16Gi"}}]

    async def dns_health(self):
        return {"deployment": {}, "services": [], "pods": [{"name": "coredns-1", "ready": "1/1", "phase": "Running"}]}


def _series(shape: dict, start: datetime, end: datetime, step: int, now: datetime) -> list[list[float]]:
    """{base, after, change_minutes_ago, noise} sampled every step over [start, end]."""
    base, after = float(shape.get("base", 0)), float(shape.get("after", shape.get("base", 0)))
    change = now - timedelta(minutes=float(shape.get("change_minutes_ago", 10**6)))
    noise = float(shape.get("noise", 0.03))
    out, t, i = [], start, 0
    while t <= end:
        v = after if t >= change else base
        jitter = ((i * 7919) % 13 - 6) / 6 * noise * (abs(v) or 1)
        out.append([t.timestamp(), round(max(0.0, v + jitter), 6)])
        t += timedelta(seconds=step)
        i += 1
    return out


SIGNAL_OF_QUERY = (("histogram_quantile(0.95", "latency_p95"), ("container_memory_working_set_bytes", "memory_working_set_bytes"),
                   ("container_spec_memory_limit_bytes", "memory_limit_bytes"), ("cfs_throttled_periods_total", "cpu_throttled_ratio"),
                   ("container_cpu_usage_seconds_total", "cpu_cores"), ("connections_max", "db_connections_max"), ("connections_usage", "db_connections"))


class ScenarioPrometheus:
    type = "prometheus"

    def __init__(self, scenario: dict[str, Any], k8s: ScenarioKubernetes, now: datetime | None = None):
        self.now = now or k8s.now
        self.metrics = (scenario.get("fixture") or {}).get("metrics") or {}
        self.alerts_def = (scenario.get("fixture") or {}).get("alerts") or []
        self.k8s = k8s
        self.ns = scenario["namespace"]
        self.failing = set(scenario.get("fail_tools") or [])

    async def aclose(self) -> None:
        pass

    def graph_link(self, promql: str, minutes: int) -> str | None:
        return None

    def _signal(self, promql: str) -> str | None:
        if re.search(r'5\.\.', promql):
            return "errors_rate"
        for needle, name in SIGNAL_OF_QUERY:
            if needle in promql:
                return name
        return "request_rate" if "_total" in promql and "rate(" in promql else None

    async def range(self, promql: str, start: datetime, end: datetime, step_seconds: int) -> dict[str, Any]:
        if "prometheus" in self.failing:
            raise ConnectorError("unavailable", "Prometheus did not answer")
        name = self._signal(promql)
        per_wl = self.metrics.get(name) or {}
        series = []
        pod_level = name in ("memory_working_set_bytes", "memory_limit_bytes", "cpu_throttled_ratio", "cpu_cores")
        for wl, shape in per_wl.items():
            if pod_level:
                for p in self.k8s.pods:
                    pname = p["metadata"]["name"]
                    if pname.startswith(wl + "-") and re.search(r'pod=~"([^"]+)"', promql) and re.fullmatch(re.search(r'pod=~"([^"]+)"', promql).group(1), pname):
                        series.append({"metric": {"pod": pname, "container": "app"}, "values": _series(shape, start, end, step_seconds, self.now)})
            else:
                series.append({"metric": {"workload": wl}, "values": _series(shape, start, end, step_seconds, self.now)})
        return {"resultType": "matrix", "series": series, "warnings": []}

    async def instant(self, promql: str, at: datetime) -> dict[str, Any]:
        r = await self.range(promql, at - timedelta(minutes=1), at, 60)
        return {"resultType": "vector", "series": [{"metric": s["metric"], "value": s["values"][-1]} for s in r["series"] if s["values"]], "warnings": []}

    async def alerts(self) -> list[dict[str, Any]]:
        return [{"group": "app", "name": a["name"], "state": "firing", "query": "", "for": "5m", "labels": {"severity": a.get("severity", "warning")}, "annotations": {},
                 "active": [{"labels": {"alertname": a["name"], "namespace": self.ns, "severity": a.get("severity", "warning"), **({"service": a["service"]} if a.get("service") else {})},
                             "state": "firing", "activeAt": _ago(self.now, a.get("minutes_ago", 10)), "value": "1"}]} for a in self.alerts_def]

    async def targets(self) -> list[dict[str, Any]]:
        return []


class ScenarioLoki:
    type = "loki"
    namespace_label = "namespace"

    def __init__(self, scenario: dict[str, Any], k8s: ScenarioKubernetes, now: datetime | None = None):
        self.now = now or k8s.now
        self.k8s = k8s
        self.lines = (scenario.get("fixture") or {}).get("loki") or []
        self.failing = set(scenario.get("fail_tools") or [])

    async def aclose(self) -> None:
        pass

    async def labels(self, start: datetime, end: datetime, query: str | None = None) -> list[str]:
        return ["namespace", "pod", "container"]

    async def label_values(self, label: str, start: datetime, end: datetime, query: str | None = None) -> list[str]:
        if label == "namespace":
            return [self.k8s.ns]
        if label == "pod":
            return [p["metadata"]["name"] for p in self.k8s.pods]
        return ["app"] if label == "container" else []

    async def format_query(self, logql: str) -> str | None:
        return logql

    def _pod_name(self, prefix: str) -> str:
        return next((p["metadata"]["name"] for p in self.k8s.pods if p["metadata"]["name"].startswith(prefix)), prefix)

    async def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 200, direction: str = "backward") -> dict[str, Any]:
        if "loki" in self.failing:
            raise ConnectorError("unavailable", "Loki did not answer")
        pod_rx = re.search(r'pod(=~|=)"([^"]+)"', logql)
        contains = re.findall(r'\|= "([^"]+)"', logql)
        regexes = re.findall(r'\|~ "((?:[^"\\]|\\.)+)"', logql)
        rows = []
        for ln in self.lines:
            pod = self._pod_name(ln["pod"])
            at = self.now - timedelta(minutes=ln.get("minutes_ago", 5))
            if not start <= at <= end:
                continue
            if pod_rx and not (re.fullmatch(pod_rx.group(2).replace("\\\\", "\\"), pod) if pod_rx.group(1) == "=~" else pod == pod_rx.group(2)):
                continue
            text = ln["line"]
            if any(c.lower() not in text.lower() for c in contains):
                continue
            if any(not re.search(r.replace('\\"', '"').replace("\\\\", "\\"), text) for r in regexes):
                continue
            rows.append({"ts": at.isoformat(), "labels": {"namespace": self.k8s.ns, "pod": pod, "container": "app"}, "line": text})
        rows.sort(key=lambda r: r["ts"], reverse=direction == "backward")
        return {"resultType": "streams", "lines": rows[:limit], "stats": {}}


class ScenarioGit:
    type = "git"

    def __init__(self, scenario: dict[str, Any], now: datetime | None = None):
        self.now = now or datetime.now(timezone.utc)
        self.repositories = [scenario.get("repository", "acme/shop")]
        self.commits = (scenario.get("fixture") or {}).get("commits") or []

    async def aclose(self) -> None:
        pass

    def check_repo(self, repo: str) -> None:
        if repo not in self.repositories:
            raise ConnectorError("forbidden", f"repository {repo} is not allowed")

    async def recent_commits(self, repo: str, path: str | None, since: datetime | None, limit: int) -> list[dict]:
        self.check_repo(repo)
        out = []
        for c in self.commits:
            at = self.now - timedelta(minutes=c.get("minutes_ago", 60))
            if since and at < since:
                continue
            sha = hashlib.sha1(c["message"].encode()).hexdigest()
            out.append({"sha": sha, "short": sha[:8], "message": c["message"], "author": c.get("author", "dev"), "date": at.isoformat(), "files": c.get("files", [])})
        return out[:limit]

    async def compare_deployment(self, repo: str, images: list[str], annotations: dict[str, str]) -> dict:
        self.check_repo(repo)
        return {"repo": repo, "resolved": False, "images": [{"image": i} for i in images], "reason": "No commit SHA in the image tags or revision annotations."}


def connectors(scenario: dict[str, Any], now: datetime | None = None) -> dict[str, tuple[None, Any]]:
    """The connectors_override an Investigator takes. Only the sources the scenario has are configured."""
    k8s = ScenarioKubernetes(scenario, now)
    out: dict[str, tuple[None, Any]] = {"kubernetes": (None, k8s)}
    f = scenario.get("fixture") or {}
    if f.get("metrics") is not None or f.get("alerts") is not None:
        out["prometheus"] = (None, ScenarioPrometheus(scenario, k8s))
    if f.get("loki") is not None:
        out["loki"] = (None, ScenarioLoki(scenario, k8s))
    if f.get("commits") is not None:
        out["git"] = (None, ScenarioGit(scenario, k8s.now))
    return out


async def seed(scenario: dict[str, Any], environment: str, tag: str, now: datetime | None = None) -> None:
    """Documents, approved past incidents and anomaly observations, in the evaluation realm and this run's environment."""
    from ..db import session_scope
    from ..ml import learning
    from ..models import KnowledgeSource, LearningRecord, MLObservation
    from ..rag import access, service

    now = now or datetime.now(timezone.utc)
    f = scenario.get("fixture") or {}
    ns = scenario["namespace"]
    async with session_scope() as db:
        if f.get("documents"):
            src = KnowledgeSource(name=f"eval-bench-{tag}", type="upload", config={}, scope_environment=environment, scope_namespace=ns, classification="internal", realm=access.EVALUATION)
            db.add(src)
            await db.flush()
            for d in f["documents"]:
                await service.index_document(db, src, d["path"], d["text"], revision=tag, url=None, extra={"service": d.get("service")})
        for inc in f.get("incidents") or []:
            db.add(LearningRecord(id=uuid.uuid4(), schema_version=learning.SCHEMA_VERSION, kind="incident", incident_id=None, environment=environment, namespace=ns,
                                  service=inc.get("service"), realm="evaluation", event_from=now - timedelta(days=inc.get("days_ago", 30)), features=inc["features"],
                                  label={"confirmed_cause": inc["cause"], "resolution": inc.get("resolution", ""), "verification": "verified", "source": f"eval:{tag}"},
                                  label_state="approved", outcome={"resolution": "resolved"}, versions={"feature_schema": learning.FEATURE_SCHEMA},
                                  fingerprint=uuid.uuid4().hex, content_hash=uuid.uuid4().hex))
        for o in f.get("ml") or []:
            at = now - timedelta(minutes=o.get("minutes_ago", 5))
            db.add(MLObservation(id=uuid.uuid4(), capability="anomaly_detection", model_id=None, algorithm=o.get("algorithm", "seasonal_baseline"), environment=environment,
                                 namespace=ns, service=o["service"], cohort=f"{environment}/{ns}/{o['service']}", window_from=at - timedelta(minutes=60), window_to=at, observed_at=at,
                                 status=o.get("status", "scored"), mode=o.get("mode", "shadow"), score=o.get("score"), threshold=o.get("threshold", 4.0), unusual=bool(o.get("unusual", True)),
                                 deviations=[{"feature": d["feature"], "z": d["z"], "observed": d.get("observed"), "typical": d.get("typical"), "direction": "higher" if d["z"] > 0 else "lower"}
                                             for d in o.get("deviations") or []],
                                 data_quality={}, context={"model_version": o.get("model_version", 1)}, feedback={}, fingerprint=uuid.uuid4().hex))


async def cleanup(environment: str, tag: str) -> None:
    from sqlalchemy import delete

    from ..db import session_scope
    from sqlalchemy import select

    from ..models import Document, KnowledgeSource, LearningRecord, MLObservation
    from ..rag import access

    async with session_scope() as db:  # rows nothing else references first, so a later failure cannot keep them
        await db.execute(delete(MLObservation).where(MLObservation.environment == environment))
        await db.execute(delete(LearningRecord).where(LearningRecord.environment == environment, LearningRecord.realm == "evaluation"))
    async with session_scope() as db:
        mine = select(KnowledgeSource.id).where(KnowledgeSource.name == f"eval-bench-{tag}", KnowledgeSource.realm == access.EVALUATION).scalar_subquery()
        # documents before their source: (source_id, realm) is not ON DELETE CASCADE, and which RI trigger fires first depends on the database
        await db.execute(delete(Document).where(Document.source_id == mine))
        await db.execute(delete(KnowledgeSource).where(KnowledgeSource.name == f"eval-bench-{tag}", KnowledgeSource.realm == access.EVALUATION))
