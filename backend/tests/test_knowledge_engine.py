"""The SRE knowledge engine: document types, structure-aware chunking, the embedding cache, weighted fusion,
filters, query understanding, reranker controls, context assembly and investigation history."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from moks.config import get_settings
from moks.db import session_scope
from moks.models import Conversation, Document, EmbeddingCache, Environment, Investigation, KnowledgeSource
from moks.rag import chunking, context, doctypes, history, query, service, structure
from moks.rag.backends import rrf
from moks.rag.rerank import Outcome
from moks.rbac import ROLES, Binding, P, Principal
from tests.conftest import login, make_user
from tests.test_rag_index import _model, os_  # noqa: F401  (fixtures)

RULES = """groups:
  - name: payments
    rules:
      - alert: PaymentsHighErrorRate
        expr: sum(rate(http_requests_total{code=~"5.."}[5m])) > 0.05
        for: 10m
        labels:
          severity: critical
        annotations:
          runbook_url: https://runbooks/payments
      - record: job:errors:rate5m
        expr: sum by (job) (rate(errors_total[5m]))
"""
MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: payments
  namespace: shop
spec:
  replicas: 2
---
apiVersion: v1
kind: Service
metadata:
  name: payments
  namespace: shop
spec:
  ports:
    - port: 80
"""
OPENAPI = """openapi: 3.0.0
info:
  title: Payments
paths:
  /payments:
    get:
      operationId: listPayments
    post:
      operationId: createPayment
components:
  schemas:
    Payment:
      type: object
"""
DASHBOARD = json.dumps({"title": "RabbitMQ", "schemaVersion": 39, "panels": [
    {"type": "row", "title": "Queues", "panels": [{"type": "timeseries", "title": "Queue depth", "targets": [{"expr": "rabbitmq_queue_messages_ready"}]}]},
    {"type": "stat", "title": "Consumers", "gridPos": {"h": 4}, "targets": [{"expr": "rabbitmq_queue_consumers"}]}]})
LOG = """2026-11-20T10:00:01Z INFO started
2026-11-20T10:00:05Z ERROR charge failed
Traceback (most recent call last):
  File "app.py", line 10, in charge
TimeoutError: ledger
2026-11-20T10:00:09Z WARN retrying
"""
POSTMORTEM = "# Postmortem: cart OOMKilled\n\n## Summary\nCart pods were OOMKilled.\n\n## Timeline\n- 09:12 alert fired\n- 09:57 rolled back\n\n## Root cause\nAn unbounded session cache.\n"


def _pieces(path: str, text: str):
    t = doctypes.classify(path, text)
    stored = structure.render(t, path, text)
    return t, stored, chunking.chunk(path, stored, t)


# ---- document types and structure ----------------------------------------------------------------------


@pytest.mark.parametrize("path,text,want", [
    ("runbooks/redis.md", "# Redis\n", "runbook"), ("docs/postmortems/2026-10.md", "# x\n", "postmortem"), ("README.md", "# x\n", "readme"),
    ("docs/guide.md", "# Guide\n", "documentation"), ("rules/payments.yaml", RULES, "alert_rules"), ("k8s/app.yaml", MANIFEST, "k8s_manifest"),
    ("charts/app/values.yaml", "replicas: 2\n", "helm_values"), ("charts/app/Chart.yaml", "name: app\n", "helm_chart"),
    ("charts/app/templates/deploy.yaml", "kind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n", "helm_template"),
    ("api/openapi.yaml", OPENAPI, "openapi"), ("grafana/rabbitmq.json", DASHBOARD, "dashboard"), ("infra/main.tf", 'resource "aws_s3_bucket" "b" {}\n', "terraform"),
    ("logs/app.log", LOG, "log"), (".github/workflows/ci.yml", "on: push\n", "ci_pipeline"), ("Dockerfile", "FROM alpine\n", "dockerfile"),
    ("src/app.py", "def f():\n  pass\n", "code"), ("config/app.json", '{"a": 1}', "config"), ("notes/oncall.md", "---\ntype: runbook\n---\n# x\n", "runbook"),
])
def test_document_types_come_from_path_source_and_shape(path, text, want):
    assert doctypes.classify(path, text) == want


def test_a_source_decides_the_type_of_what_it_alone_produces():
    assert doctypes.classify("incidents/1.md", "# x", "incident") == "postmortem"
    assert doctypes.classify("investigations/1.md", "# x", "investigation") == "investigation"
    assert doctypes.classify("https://wiki.example.com/runbooks/db", "x", "web") == "runbook"


def test_alert_rules_split_per_rule_and_say_which_alert_and_severity():
    _, stored, pieces = _pieces("monitoring/rules.yaml", RULES)
    assert [p.heading for p in pieces] == ["alert PaymentsHighErrorRate (group payments)", "record job:errors:rate5m (group payments)"]
    assert pieces[0].meta == {"alert": "PaymentsHighErrorRate", "group": "payments", "severity": "critical", "has_runbook_url": True}
    assert "groups:" in pieces[0].text  # the group's opening lines join its first rule rather than being a chunk of their own


def test_manifests_split_per_resource_with_kind_namespace_and_name():
    t, stored, pieces = _pieces("deploy/payments.yaml", MANIFEST)
    assert [p.meta["resource"] for p in pieces] == [{"kind": "Deployment", "namespace": "shop", "name": "payments"}, {"kind": "Service", "namespace": "shop", "name": "payments"}]
    assert doctypes.resource_types(t, stored) == ["deployment", "service"]


def test_openapi_splits_per_operation_and_schema():
    _, _, pieces = _pieces("api/openapi.yaml", OPENAPI)
    assert [p.heading for p in pieces] == ["API Payments", "GET /payments", "POST /payments", "schema Payment"]
    assert pieces[2].meta == {"operation": "POST /payments", "operation_id": "createPayment"}


def test_a_dashboard_is_stored_as_its_panels_and_queries():
    t, stored, pieces = _pieces("grafana/rabbitmq.json", DASHBOARD)
    assert "gridPos" not in stored and "Query: rabbitmq_queue_messages_ready" in stored and "Row: Queues" in stored
    assert [p.meta.get("panel") for p in pieces if p.meta.get("panel")] == ["Queue depth", "Consumers"]
    assert doctypes.is_rendered(t, "grafana/rabbitmq.json") and service._line_url("https://github.com/o/r/blob/x/g.json", 3, 5, "github", "data", rendered=True) == "https://github.com/o/r/blob/x/g.json"


def test_a_log_chunk_ends_only_where_an_event_starts(monkeypatch):
    monkeypatch.setattr(get_settings(), "rag_chunk_tokens", 50)
    monkeypatch.setattr(get_settings(), "rag_chunk_overlap_tokens", 0)
    _, stored, pieces = _pieces("logs/app.log", LOG * 3)
    assert len(pieces) > 1
    for p in pieces:  # a stack trace stays with the event that raised it
        assert not p.text.startswith(("Traceback", "  File", "TimeoutError"))
    assert pieces[0].meta["levels"] and pieces[0].heading.startswith("log events 2026-11-20T10:00:01")


def test_postmortem_sections_are_labelled_and_runbook_steps_counted():
    _, _, pieces = _pieces("postmortems/cart.md", POSTMORTEM)
    assert {p.meta.get("section") for p in pieces} >= {"summary", "timeline", "root_cause"}
    _, _, pieces = _pieces("runbooks/x.md", "# X\n\n## Mitigation\n1. Check lag.\n2. Promote the replica.\n   Wait for it.\n3. Repoint.\n")
    assert pieces[-1].meta == {"section": "mitigation", "steps": [1, 3]}


@pytest.mark.parametrize("path,text", [("monitoring/rules.yaml", RULES), ("deploy/payments.yaml", MANIFEST), ("api/openapi.yaml", OPENAPI),
                                       ("grafana/rabbitmq.json", DASHBOARD), ("logs/app.log", LOG), ("postmortems/cart.md", POSTMORTEM)])
def test_every_typed_piece_points_at_exactly_its_stored_text(path, text):
    _, stored, pieces = _pieces(path, text)
    for p in pieces:
        assert stored[p.start_offset:p.end_offset] == p.text
        assert p.start_line == stored.count("\n", 0, p.start_offset) + 1 and p.end_line == stored.count("\n", 0, p.end_offset) + 1
    covered = "".join(stored[p.start_offset:p.end_offset] for p in pieces)
    wanted = "".join(ln for ln in stored.splitlines() if ln.strip() != "---")  # a YAML document separator belongs to no document
    assert "".join(wanted.split()) == "".join(covered.split()) or "".join(wanted.split()) in "".join(covered.split())  # nothing is dropped


def test_structured_chunking_off_splits_as_before_and_reads_documents_again():
    token = chunking.use_structured(False)
    try:
        assert [(p.heading, p.text) for p in chunking.chunk("monitoring/rules.yaml", RULES, "alert_rules")] == [(p.heading, p.text) for p in chunking.chunk("monitoring/rules.yaml", RULES)]
        assert chunking.version_for("runbooks/a.md") == chunking.LEGACY_PIPELINE_VERSION and chunking.version_for("a.py") == chunking.LEGACY_CODE_PIPELINE_VERSION
    finally:
        chunking.reset_structured(token)
    assert chunking.version_for("runbooks/a.md") == chunking.PIPELINE_VERSION


# ---- fusion and understanding ---------------------------------------------------------------------


def test_default_weights_fuse_exactly_as_listing_identifiers_twice_did():
    kw, ident, vec = ["a", "b", "c"], ["c", "d"], ["b", "a"]
    weighted, listed = rrf(kw, ident, vec, weights=[1.0, 2.0, 1.0]), rrf(kw, ident, ident, vec)
    assert [c for c, _ in weighted] == [c for c, _ in listed] and all(a == pytest.approx(b) for (_, a), (_, b) in zip(weighted, listed))
    assert dict(rrf(kw, ident, weights=[1.0, 0.0])) == dict(rrf(kw))  # a weight of 0 leaves a ranker out


@pytest.mark.parametrize("q,first", [("Find similar OOMKilled incidents", "similar_incidents"), ("Has the cart service been OOMKilled before?", "similar_incidents"),
                                      ("Which services depend on Redis?", "dependencies"), ("What changed in the last hour?", "change"),
                                      ("How do I troubleshoot RabbitMQ backlog?", "procedure"), ("Why is payment-service failing?", "troubleshooting"),
                                      ("Which Grafana panel shows queue depth?", "dashboard")])
def test_intent_rules(q, first):
    assert query.intents(q)[0] == first


def test_time_windows():
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    assert query.time_window("what changed in the last hour", now)["since"] == now - timedelta(hours=1)
    assert query.time_window("errors in the last 3 days", now)["since"] == now - timedelta(days=3)
    assert query.time_window("deploys yesterday", now)["since"] == datetime(2026, 11, 19, tzinfo=timezone.utc)
    assert query.time_window("since 2026-11-01", now)["since"] == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert query.time_window("how do I restart redis", now) is None


def test_names_resolve_across_spelling_and_dependents_come_from_declarations():
    catalog = [("prod", "shop", "payments", None, {}), ("prod", "shop", None, "redis", {"dependency_of": ["payments", "cart"]}), ("production", "data", "orders", None, {})]
    got = query.resolve("Why is payment-service slow in production, and who depends on redis?", catalog)
    assert got["services"] == ["payments", "redis"] and got["environments"] == ["prod", "production"] and got["dependents"] == {"redis": ["cart", "payments"]}
    assert query.resolve("is prod ok", catalog)["environments"] == ["prod", "production"]


# ---- context assembly -----------------------------------------------------------------------------------


def _hit(i, doc, text, offsets, lines, state="current", **kw):
    return {"chunk_id": f"c{i}", "document_id": doc, "text": text, "heading": kw.pop("heading", ""), "title": kw.pop("title", doc), "doc_type": kw.pop("doc_type", "runbook"),
            "citation": {"path": f"{doc}.md", "offsets": offsets, "lines": lines}, "freshness": {"state": state, **kw.pop("freshness", {})}, "scope": {"environment": "prod", "namespace": "shop"}, **kw}


def test_assembly_joins_one_documents_chunks_and_drops_repeats():
    stored = "Step one checks lag.\nStep two promotes the replica.\nStep three repoints the sentinel."
    a, b = stored[:51], stored[21:]  # they overlap: "Step two promotes the replica."
    hits = [_hit(1, "d1", b, [21, len(stored)], [2, 3]), _hit(2, "d1", a, [0, 51], [1, 2]), _hit(3, "d2", b, [21, len(stored)], [2, 3]), _hit(1, "d1", b, [21, len(stored)], [2, 3])]
    laid = context.assemble(hits)
    assert len(laid["blocks"]) == 1 and laid["blocks"][0]["text"] == stored and laid["blocks"][0]["ids"] == ["2", "1"] and laid["blocks"][0]["lines"] == [1, 3]
    assert {d["id"]: d["reason"] for d in laid["dropped"]} == {"3": "the same text as 1, from another document", "4": "the same passage was already included"}


def test_assembly_puts_current_guidance_first_labels_history_and_keeps_the_budget():
    hits = [_hit(1, "old", "Roll back with deploy.sh legacy script now.", [0, 44], [1, 1], state="superseded", freshness={"reason": "superseded by rollback.md"}),
            _hit(2, "new", "Roll back with the deploy pipeline and watch.", [0, 45], [1, 1]),
            _hit(3, "pm", "Cart pods were OOMKilled after release 2.3 shipped.", [0, 51], [1, 1], doc_type="postmortem", freshness={"possible_conflict": True, "conflict_with": ["x.md"]})]
    laid = context.assemble(hits)
    assert [b["document_id"] for b in laid["blocks"]] == ["new", "pm", "old"]
    labels = {b["document_id"]: " ".join(b["labels"]) for b in laid["blocks"]}
    assert "superseded: superseded by rollback.md" in labels["old"] and "historical record" in labels["pm"] and "may conflict with x.md" in labels["pm"]
    assert [b["document_id"] for b in context.assemble(hits, history=True)["blocks"]] == ["old", "new", "pm"]
    tight = context.assemble(hits, total_chars=260)
    assert len(tight["blocks"]) == 1 and {d["reason"] for d in tight["dropped"]} == {"past the context budget"}


def test_assembly_joins_the_context_search_attached_without_giving_it_an_id():
    stored = "# PM\n## Summary\nCart was OOMKilled.\n## Root cause\nAn unbounded session cache."
    hit = _hit(1, "pm", stored[5:35], [5, 35], [2, 3], doc_type="postmortem",
               context=[{"chunk_id": "c9", "text": stored[36:], "offsets": [36, len(stored)], "lines": [4, 5], "heading": "PM > Root cause"}])
    laid = context.assemble([hit])
    assert len(laid["blocks"]) == 1 and laid["blocks"][0]["ids"] == ["1"] and "unbounded session cache" in laid["blocks"][0]["text"] and laid["blocks"][0]["context_chunks"] == 1
    text = context.render(laid, lambda label, body, limit: f"<untrusted_data>{body}</untrusted_data>")
    assert text.startswith("[1] postmortem") and "<untrusted_data>" in text


# ---- indexing, the embedding cache and search ---------------------------------------------------------


async def _source(env=None, ns="team", **kw):
    """A scope of its own: a document every test can read would change what later tests retrieve."""
    env = env or f"ke-{uuid.uuid4().hex[:6]}"
    async with session_scope() as db:
        src = KnowledgeSource(name=f"ke-{uuid.uuid4().hex[:6]}", type="upload", config={}, scope_environment=env, scope_namespace=ns, **kw)
        db.add(src)
        await db.flush()
        return src.id


async def _put(sid, path, text, extra=None):
    async with session_scope() as db:
        src = await db.get(KnowledgeSource, sid)
        doc = await service.index_document(db, src, path, text, revision=None, url=None, extra=extra or {})
        return doc.id


async def _find(who, q, **kw):
    async with session_scope() as db:
        return await service.search(db, who, q, 8, **kw)


ADMIN = Principal("00000000-0000-0000-0000-0000000000ad", "ke@x", [Binding("investigator", "*", "*")])


async def test_documents_store_their_type_resources_and_chunk_metadata():
    sid = await _source()
    did = await _put(sid, "deploy/payments.yaml", MANIFEST)
    async with session_scope() as db:
        doc = await db.get(Document, did)
        assert doc.doc_type == "k8s_manifest" and doc.resource_types == ["deployment", "service"] and doc.content_changed_at is not None
    tag = uuid.uuid4().hex[:6]
    await _put(sid, "runbooks/r.md", f"# Restart {tag}\n\n## Mitigation\n1. drain {tag}\n2. restart {tag}\n")
    r = await _find(ADMIN, f"restart {tag}")
    hit = r["results"][0]
    assert hit["doc_type"] == "runbook" and hit["meta"]["section"] == "mitigation" and "content_changed_at" in hit["freshness"]


async def test_a_reread_is_not_a_change_but_new_words_are():
    sid = await _source()
    did = await _put(sid, "runbooks/a.md", "# A\nfirst words\n")
    async with session_scope() as db:
        doc = await db.get(Document, did)
        first = doc.content_changed_at
        doc.pipeline_version = 1  # as if an older pipeline had read it
    await _put(sid, "runbooks/a.md", "# A\nfirst words\n")
    async with session_scope() as db:
        assert (await db.get(Document, did)).content_changed_at == first
    await _put(sid, "runbooks/a.md", "# A\nnew words\n")
    async with session_scope() as db:
        assert (await db.get(Document, did)).content_changed_at > first


async def test_filters_only_narrow_and_unknown_types_are_refused(client):
    tag = uuid.uuid4().hex[:6]
    sid = await _source()
    await _put(sid, "runbooks/q.md", f"# Queue {tag}\nconsumers {tag}\n")
    await _put(sid, "monitoring/q.yaml", RULES.replace("payments", f"q{tag}") + f"# {tag}\n")
    old = await _put(sid, "docs/old.md", f"# Old {tag}\nconsumers {tag}\n")
    async with session_scope() as db:
        (await db.get(Document, old)).content_changed_at = datetime.now(timezone.utc) - timedelta(days=40)
    assert {h["doc_type"] for h in (await _find(ADMIN, tag, doc_types=["runbook"]))["results"]} == {"runbook"}
    recent = await _find(ADMIN, tag, changed_since=datetime.now(timezone.utc) - timedelta(days=1))
    assert recent["results"] and all(h["citation"]["path"] != "docs/old.md" for h in recent["results"])
    with pytest.raises(Exception, match="unknown document type"):
        await _find(ADMIN, tag, doc_types=["spreadsheet"])
    h = await login(client, "admin@example.com")
    assert (await client.get(f"/moks/api/knowledge/search?q={tag}&doc_type=spreadsheet", headers=h)).status_code == 422
    r = await client.get(f"/moks/api/knowledge/search?q={tag}&doc_type=runbook", headers=h)
    assert r.status_code == 200 and {x["doc_type"] for x in r.json()["results"]} == {"runbook"}


async def test_a_question_resolves_only_services_its_asker_can_read():
    tag = uuid.uuid4().hex[:6]
    shop = await _source(f"prod-{tag}", "shop", service=f"cart{tag}")
    billing = await _source(f"prod-{tag}", "billing", service=f"ledger{tag}", applies_to={"dependency_of": [f"invoices{tag}"]})
    await _put(shop, "runbooks/cart.md", f"# Cart {tag}\nrestart cart{tag}\n")
    await _put(billing, "runbooks/ledger.md", f"# Ledger {tag}\nrotate ledger{tag}\n")
    reader = Principal("u-ke", "r@x", [Binding("investigator", f"prod-{tag}", "shop")])
    got = (await _find(reader, f"does cart{tag} or ledger{tag} depend on anything"))["understanding"]
    assert got["services"] == [f"cart{tag}"] and got["dependents"] == {}  # the billing service is not even named back
    wide = (await _find(ADMIN, f"what depends on ledger{tag}"))["understanding"]
    assert wide["dependents"] == {f"ledger{tag}": [f"invoices{tag}"]}


async def test_the_embedding_cache_embeds_only_changed_text_and_rebuilds_for_free(os_, monkeypatch):  # noqa: F811
    calls: list[int] = []
    sent: list[str] = []

    class Counting:
        async def embed(self, texts):
            calls.append(len(texts))
            sent.extend(texts)
            return [[float(len(t) % 7), 0.5, 0.25, 1.0] for t in texts]

    await _model(monkeypatch, size=4)
    monkeypatch.setattr(service, "provider_for", lambda cfg: Counting())
    sid = await _source()
    sections = "".join(f"## Part {i}\n" + f"Words for part {i}. " * 40 + "\n\n" for i in range(6))
    await _put(sid, "runbooks/long.md", "# Long\n\n" + sections)
    await service.drain_outbox()
    first = sum(calls) - 1  # the first call probes the model's dimensions
    assert first >= 6
    await _put(sid, "runbooks/long.md", "# Long\n\n" + sections.replace("Words for part 3.", "Changed words for part 3.", 1))
    await service.drain_outbox()
    assert 1 <= sum(calls) - 1 - first <= 2  # only the changed part (and a chunk overlapping it) is sent
    mine = {t for t in sent if "Words for part" in t}
    sent.clear()
    async with session_scope() as db:
        await service.reindex(db)  # a rebuild with the same model reads this document's vectors from the cache
    assert not mine & set(sent)  # other tests' documents in this database may still need embedding; this one does not
    async with session_scope() as db:
        assert (await db.execute(select(func.count()).select_from(EmbeddingCache))).scalar_one() >= first


async def test_cache_cleanup_drops_vectors_no_index_can_use():
    async with session_scope() as db:
        db.add(EmbeddingCache(model_key=f"gone/{uuid.uuid4().hex}", input_hash="0" * 64, dimensions=1, vector=b"\x00\x00\x80\x3f"))
    assert await service.cleanup_embedding_cache(force=True) >= 1


class _Scores:
    """A reranker that prefers whatever ranked last, scoring each passage by its position."""
    candidates = 30

    def __init__(self):
        self.seen = 0

    async def rerank(self, query, items):
        self.seen += 1
        return Outcome(scores={cid: float(i) for i, (cid, _) in enumerate(items)}, model="m", revision="r", candidates=len(items))


async def test_a_reranker_floor_drops_low_scores_but_never_an_exact_identifier(monkeypatch):
    word = "".join(chr(97 + b % 26) for b in uuid.uuid4().bytes[:8])  # letters only: a hex tag would itself be an exact identifier
    code = f"ERR_FLOOR_{uuid.uuid4().hex[:6].upper()}"
    sid = await _source()
    for i in range(5):
        await _put(sid, f"runbooks/f{i}.md", f"# Floor {word}\nfloor {word} passage {'one two three four five'.split()[i]}" + (f" {code}" if i == 0 else ""))
    monkeypatch.setattr(get_settings(), "rag_rerank_min_score", 2.5)
    r = await _find(ADMIN, f"floor {word}", reranker=_Scores())  # the stand-in scores 0, 1, 2, ... in rank order
    assert r["reranking"]["state"] == "applied" and r["reranking"]["below_threshold"] == 3 and len(r["results"]) == 2
    r = await _find(ADMIN, code, reranker=_Scores())  # scored 0, below the floor, and kept: matching it is why it was found
    assert r["reranking"]["below_threshold"] == 0 and [code in h["text"] for h in r["results"]] == [True]


async def test_the_search_page_can_skip_the_reranker_that_investigations_use(monkeypatch):
    tag = uuid.uuid4().hex[:6]
    sid = await _source()
    await _put(sid, "runbooks/s.md", f"# Skip {tag}\nskip {tag}\n")
    fake = _Scores()
    monkeypatch.setattr(service.reranking, "active", lambda: fake)
    monkeypatch.setattr(get_settings(), "rag_rerank_interactive", False)
    assert (await _find(ADMIN, f"skip {tag}", interactive=True))["reranking"]["state"] == "skipped" and fake.seen == 0
    assert (await _find(ADMIN, f"skip {tag}"))["reranking"]["state"] == "applied" and fake.seen == 1


async def test_the_context_endpoint_assembles_under_the_callers_own_grants(client):
    tag = uuid.uuid4().hex[:6]
    sid = await _source()
    await _put(sid, "runbooks/c.md", f"# Context {tag}\n\n## Mitigation\n1. step one {tag}\n2. step two {tag}\n")
    h = await login(client, "admin@example.com")
    r = await client.post("/moks/api/knowledge/context", json={"query": f"context {tag} step", "budget_tokens": 500}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocks"] and body["blocks"][0]["path"] == "runbooks/c.md" and "<untrusted_data" in body["prompt"] and body["tokens"] <= 500
    assert (await client.post("/moks/api/knowledge/context", json={"query": "x", "budget_tokens": 1}, headers=h)).status_code == 422


# ---- investigation history ---------------------------------------------------------------------------


def test_every_role_that_reads_knowledge_reads_investigations():
    assert history.access_matches()
    assert all(P.INVESTIGATION_READ in perms for perms in ROLES.values() if P.KNOWLEDGE_READ in perms)


async def _investigation(env_id, user_id, namespace, question, **kw):
    async with session_scope() as db:
        now = datetime.now(timezone.utc)
        inv = Investigation(created_by=user_id, environment_id=env_id, namespace=namespace, question=question, time_from=now - timedelta(minutes=30), time_to=now,
                            status=kw.pop("status", "completed"), report=kw.pop("report", {"summary": f"{question} summary", "findings": [{"kind": "oomkilled", "severity": "high", "title": "cart OOMKilled"}],
                                                                                         "suspected_causes": [{"cause": "unbounded cache", "confidence": "medium"}]}), **kw)
        db.add(inv)
        await db.flush()
        return inv.id


async def test_investigation_history_is_scoped_like_the_investigation_and_leaves_when_deleted(client, monkeypatch):
    tag = uuid.uuid4().hex[:6]
    uid = await make_user(f"hist-{tag}@example.com", [f"investigator:hist-{tag}:shop"])
    async with session_scope() as db:
        env = Environment(name=f"hist-{tag}", display_name="h", kind="minikube", allowed_namespaces=["*"], denied_namespaces=[])
        db.add(env)
        await db.flush()
        env_id = env.id
        conv = Conversation(created_by=uid, environment_id=env_id, namespace="shop", title="t")
        db.add(conv)
        await db.flush()
        conv_id = conv.id
    kept = await _investigation(env_id, uid, "shop", f"why is cart{tag} OOMKilled", conversation_id=conv_id)
    await _investigation(env_id, uid, "*", f"cart{tag} everywhere")  # every namespace: never indexed
    await _investigation(env_id, uid, "shop", f"cart{tag} still running", status="running")
    await _investigation(env_id, uid, "shop", f"cart{tag} eval", trigger="eval")
    monkeypatch.setattr(get_settings(), "rag_index_investigations", True)
    async with session_scope() as db:
        src = await history.source(db)
        result = await history.sync(db, src)
    assert result["complete"] and result["discovered"] >= 1
    async with session_scope() as db:
        docs = (await db.execute(select(Document).where(Document.source_id == src.id, Document.status == "active", Document.path.like("investigations/%")))).scalars().all()
        mine = [d for d in docs if d.path == history.path_of(kept)]
        assert len(mine) == 1 and mine[0].doc_type == "investigation" and (mine[0].environment, mine[0].namespace) == (f"hist-{tag}", "shop")
        assert not any(f"cart{tag} everywhere" in (d.title or "") for d in docs)
    inside = Principal(str(uid), "a@x", [Binding("investigator", f"hist-{tag}", "shop")])
    outside = Principal(str(uuid.uuid4()), "b@x", [Binding("investigator", f"hist-{tag}", "payments")])
    hit = (await _find(inside, f"cart{tag} OOMKilled unbounded cache"))["results"]
    assert hit and hit[0]["doc_type"] == "investigation" and hit[0]["freshness"]["historical"]
    assert not (await _find(outside, f"cart{tag} OOMKilled unbounded cache"))["results"]
    h = await login(client, f"hist-{tag}@example.com")
    assert (await client.delete(f"/moks/api/conversations/{conv_id}", headers=h)).status_code == 200
    assert not (await _find(inside, f"cart{tag} OOMKilled unbounded cache"))["results"]  # gone at commit, not at the next sync
    monkeypatch.setattr(get_settings(), "rag_index_investigations", False)
    async with session_scope() as db:
        assert (await history.sync(db, await history.source(db)))["enabled"] is False
        left = (await db.execute(select(func.count()).select_from(Document).where(Document.source_id == src.id, Document.status == "active"))).scalar_one()
    assert left == 0


async def test_kb_search_accepts_document_types():
    from moks.tools.phase2 import KbArgs

    assert KbArgs(query="rabbitmq backlog", doc_types=["runbook", "alert_rules"]).doc_types == ["runbook", "alert_rules"]
    with pytest.raises(Exception):
        KbArgs(query="x y", doc_types=["spreadsheet"])


async def test_seeding_the_cache_is_a_no_op_without_vectors(monkeypatch):
    monkeypatch.setattr(service, "_seeded", False)
    monkeypatch.setattr(service, "backend_name", lambda: "postgres")
    async with session_scope() as db:
        has = await service.PostgresBackend(db).has_vector()
    if has:
        pytest.skip("pgvector is installed on this server")
    assert await service.seed_embedding_cache() == 0 and service._seeded
