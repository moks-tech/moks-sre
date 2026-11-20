"""More workload kinds for the CrewAI investigation: CronJobs, Jobs, standalone ReplicaSets and Pods, each resolved to
its pods by owner reference, and listed by the workloads tool so the form can offer them."""
from __future__ import annotations

import typing

import pytest

from moks.api.investigations import CrewIn
from moks.connectors.kubernetes import WORKLOAD_KINDS, summarize_workload
from moks.crew import config
from moks.crew.tools import BY_NAME, Members, Target, bind, controllers_for, owned_by


def target(kind: str, workload: str) -> Target:
    return Target(environment="e", namespace="shop", workload=workload, kind=kind, minutes=60, repository="o/r")


def test_every_kind_the_crew_offers_is_accepted_and_listed():
    assert set(typing.get_args(CrewIn.model_fields["kind"].annotation)) == set(config.KINDS)
    assert set(config.KINDS) - {"Pod"} == set(WORKLOAD_KINDS)  # pods are listed by k8s_list_pods
    spec = {f["name"]: f for f in config.FLOWS[config.FLOW]["inputs"]}
    assert spec["kind"]["choices"] == list(config.KINDS)
    assert (spec["namespace"]["type"], spec["workload"]["type"]) == ("namespace", "workload")


def test_jobs_and_cronjobs_are_summarized_by_how_they_ran_not_by_replicas():
    job = summarize_workload("Job", {
        "metadata": {"name": "backup-29012345", "uid": "j1", "ownerReferences": [{"kind": "CronJob", "name": "backup", "uid": "c1", "controller": True}]},
        "spec": {"template": {"spec": {"containers": [{"image": "postgres:16"}]}}},
        "status": {"failed": 3, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}})
    assert job["state"] == "Failed" and job["failed"] == 3 and job["desired"] == 0 and job["images"] == ["postgres:16"]
    assert (job["ownerKind"], job["ownerName"], job["ownerUid"]) == ("CronJob", "backup", "c1")
    cron = summarize_workload("CronJob", {
        "metadata": {"name": "backup", "uid": "c1"},
        "spec": {"schedule": "0 2 * * *", "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"image": "postgres:16"}]}}}}},
        "status": {"active": [{"name": "backup-29012345"}], "lastScheduleTime": "2026-11-20T02:00:00Z"}})
    assert cron["schedule"] == "0 2 * * *" and cron["active"] == 1 and cron["images"] == ["postgres:16"] and cron["desired"] == 0


WORKLOADS = [
    {"kind": "CronJob", "name": "backup", "uid": "c1"},
    {"kind": "Job", "name": "backup-1", "uid": "j1", "ownerKind": "CronJob", "ownerName": "backup", "ownerUid": "c1"},
    {"kind": "Job", "name": "backup-2", "uid": "j2", "ownerKind": "CronJob", "ownerName": "backup", "ownerUid": "c1"},
    {"kind": "Job", "name": "backup-old", "uid": "j0", "ownerKind": "CronJob", "ownerName": "backup", "ownerUid": "c0-recreated"},
    {"kind": "Job", "name": "migrate", "uid": "j9", "ownerKind": None, "ownerName": None, "ownerUid": None},
    {"kind": "ReplicaSet", "name": "legacy", "uid": "r1"},
]
PODS = [{"name": "backup-1-abc", "owner": "Job/backup-1", "ownerUid": "j1"}, {"name": "backup-old-x", "owner": "Job/backup-old", "ownerUid": "j0"},
        {"name": "migrate-q", "owner": "Job/migrate", "ownerUid": "j9"}, {"name": "legacy-z", "owner": "ReplicaSet/legacy", "ownerUid": "r1"}]


def test_a_cronjobs_pods_are_those_of_the_jobs_it_owns_by_uid():
    owners, why = controllers_for(target("CronJob", "backup"), WORKLOADS)
    assert owners == {("Job", "backup-1", "j1"), ("Job", "backup-2", "j2")} and not why
    assert {p["name"] for p in PODS if owned_by(p, owners)} == {"backup-1-abc"}  # a recreated CronJob's old Job is not its own


@pytest.mark.parametrize("kind,name,pods", [("Job", "migrate", {"migrate-q"}), ("ReplicaSet", "legacy", {"legacy-z"})])
def test_jobs_and_standalone_replicasets_own_their_pods_directly(kind, name, pods):
    owners, _ = controllers_for(target(kind, name), WORKLOADS)
    assert {p["name"] for p in PODS if owned_by(p, owners)} == pods


def test_a_missing_target_or_a_cronjob_without_jobs_says_why():
    assert controllers_for(target("Job", "nope"), WORKLOADS) == (frozenset(), "there is no Job nope in shop")
    owners, why = controllers_for(target("CronJob", "backup"), [{"kind": "CronJob", "name": "backup", "uid": "c1"}])
    assert not owners and "no Jobs" in why


def test_deployment_only_tools_say_they_do_not_apply_to_other_kinds():
    for tool in ("rollout_history", "deployed_commit"):
        with pytest.raises(LookupError, match="no Deployment rollout history"):
            bind(BY_NAME[tool], target("CronJob", "backup"), {}, log_lines=10, members=Members(frozenset(), frozenset()))
    assert bind(BY_NAME["rollout_history"], target("Deployment", "cart"), {}, log_lines=10)["deployment"] == "cart"


def test_a_pod_target_reads_only_that_pod():
    t = target("Pod", "debug-shell")
    members = Members(frozenset({"debug-shell"}), frozenset())
    assert bind(BY_NAME["describe_pod"], t, {"pod": "debug-shell"}, log_lines=10, members=members)["pod"] == "debug-shell"
    from moks.crew.tools import Refused

    with pytest.raises(Refused):
        bind(BY_NAME["describe_pod"], t, {"pod": "other"}, log_lines=10, members=members)
