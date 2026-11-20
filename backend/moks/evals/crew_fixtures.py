"""Fixture connectors for the CrewAI incident scenarios (datasets/crewai_incidents.yaml): Kubernetes, Prometheus, Loki and
Git serving a case's recorded state through the same interfaces as the live connectors, so every tool, the evidence
registry and the validation run exactly as they do against a cluster. Nothing here reaches a network.

Times in a case are minutes before now (`minutes_ago`), so a scenario keeps its shape whenever it runs.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..connectors.base import ConnectorError
from ..connectors.kubernetes import summarize_hpa, summarize_scaled_object, summarize_workload
from .fixture import FixtureKubernetes


def _ago(minutes: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _iso(minutes: float) -> str:
    return _ago(minutes).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rs(pod: str) -> str:
    return pod.rsplit("-", 1)[0]


class CrewFixtureKubernetes(FixtureKubernetes):
    """The evaluation fixture with what the incident crew also reads: owner references with UIDs (so the target's pods
    resolve as they do live), events with times, HPAs, KEDA ScaledObjects, claims and nodes."""

    def __init__(self, case: dict[str, Any]):
        super().__init__(case)
        f = case["fixture"]
        for raw, spec in zip(self.pods, f.get("pods", [])):
            owner = spec.get("owner") or _rs(spec["name"])
            raw["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": owner, "uid": f"uid-{owner}", "controller": True}]
            raw["spec"]["nodeName"] = spec.get("node", "w1")
            if spec.get("volumes"):
                raw["spec"]["volumes"] = [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in spec["volumes"]]
        self.hpas_def = f.get("hpas", [])
        self.scaled_def = f.get("scaled_objects")
        self.pvcs_def = f.get("pvcs", [])
        self.nodes_def = f.get("nodes")

    async def events(self, namespace, involved_name=None, involved_kind=None, warnings_only=True, limit=100):
        self._check(namespace)
        rows = [{"type": e.get("type", "Warning"), "reason": e["reason"], "object": e["object"], "message": e.get("message", ""), "count": e.get("count", 1),
                 "firstSeen": _iso(e.get("first_minutes_ago", e.get("minutes_ago", 2))), "lastSeen": _iso(e.get("minutes_ago", 2))} for e in self.events_def]
        if involved_name:
            rows = [r for r in rows if r["object"].split("/", 1)[-1] == involved_name or r["object"].split("/", 1)[-1].startswith(involved_name + "-")]
        return [r for r in rows if not warnings_only or r["type"] == "Warning"][:limit]

    async def workloads(self, namespace, kinds=None):
        self._check(namespace)
        return [summarize_workload("Deployment", {
            "metadata": {"name": d["name"], "uid": f"uid-{d['name']}", "generation": 2, "annotations": {"deployment.kubernetes.io/revision": str(d.get("revision", 1))}},
            "spec": {"replicas": d["replicas"], "selector": {"matchLabels": {"app": d["name"]}}, "template": {"spec": {"containers": [{"image": d.get("image", "example/app:1")}]}}},
            "status": {"readyReplicas": d["ready"], "observedGeneration": 2, "conditions": d.get("conditions", [])}}) for d in self.deployments]

    async def rollout_history(self, namespace, deployment):
        """Revisions as the case gives them; the ReplicaSet names follow the pods' owners, newest revision first."""
        self._check(namespace)
        owners = sorted({p["metadata"]["ownerReferences"][0]["name"] for p in self.pods if p["metadata"]["labels"].get("app") == deployment})
        revs = sorted(self.rollouts.get(deployment, [{"revision": 1, "minutes_ago": 4000, "images": ["example/app:1"]}]), key=lambda r: -r["revision"])
        out = []
        for i, r in enumerate(revs):
            rs = r.get("replica_set") or (owners[i] if i < len(owners) else f"{deployment}-r{r['revision']}")
            out.append({"revision": r["revision"], "replicaSet": rs, "uid": f"uid-{rs}", "created": _iso(r["minutes_ago"]), "images": r["images"],
                        "replicas": r.get("replicas", 0), "readyReplicas": r.get("ready", 0), "changeCause": r.get("change_cause")})
        return {"deployment": {"name": deployment, "uid": f"uid-{deployment}"}, "revisions": out}

    async def hpas(self, namespace):
        self._check(namespace)
        return [summarize_hpa({"metadata": {"name": h["name"]}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": h["target"]}, "minReplicas": h.get("min", 1),
                                                                         "maxReplicas": h["max"], "metrics": h.get("metrics", [])},
                               "status": {"currentReplicas": h["current"], "desiredReplicas": h.get("desired", h["current"]), "conditions": h.get("conditions", [])}})
                for h in self.hpas_def]

    async def scaled_objects(self, namespace):
        self._check(namespace)
        if self.scaled_def is None:
            return None
        return [summarize_scaled_object({"metadata": {"name": o["name"], "annotations": {"autoscaling.keda.sh/paused": "true"} if o.get("paused") else {}},
                                         "spec": {"scaleTargetRef": {"name": o["target"]}, "minReplicaCount": o.get("min", 0), "maxReplicaCount": o.get("max", 10),
                                                  "triggers": [{"type": t} for t in o.get("triggers", ["rabbitmq"])]},
                                         "status": {"conditions": o.get("conditions", [])}}) for o in self.scaled_def]

    async def pvcs(self, namespace):
        self._check(namespace)
        return [{"name": c["name"], "storage_class": c.get("storage_class", "standard"), "request": c.get("request", "1Gi"), "capacity": c.get("capacity"),
                 "phase": c.get("phase", "Bound"), "access_modes": ["ReadWriteOnce"], "volume": c.get("volume"), "conditions": c.get("conditions", [])} for c in self.pvcs_def]

    async def nodes(self):
        return self.nodes_def or await super().nodes()

    async def describe_pod(self, namespace, name):
        out = await super().describe_pod(namespace, name)
        spec = next((p for p in self.case_pods if p["name"] == name), {})
        out["events"] = [{"reason": e["reason"], "message": e.get("message", "")} for e in self.events_def if e["object"].endswith("/" + name)]
        out["node"] = spec.get("node", "w1")
        return out

    @property
    def case_pods(self) -> list[dict]:
        return [{"name": p["metadata"]["name"], "node": p["spec"].get("nodeName")} for p in self.pods]


class FixturePrometheus:
    """Series chosen by a substring of the PromQL (`match`), each a list of [minutes_ago, value]; alert rules as recorded."""
    type = "prometheus"

    def __init__(self, case: dict[str, Any]):
        p = case["fixture"].get("prometheus") or {}
        self.series = p.get("series", [])
        self.rules = p.get("alerts", [])

    async def aclose(self):
        pass

    def graph_link(self, promql: str, minutes: int) -> str | None:
        return None

    def _matching(self, promql: str) -> list[dict]:
        return [s for s in self.series if s["match"] in promql and all(x not in promql for x in s.get("unless", []))]

    async def range(self, promql: str, start: datetime, end: datetime, step_seconds: int) -> dict[str, Any]:
        out = [{"metric": s.get("metric", {}), "values": [[_ago(m).timestamp(), float(v)] for m, v in s["values"]]} for s in self._matching(promql)]
        return {"resultType": "matrix", "series": out, "warnings": []}

    async def instant(self, promql: str, at: datetime) -> dict[str, Any]:
        out = [{"metric": s.get("metric", {}), "value": [at.timestamp(), float(s["values"][-1][1])]} for s in self._matching(promql) if s["values"]]
        return {"resultType": "vector", "series": out, "warnings": []}

    async def alerts(self) -> list[dict[str, Any]]:
        return [{"group": "fixture", "name": a["name"], "state": "firing", "query": a.get("query", ""), "for": "5m", "labels": {"severity": a.get("severity", "warning")},
                 "annotations": {"summary": a.get("summary", "")}, "active": [{"labels": {"namespace": a.get("namespace"), **a.get("labels", {})}, "state": "firing",
                                                                                 "activeAt": _iso(a.get("minutes_ago", 10)), "value": str(a.get("value", 1))}]}
                for a in self.rules]

    async def targets(self) -> list[dict[str, Any]]:
        return []


class FixtureLoki:
    """Log lines with pods and times; a LogQL query keeps the lines of the pods it names and the text it filters on."""
    type = "loki"
    namespace_label = "namespace"

    def __init__(self, case: dict[str, Any]):
        self.ns = case["namespace"]
        self.lines = case["fixture"].get("loki", [])

    async def aclose(self):
        pass

    async def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 200, direction: str = "backward") -> dict[str, Any]:
        if f'namespace="{self.ns}"' not in logql:
            return {"resultType": "streams", "lines": [], "stats": {}}
        pods = re.search(r'pod=~?"([^"]+)"', logql)
        filters = [f.replace("(?i)", "") for f in re.findall(r'\|[=~] "((?:[^"\\]|\\.)*)"', logql)]
        rows = []
        for line in self.lines:
            if pods and not re.fullmatch(pods.group(1).replace("\\\\", "\\"), line["pod"]):
                continue
            if any(not re.search(f.replace('\\"', '"').replace("\\\\", "\\"), line["line"], re.I) for f in filters):
                continue
            rows.append({"ts": _ago(line.get("minutes_ago", 1)).isoformat(), "labels": {"namespace": self.ns, "pod": line["pod"]}, "line": line["line"]})
        rows.sort(key=lambda r: r["ts"], reverse=direction == "backward")
        return {"resultType": "streams", "lines": rows[:limit], "stats": {}}


class FixtureGit:
    """One repository's commits (`minutes_ago`, `message`, optional `paths`)."""
    type = "git"

    def __init__(self, case: dict[str, Any]):
        g = case["fixture"].get("git") or {}
        self.repositories = [g.get("repository", "example/app")]
        self.commits = g.get("commits", [])

    async def aclose(self):
        pass

    def check_repo(self, repo: str) -> None:
        if repo not in self.repositories:
            raise ConnectorError("forbidden", f"repository {repo} is not allowed")

    async def recent_commits(self, repo: str, path: str | None, since: datetime | None, limit: int) -> list[dict]:
        self.check_repo(repo)
        out = []
        for i, c in enumerate(sorted(self.commits, key=lambda c: c["minutes_ago"])):
            when = _ago(c["minutes_ago"])
            if since and when < since:
                continue
            if path and not any(p.startswith(path) for p in c.get("paths", [])):
                continue
            sha = f"{i + 1:02d}" + "ab" * 19
            out.append({"sha": sha, "short": sha[:8], "message": c["message"][:200], "author": c.get("author", "dev"), "date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "url": None})
        return out[:limit]

    async def compare_deployment(self, repo: str, images: list[str], annotations: dict[str, str]) -> dict:
        self.check_repo(repo)
        return {"matched": False, "reason": "the image tags carry no commit SHA", "images": images}


def connectors_for(case: dict[str, Any]) -> dict[str, tuple[None, Any]]:
    """What a case's environment has: Kubernetes always; Prometheus, Loki and Git when the case records them."""
    f = case["fixture"]
    out: dict[str, tuple[None, Any]] = {"kubernetes": (None, CrewFixtureKubernetes(case))}
    if "prometheus" in f:
        out["prometheus"] = (None, FixturePrometheus(case))
    if "loki" in f:
        out["loki"] = (None, FixtureLoki(case))
    if "git" in f:
        out["git"] = (None, FixtureGit(case))
    return out

