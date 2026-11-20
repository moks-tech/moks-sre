"""The evaluation gates run in the test suite, so a retrieval or skill-policy regression fails CI."""
import uuid

import pytest
from sqlalchemy import select

from moks.db import session_scope
from moks.evals import gates, runner
from moks.models import EvalRun


@pytest.mark.parametrize("dataset", ["rag", "sre", "skills"])
async def test_every_gate_passes_and_results_record_their_versions(dataset):
    rid = await runner.start(dataset, "rules")
    await runner.execute(rid)
    async with session_scope() as db:
        run = await db.get(EvalRun, rid)
    failed = [(g["name"], g["failures"]) for g in run.gates if g["state"] not in ("passed", "not_applicable")]
    assert not failed, failed
    assert all(g.get("reason") for g in run.gates if g["state"] == "not_applicable")  # a gate that did not apply says why
    assert run.kind == runner.load(dataset)["kind"] and {"dataset", "skills_generation", "pipeline", "prompt"} <= set(run.versions)


async def test_the_sre_dataset_compares_search_before_and_after_on_the_same_questions():
    """The upgrade is measured, not asserted: the same corpus, questions and grants, once as search was (format-only
    splitting, no query signals, rank-order context) and once as configured, and it must not be worse."""
    rid = await runner.start("sre", "rules")
    await runner.execute(rid)
    async with session_scope() as db:
        run = await db.get(EvalRun, rid)
    m = run.versions["metrics"]
    assert run.versions["dataset_version"] == 1 and m["comparison_cases"] >= 20
    for name in ("recall_at_5", "mrr", "ndcg_at_5", "precision_at_3", "context_precision", "context_recall"):
        assert m["comparison"][name]["current"] >= m["comparison"][name]["baseline"], (name, m["comparison"][name])
    assert m["citation_accuracy"] == 1.0
    assert {r["variant"] for r in run.results} == {"baseline", "current"}
