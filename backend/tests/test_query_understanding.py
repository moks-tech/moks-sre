"""Reading a question: normalisation that never touches an identifier, typed entities, the question type, and
expansion that only ever adds."""
from __future__ import annotations

import pytest

from moks.evals import embedding_bench, embedding_eval
from moks.rag import query
from moks.rag.service import _exact_tokens, identifiers

IDENTIFIER_HEAVY = [
    "why is pod/api-7f9c6d8b5-x2k4p OOMKilled with exit code 137",
    "Error acquiring the state lock ConditionalCheckFailedException on module.vpc.aws_subnet.private[0]",
    'rate(http_request_duration_seconds_bucket{job="api", route=~"/v1/.*"}[5m]) is high',
    '{namespace="payments", app="api"} |= "timeout" | json | status >= 500',
    "AccessDenied for arn:aws:iam::123456789012:role/moks-api on s3://example-bucket/path",
    "MOKS_RAG_BACKEND=postgres and spring.datasource.hikari.maximum-pool-size",
    "trace 4bf92f3577b34da6a3ce929d0e0e4736 missing service.name",
    "CrashLoopBackOff after helm upgrade to v1.29.3 in eu-west-1",
]


def test_normalization_only_fixes_what_nobody_meant_to_type():
    assert query.normalize_query("moks–api  ‘service’​ is slow\n") == "moks-api 'service' is slow"
    assert query.normalize_query("“OOMKilled”") == '"OOMKilled"'
    assert query.normalize_query("ＯＯＭKilled") == "OOMKilled"  # full-width letters
    assert query.normalize_query("   ") == ""


@pytest.mark.parametrize("q", IDENTIFIER_HEAVY)
def test_normalization_keeps_every_identifier_exactly(q):
    assert query.normalize_query(q) == q  # already clean text comes back unchanged, case and punctuation included
    for ident in identifiers(q):
        assert ident in query.normalize_query(q)


@pytest.mark.parametrize("q", IDENTIFIER_HEAVY)
def test_exact_tokens_never_lose_an_identifier_the_search_already_had(q):
    tokens = _exact_tokens(q, query.entities(q))
    for ident in identifiers(q)[:len(tokens)]:
        assert ident in tokens


def test_entities_by_type():
    found = query.entities("kubectl -n payments get pod/api-7f9c6d8b5-x2k4p shows OOMKilled exit code 137 in us-east-1 on EKS")
    assert found["k8s_namespace"] == ["payments"] and found["k8s_object"] == ["pod/api-7f9c6d8b5-x2k4p"] and found["pod_name"] == ["api-7f9c6d8b5-x2k4p"]
    assert found["k8s_status"] == ["OOMKilled"] and found["exit_code"] == ["exit code 137"] and found["aws_region"] == ["us-east-1"] and found["aws_service"] == ["EKS"]
    assert "http_status" not in found  # "code 137" is an exit code, not an HTTP status
    tf = query.entities('terraform plan destroys module.vpc.aws_subnet.private["eu-west-1a"] and module.eks')
    assert tf["terraform_address"] == ['module.vpc.aws_subnet.private["eu-west-1a"]'] and tf["terraform_module"] == ["module.eks"]
    obs = query.entities('sum by (level) (count_over_time({app="checkout-api"} | json [5m])) and histogram_quantile(0.99, x) with service.name unset, HTTP 503')
    assert obs["service"] == ["checkout-api"] and "count_over_time" in obs["promql_function"] and obs["otel_attribute"] == ["service.name"]
    assert obs["label_selector"] == ['{app="checkout-api"}'] and "| json" in obs["logql_stage"] and obs["http_status"] == ["HTTP 503"]
    aws = query.entities("arn:aws:iam::123456789012:role/x i-0abc12345def67890 vpc-0a1b2c3d s3://bucket/key")
    assert aws["arn"] == ["arn:aws:iam::123456789012:role/x"] and aws["aws_resource_id"] == ["i-0abc12345def67890", "vpc-0a1b2c3d"] and aws["s3_uri"] == ["s3://bucket/key"]


def test_whole_entities_the_identifier_pattern_splits_are_matched_exactly():
    q = "AccessDenied on arn:aws:iam::123456789012:role/moks-api"
    tokens = _exact_tokens(q, query.entities(q))
    assert "arn:aws:iam::123456789012:role/moks-api" in tokens and len(tokens) <= 12


@pytest.mark.parametrize("q,expected", [
    ("Why is my pod restarting?", "troubleshooting"),
    ("Terraform deployment failed", "troubleshooting"),
    ("OOMKilled", "identifier"),
    ("Not authorized to perform sts:AssumeRoleWithWebIdentity", "identifier"),
    ("how do I roll back a bad release", "how_to"),
    ("difference between liveness and readiness probes", "conceptual"),
    ("has payments been OOMKilled before and what fixed it", "incident_history"),
    ("python function that retries with exponential backoff", "code"),
    ("kubectl -n payments get pod/api-7f9c6d8b5-x2k4p shows OOMKilled exit code 137", "troubleshooting"),
])
def test_question_types(q, expected):
    got = query.classify(q, query.entities(q), identifiers(q))
    assert got["type"] == expected and got["rule"]


def test_the_classifier_agrees_with_the_labelled_dataset_often_enough():
    """A regression guard, not a target: the rules are explainable, and the dataset's labels are one person's."""
    assert embedding_bench.classifier_accuracy(embedding_eval.load())["accuracy"] >= 0.7


def test_expansion_adds_related_terms_and_never_repeats_or_removes():
    q = "why is my pod restarting"
    terms = query.expansion_terms(q)
    assert "CrashLoopBackOff" in terms and "OOMKilled" in terms
    assert all(t.lower() not in q.lower() for t in terms) and len(terms) <= query.MAX_EXPANSIONS
    assert query.expansion_terms("CrashLoopBackOff restarting") == [t for t in query.expansion_terms("restarting") if t != "CrashLoopBackOff"]
    assert query.expansion_terms("ERR_PAY_4071") == []
    assert query.expansion_terms("subtle") == []  # whole words only: "tf" is not inside "subtle"... nor "slow" inside "slowly"
    assert query.expansion_terms("slowly") == []


def test_understanding_reports_type_and_entities_without_changing_the_old_fields():
    got = query.understand("why did pod checkout-7f return HTTP 503 and CrashLoopBackOff since 10:30", ["checkout-7f"], expansion=True)
    assert "pod" in got["resource_types"] and "HTTP 503" in got["errors"] and got["identifiers"] == ["checkout-7f"]
    assert got["query_type"]["type"] == "troubleshooting" and got["entities"]["k8s_status"] == ["CrashLoopBackOff"] and "expansions" in got
