#!/usr/bin/env python3
"""Drives a running MOKS through its HTTP API, for scripts/e2e-kind.sh.

It signs in as the administrator the script created, asks MOKS why the lab's `payments` Deployment is crashing,
then has the executor restart the lab's healthy `moks-e2e` Deployment through an action plan. Nothing here is
faked: the reads go to the cluster's API server through the reader ServiceAccount, and the write goes through the
executor's own ServiceAccount and Role.

Standard library only, so it runs on a CI runner with nothing installed. Settings come from the environment:
MOKS_E2E_URL (http://127.0.0.1:18080/moks), MOKS_E2E_EMAIL, MOKS_E2E_PASSWORD.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from typing import Any

API = os.environ["MOKS_E2E_URL"].rstrip("/") + "/api"
ENVIRONMENT = os.environ.get("MOKS_E2E_ENVIRONMENT", "kind")
NAMESPACE = "moks-sre-lab"
INVESTIGATION_SECONDS = 300   # config.maxDurationSeconds is 180
PLAN_SECONDS = 300            # the executor validates a rollout for up to 180 s
PLAN_FINISHED = {"succeeded", "failed", "recovered", "reconciliation_required", "reconciled", "cancelled", "expired", "rejected"}

summary: list[str] = []


def ok(text: str) -> None:
    print(f"ok    {text}", flush=True)
    summary.append(f"- {text}")


def fail(text: str, detail: Any = None) -> None:
    print(f"FAIL  {text}", flush=True)
    if detail is not None:
        print(json.dumps(detail, indent=1, default=str)[:6000], flush=True)
    raise SystemExit(1)


class Session:
    """The session cookie is Secure, and this talks plain HTTP to a port-forward, so the cookie is carried by hand."""

    def __init__(self) -> None:
        self.cookie: str | None = None
        self.csrf: str | None = None

    def call(self, method: str, path: str, body: dict | None = None, expect: tuple[int, ...] = (200, 201)) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if self.cookie:
            headers["Cookie"] = f"moks_session={self.cookie}"
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw, cookies = response.status, response.read(), response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            status, raw, cookies = exc.code, exc.read(), []
        for value in cookies:
            jar: SimpleCookie = SimpleCookie()
            jar.load(value)
            if "moks_session" in jar:
                self.cookie = jar["moks_session"].value
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = raw.decode(errors="replace")[:800]
        if status not in expect:
            fail(f"{method} {path} answered HTTP {status}, expected {' or '.join(map(str, expect))}", parsed)
        return status, parsed

    def get(self, path: str) -> Any:
        return self.call("GET", path)[1]

    def post(self, path: str, body: dict) -> Any:
        return self.call("POST", path, body)[1]


def wait_for(what: str, seconds: int, read, done) -> Any:
    deadline, last = time.monotonic() + seconds, None
    while time.monotonic() < deadline:
        last = read()
        if done(last):
            return last
        time.sleep(3)
    fail(f"{what} did not finish within {seconds}s", {k: last.get(k) for k in ("status", "stage", "error", "result", "mutation_state")} if isinstance(last, dict) else last)


def sign_in(s: Session) -> None:
    s.call("POST", "/auth/login", {"email": os.environ["MOKS_E2E_EMAIL"], "password": os.environ["MOKS_E2E_PASSWORD"]})
    me = s.get("/auth/me")
    s.csrf = me["csrf_token"]
    if "action.execute" not in me["permissions"]:
        fail("the administrator is missing action.execute", me)
    ok(f"signed in as {me['email']} with a password (auth mode {me['auth_mode']})")


def investigate(s: Session) -> None:
    started = s.post("/investigations", {"question": "Why is payments crashing?", "environment": ENVIRONMENT, "namespace": NAMESPACE, "minutes": 30})
    inv = wait_for("the investigation", INVESTIGATION_SECONDS, lambda: s.get(f"/investigations/{started['id']}"), lambda i: i["status"] in ("completed", "failed", "cancelled"))
    report, evidence = inv.get("report") or {}, inv.get("evidence") or []
    print(json.dumps({"status": inv["status"], "mode": inv["mode"], "tool_calls": inv["tool_calls"], "error": inv["error"], "report": report,
                      "evidence": [{k: e[k] for k in ("ref", "tool", "ok", "kind", "summary")} for e in evidence]}, indent=1, default=str)[:20000], flush=True)
    if inv["status"] != "completed":
        fail(f"the investigation ended {inv['status']}: {inv['error']}")
    live = [e for e in evidence if e["ok"] and e["tool"].startswith("k8s_")]
    if not live:
        fail("no Kubernetes evidence was collected from the API server")
    if inv["mode"] != "deterministic" or report.get("generated_by") != "rules":
        fail(f"no LLM is configured, so this run should be stored as rule-based, not {inv['mode']}/{report.get('generated_by')}")
    ok(f"investigation completed without a model ({inv['tool_calls']} tool calls, {len(live)} successful Kubernetes reads)")

    text = json.dumps(report).lower()
    if "payments" not in text or "crashloopbackoff" not in text:
        fail("the report does not name payments and CrashLoopBackOff")
    ok("the report names the payments CrashLoopBackOff")

    refs = {e["ref"] for e in evidence}
    facts = report.get("observed_facts") or []
    if not facts:
        fail("the report states no observed facts")
    uncited = [f for f in facts if not f.get("evidence") or any(str(r).split(".")[0] not in refs for r in f["evidence"])]
    if uncited:
        fail("observed facts cite evidence that was not collected", uncited)
    ok(f"every observed fact ({len(facts)}) cites evidence that was collected")

    if "PAYMENT_MODE" not in json.dumps(evidence):  # only the container's log names it: the pod spec uses envFrom, and MOKS cannot read ConfigMaps
        fail("the container's own log line (invalid PAYMENT_MODE=lve), which is the cause, is not in the evidence")
    ok("the crash's own log line (invalid PAYMENT_MODE) is in the evidence")


def restart_through_a_plan(s: Session) -> None:
    plan = s.post("/actions/plans", {"environment": ENVIRONMENT, "namespace": NAMESPACE, "action": "restart_deployment", "target_kind": "Deployment",
                                     "target_name": "moks-e2e", "params": {}, "reason": "CI end-to-end test: one rolling restart on a kind cluster"})
    if plan["status"] != "pending" or plan["requires_approval"]:
        fail("a restart outside production, requested by an administrator, should be pending with no approval needed", plan)
    before = plan["pre_state"].get("revision")
    queued = s.post(f"/actions/plans/{plan['id']}/execute", {"plan_hash": plan["plan_hash"]})
    if queued["status"] != "queued":
        fail("the plan was not queued", queued)
    done = wait_for("the action plan", PLAN_SECONDS, lambda: s.get(f"/actions/plans/{plan['id']}"), lambda p: p["status"] in PLAN_FINISHED)
    print(json.dumps({k: done.get(k) for k in ("status", "mutation_state", "pre_state", "result", "executed_at", "finished_at")}, indent=1, default=str)[:8000], flush=True)
    if done["status"] != "succeeded" or not ((done.get("result") or {}).get("validation") or {}).get("ok"):
        fail(f"the plan ended {done['status']}", done.get("result"))
    ok(f"the executor restarted Deployment moks-e2e (revision {before} before) and validated the rollout")

    status, _ = s.call("POST", f"/actions/plans/{plan['id']}/execute", {"plan_hash": plan["plan_hash"]}, expect=(409,))
    ok(f"a second execute of the same plan was refused (HTTP {status})")


def main() -> None:
    s = Session()
    sign_in(s)
    investigate(s)
    restart_through_a_plan(s)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as out:
            out.write("### MOKS on kind\n\n" + "\n".join(summary) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        print(f"FAIL  could not reach {API}: {exc}", flush=True)
        sys.exit(1)
