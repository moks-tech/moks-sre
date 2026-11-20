"""The Grafana dashboards in deploy/grafana/dashboards: built by deploy/grafana/build.py, reading only metrics MOKS exports, with
the labels those metrics have, laid out on Grafana's 24-column grid."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from prometheus_client.metrics import MetricWrapperBase

from moks import observability

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "deploy/grafana"
# Series MOKS does not define but the dashboards read: Prometheus's own, the Python client's process collector, the trace collector.
FOREIGN = re.compile(r"^(up|ALERTS|process_[a-z_]+|otelcol_[a-z_]+)$")
SELECTOR = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)\{([^}]*)\}")
MATCHER = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"')
SCRAPE_LABELS = {"job", "instance", "component", "service", "pod", "namespace"}


def _build():
    spec = importlib.util.spec_from_file_location("grafana_build", GRAFANA / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _suffixes(kind: str) -> tuple[str, ...]:
    return {"counter": ("_total", "_created"), "histogram": ("_bucket", "_count", "_sum", "_created"), "gauge": ("",)}[kind]


def _exported() -> dict[str, set[str]]:
    """Every sample name MOKS's processes export, with its label names: moks/observability.py and the reranker/embedder service."""
    names: dict[str, set[str]] = {}
    for obj in vars(observability).values():
        if isinstance(obj, MetricWrapperBase):
            base = obj._name.removesuffix("_total") if obj._type == "counter" else obj._name
            for s in _suffixes(obj._type):
                names[base + s] = set(obj._labelnames) | ({"le"} if s == "_bucket" else set())
    # The reranker image has its own requirements; read its definitions instead of importing it.
    src = (ROOT / "reranker/app.py").read_text()
    for kind, name, labels in re.findall(r'(Counter|Histogram|Gauge)\("([a-z_]+)", "[^"]*"(?:, \[([^\]]*)\])?', src):
        kind = kind.lower()
        base = name.removesuffix("_total") if kind == "counter" else name
        for s in _suffixes(kind):
            names[base + s] = set(re.findall(r'"([a-z_]+)"', labels)) | ({"le"} if s == "_bucket" else set())
    return names


@pytest.fixture(scope="module")
def build():
    return _build()


@pytest.fixture(scope="module")
def dashboards(build):
    return {name: json.loads(body) for name, body in build.render().items()}


def _exprs(d: dict) -> list[tuple[str, str]]:
    out = [(p["title"], t["expr"]) for p in d["panels"] for t in p.get("targets", []) if t.get("datasource", {}).get("type") == "prometheus"]
    return out + [(a["name"], a["expr"]) for a in d["annotations"]["list"] if a.get("expr")]


def test_the_committed_files_are_what_build_py_writes(build):
    files = build.render()
    on_disk = {p.name: p.read_text() for p in (GRAFANA / "dashboards").glob("moks-*.json")}
    assert set(on_disk) == set(files), "run python3 deploy/grafana/build.py"
    for name, body in files.items():
        assert on_disk[name] == body, f"{name} differs from deploy/grafana/build.py: run python3 deploy/grafana/build.py"


def test_every_metric_a_panel_reads_is_exported_with_those_labels(dashboards):
    exported = _exported()
    assert "moks_crewai_runs_total" in exported and "reranker_requests_total" in exported
    problems = []
    for file, d in dashboards.items():
        for title, expr in _exprs(d):
            for name, matchers in SELECTOR.findall(expr):
                if FOREIGN.match(name):
                    continue
                if name not in exported:
                    problems.append(f"{file} / {title}: {name} is not a metric MOKS exports")
                    continue
                unknown = {label for label, _ in MATCHER.findall(matchers)} - exported[name] - SCRAPE_LABELS
                if unknown:
                    problems.append(f"{file} / {title}: {name} has no label {sorted(unknown)}")
            for grouping in re.findall(r"\b(?:by|without)\s*\(([^)]*)\)", expr):
                for label in filter(None, (x.strip() for x in grouping.split(","))):
                    owners = [n for n, _ in SELECTOR.findall(expr) if not FOREIGN.match(n) and n in exported]
                    if owners and label not in SCRAPE_LABELS and label != "code" and not any(label in exported[n] for n in owners):
                        problems.append(f"{file} / {title}: no metric in the query has label {label}")
    assert not problems, "\n".join(problems)


def test_the_metrics_every_dashboard_relies_on_are_covered(dashboards):
    """New metrics should reach a dashboard: each moks_* family appears somewhere (the retired OpenSearch ones excepted)."""
    read = {name for d in dashboards.values() for _, e in _exprs(d) for name, _ in SELECTOR.findall(e)}
    retired = {"moks_opensearch_bulk_items", "moks_opensearch_search_seconds", "moks_opensearch_search_failures", "moks_search_backend"}
    missing = []
    for obj in vars(observability).values():
        if isinstance(obj, MetricWrapperBase) and obj._name not in retired:
            base = obj._name.removesuffix("_total") if obj._type == "counter" else obj._name
            if not any(n.startswith(base) for n in read):
                missing.append(obj._name)
    assert not missing, f"metrics on no dashboard: {missing}"


def test_panels_fit_the_grid_without_overlapping(dashboards):
    for file, d in dashboards.items():
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids)), file
        cells: dict[tuple[int, int], int] = {}
        for p in d["panels"]:
            g = p["gridPos"]
            assert 0 <= g["x"] and g["x"] + g["w"] <= 24 and g["h"] > 0, (file, p["title"])
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    assert (x, y) not in cells, f"{file}: {p['title']!r} overlaps panel {cells[(x, y)]}"
                    cells[(x, y)] = p["id"]


def test_every_panel_says_what_it_shows(dashboards):
    uids = [d["uid"] for d in dashboards.values()]
    assert len(set(uids)) == len(uids) and "moks-overview" in uids  # the uid bookmarks and links already use
    for file, d in dashboards.items():
        assert d["title"].startswith("MOKS · ") and "moks" in d["tags"], file
        for p in d["panels"]:
            if p["type"] not in ("row", "text"):
                assert p.get("description"), f"{file}: {p['title']!r} has no description"
                assert all(t.get("refId") for t in p["targets"]), (file, p["title"])
        assert {v["name"] for v in d["templating"]["list"]} >= {"datasource", "logs"}
        if any("$component" in e for _, e in _exprs(d)):
            assert any(v["name"] == "component" and v["allValue"] == ".*" for v in d["templating"]["list"]), file  # ".*" also matches series without the label


def test_queries_are_balanced(dashboards):
    """No PromQL parser in the backend's requirements: a bracket check catches most hand-editing slips (live Prometheus checks the rest)."""
    pairs = {")": "(", "]": "[", "}": "{"}
    for file, d in dashboards.items():
        for title, expr in _exprs(d):
            stack: list[str] = []
            for ch in re.sub(r'"[^"]*"', '""', expr):
                if ch in "([{":
                    stack.append(ch)
                elif ch in pairs:
                    assert stack and stack.pop() == pairs[ch], f"{file} / {title}: {expr}"
            assert not stack, f"{file} / {title}: {expr}"
