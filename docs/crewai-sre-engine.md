# CrewAI SRE investigation engine (opt-in)

The *Incident investigation* flow of the CrewAI console: seven specialised agents investigate an incident around one
workload, form hypotheses, have them challenged by an independent reviewer and produce a root-cause analysis in which
every finding and hypothesis is held to recorded evidence **in code**. It extends the existing CrewAI workload
investigation ([crewai-investigation.md](crewai-investigation.md)); it does not replace it, and LangGraph stays the
default orchestrator for everything else.

This document starts with the gap analysis that shaped the work (section 1), then describes what was built.

## 1. Gap analysis (2026-11-20, against `master` at 0f981cc)

What existed: the opt-in workload flow (`moks/crew`, 3 sequential agents, 14 read-only tools, evidence registry,
code-side citation and confidence checks, owner-reference scoping, Bridge-based cancellation), the CrewAI console
(runs, logs, transcripts, schedules, versioned crew configuration), its own worker and image, 79 tests with a
scripted model and `FakeKubernetes`.

| Requirement | Existed | Gap | Decision |
|---|---|---|---|
| Optional, `MOKS_CREWAI_ENABLED` | Yes: flag, `moks-crewai` image and worker, route 404 when off | none | Reuse unchanged. |
| LangGraph stays default | Yes: one branch in `Investigator._investigate` | none | Reuse the same branch. |
| Reuse tools, RBAC, LLM config, streaming, jobs, tracing | Yes: `ToolExecutor`, `budget.complete`, SSE events, worker claim classes, spans | none | Every new call goes through the same paths. |
| Python for CrewAI | Yes | none | |
| TypeScript for API, UI, MCP | UI is TypeScript (React). The API is Python (FastAPI). MOKS has no MCP server. | API language, MCP | **Not changed.** Rewriting the API in TypeScript would be the competing architecture the request rules out; the new endpoints extend the FastAPI routers. No MCP server is added: nothing in MOKS uses one yet. |
| Read-only tools only, no mutations | Yes: allowlist of `read_only`, non-`privileged` tools; the executor refuses privileged tools; actions are a separate approval workflow | none | New tools are read-only; a test asserts the allowlist. |
| **7 specialised agents** | 3 (workload, change, reviewer-and-writer) | Incident commander, a Kubernetes/observability split, a knowledge agent, a reviewer separate from the RCA author | New crew `sre` with the seven agents, as its own flow. The workload flow keeps its three. |
| Kubernetes: pods, restarts, CrashLoopBackOff, OOMKilled, ImagePullBackOff, Deployments/ReplicaSets, events, resources | Yes | none | |
| Kubernetes: node pressure | `k8s_nodes` exists, not offered to the crew | wiring | Offer it, narrowed to the target's nodes. |
| Kubernetes: scheduling failures | Via events (`FailedScheduling`) | none | |
| Kubernetes: HPA and KEDA | HPA read only for action plans; no KEDA read at all | read tool | New tool `k8s_autoscaling` (HPA v2; KEDA ScaledObjects when the CRD exists; RBAC adds read on `keda.sh`). |
| Kubernetes: PVC and storage | Connector method used by FinOps only | read tool | New tool `k8s_storage` (claims, phase, capacity, storage class, the pods that mount them). |
| Observability: Prometheus, Loki, Grafana | Tools exist; the crew had pod resources, alerts and Loki only | wiring | Offer Grafana dashboards and alerts, and a namespace-pinned metric reader. |
| Observability: error rate, latency percentiles, throughput | No tool | read tool | New tool `prom_service_signals`: code-built, namespace-pinned PromQL over Istio's standard metrics, with the OpenTelemetry HTTP metrics as fallback. No raw PromQL from the model. |
| Observability: saturation | cAdvisor CPU/memory/throttling | none | |
| Observability: OpenTelemetry/Jaeger traces | No trace connector; only trace-id log search | **trace backend** | Not built here: a Jaeger/Tempo connector is a new integration with its own credentials and permissions. The agent searches logs by trace id and records "no trace source" as missing telemetry. |
| Observability: service dependencies | `k8s_services` (selectors, endpoints) | dependency graph | Offer `k8s_services`; no new graph. |
| Change analysis: deployments, image changes, commits, configuration, Kubernetes manifests | Rollout history, deployed commit, commits, config audit | none | |
| Change analysis: Helm releases | Nothing; Helm keeps releases in Secrets, which MOKS never reads | by design | Changes are seen through Argo CD (`k8s_argocd_status`, now offered) and rollout history. |
| Change analysis: Terraform, infrastructure | Commits only | none | Commits filtered by path (the agent may pass `terraform/`, `deploy/`). |
| Change analysis: pull requests | No tool | PR listing | Not built: commits carry the merge information MOKS can read. |
| Change windows 15 m / 1 h / 6 h / 24 h | No | windows | Every change MOKS reads is classified **in code** into the smallest window it falls in; the lookback is a flow input. |
| Historical knowledge (RAG) | `kb_search` in the crew; `similar_incidents` not offered | wiring | Knowledge agent with both, under the person's access and the target's namespace. |
| Evidence reviewer | Reviewer existed but also wrote the report; no verdicts, no conflicts, no requests | independent review | Reviewer produces verdicts, conflicts, missing telemetry and evidence requests; a separate RCA agent writes the report. |
| Workflow steps 1-10 | Steps 1, 3, 4, 5, 10 (sequential) | hypotheses, validation, review, "ask for more" | Explicit 10-step workflow (section 3). |
| Agents request more evidence, bounded | No | loop | The reviewer's evidence requests are checked against the allowlist and the target in code and run by MOKS, for at most `review_rounds` rounds and `followup_calls` calls each. |
| Finding fields (ID, observation, source, resource, timestamp, time range, citation, confidence, hypothesis) | Statement plus evidence ids | most fields | Typed schema (`moks/crew/schema.py`); every field except the observation text is set by MOKS. |
| Fact vs hypothesis; four levels | Facts vs likely causes; low/medium/high | four levels | `confirmed observation`, `strongly supported`, `possible explanation`, `insufficient evidence`, from explicit criteria. |
| Confidence from criteria, not self-assessment | Capped by number of live sources | criteria list | A score from named criteria (section 4); the model's own confidence is ignored. |
| Short-term memory | CrewAI task context (raw model text) | structure, trust | A case file built by MOKS (plan, findings, hypotheses) passed to later tasks inside an untrusted-data block. |
| Evidence memory | Registry | none | Reused. |
| Task checkpoints | None (documented limitation: no resume) | checkpoints | A checkpoint after every task, through the investigation's fenced checkpoint row; a resumed attempt skips finished tasks. |
| No cross-investigation memory | CrewAI memory off | none | Stays off. The 2026-11-19 plan for LanceDB crew memory was never shipped and is not part of this engine. |
| Tenant/user/authorization boundaries | Investigation rows, `investigation.read`, scope | none | All artifacts live on the investigation (report, evidence, checkpoints, console run). |
| Timeouts | Crew and tool | per task | Per-task time limit; a task that reaches it ends with what it has, and the run continues. |
| Retries | `max_retry_limit=0`; HTTP retries inside providers | task-level policy | Transient model errors retried; invalid structured output retried with a CrewAI guardrail; a failed task retried once. |
| Cancellation | Bridge close | none | Reused. |
| Budgets | MOKS token budget, crew tool cap | crew token budget, per-agent tool caps | Added, with a reserve for review and RCA. |
| Token and model usage | Per step in the console history | in the report | Per agent and total in `report.crewai.usage`. |
| Partial results | Partial status, rule-based fallback | per task | Every task has its own status; the report says which ran. |
| Fallback to LangGraph | Rule-based report only | LangGraph | No usable RCA: LangGraph's report writer runs over the crew's evidence. Crew cannot start (no slot, flow off, version mismatch): the LangGraph investigation runs instead. Never for console test runs. |
| Structured errors | Free text | codes | `report.crewai.errors[{code, task, message}]`. |
| Untrusted logs, commits, annotations, documents | Observations wrapped; target fixed in code | earlier agents' output reached later agents raw | The case file is wrapped as untrusted data; reviewer requests are validated in code; tested with injected text in logs, commit messages, annotations and runbooks. |
| Scenario tests and metrics | 2 demo scenarios, no metrics | 10 scenarios, 7 metrics | Evaluation dataset `crewai_incidents` (fixture Kubernetes, Prometheus, Loki and Git) scored by `moks/crew/metrics.py`; `moks eval crewai_incidents --mode scripted` in CI, `--mode llm` on the CrewAI worker for a real model. |

## 2. Architecture

```
POST /investigations/crewai {flow: incident_investigation, …}      (api, default image: no crewai)
  └─ start_run: flow on? CrewAI worker running? inputs, RBAC, scope, audit → Investigation + seq-0 event {crewai: {flow, workload, kind, change_window, config_version}}
moks-crewai worker (class crewai) claims it → Investigator._investigate
  └─ crew/runner.run → crew/incident.run                         (crew cannot start → CrewFallback → LangGraph graph.run)
       ├─ IncidentRun (a crew/runner.Run): Bridge, ToolExecutor, owner-reference target, evidence registry, llm.budget, console history
       ├─ workflow(): one CrewAI task per crew, kicked off by MOKS, a checkpoint after each step
       │    plan → kubernetes → observability → change → knowledge → hypotheses → review ⟲ (MOKS collects requests) → rca
       │    after every step: crew/validation (parse, accept, grade, changes, severity) updates the Case
       └─ validation.to_report → Report + report.crewai {investigation, usage, errors, …}   (nothing usable → LangGraph's report writer)
  └─ the engine's usual validate_claims, verify_causes, relevance, stored report, conversation reply
```

Why MOKS drives the steps instead of `Process.sequential` or a CrewAI Flow: each step needs its own time limit,
retry and output check, the review loop must be bounded by code, and finished steps must survive a worker crash.
A Flow's state persistence writes a local SQLite file, which would put evidence on the pod's disk; MOKS uses its
own fenced checkpoint row instead. CrewAI still runs every agent: its ReAct loop, tool calling, guardrails and
`akickoff`.

| Module | Role |
|---|---|
| `crew/incident_config.py` | The crew: agents, tasks, tool allowlists, limits and ceilings, output contracts, the flow's inputs |
| `crew/config.py` | Every crew's configuration as the console versions it (`crew` argument everywhere) |
| `crew/incident.py` | The workflow, `IncidentRun` (limits, retries, usage, evidence requests, checkpoints), the result block |
| `crew/adapters.py` | `build_task_crew`, `IncidentLLM` and `IncidentTool` bound to one step, `evidence_list` and `evidence_detail` |
| `crew/schema.py` | What agents may answer (`Raw*`, lenient and bounded) and what MOKS stores (typed) |
| `crew/validation.py` | Everything decided about the answers |
| `crew/metrics.py` | Scoring against a scenario |
| `evals/crew_eval.py`, `evals/crew_fixtures.py`, `evals/datasets/crewai_incidents.yaml` | The ten scenarios |

## 3. Agents and workflow

| Step | Agent | Tools (all read-only MOKS tools through the `ToolExecutor`) | Answer |
|---|---|---|---|
| 1-2 plan | Incident commander | `namespace_health`, `workload_status`, `firing_alerts` | severity, affected resources, objectives |
| 3 kubernetes | Kubernetes investigator | pods, `describe_pod`, events, `pod_logs`, `pod_metrics`, `node_health`, `autoscaling` (HPA, KEDA), `storage` (PVCs), `services`, namespace health, workload status | findings |
| 4 observability | Observability investigator | `service_signals` (error ratio, p50/p95/p99, throughput), `search_logs`, `trace_logs`, `pod_metrics`, `metric` (namespace pinned), Prometheus and Grafana alerts, `dashboards`, `datastores`, `postgres_health`, `redis_health` | findings |
| 5 change | Change analyst | `rollout_history`, `changed_commits` (change window, optional path), `deployed_commit`, `config_audit`, `gitops_status` (Argo CD), events | findings |
| 6 knowledge | Historical knowledge analyst | `search_runbooks` (RAG under the person's access, target namespace), `similar_incidents` | findings (documentation, never live state) |
| 7 hypotheses | Incident commander | `evidence_list`, `evidence_detail` (registry only) | up to five hypotheses citing findings or evidence |
| 8-9 review | Evidence reviewer | `evidence_list`, `evidence_detail` | verdicts, conflicts, missing telemetry, evidence requests |
| 10 rca | Root cause analyst | `evidence_list`, `evidence_detail` | summary, root cause by hypothesis id, timeline, impact, next steps |

The target (environment, namespace, workload and its pods by owner reference, time window, change window) is bound
in code and never taken from a model; everything in [crewai-investigation.md](crewai-investigation.md) section 5
applies unchanged. A pod outside the workload, another namespace (as an argument or as a metric label) or a tool off
the allowlist is refused, audited and recorded as a gap.

**Asking for more evidence, bounded.** The reviewer's `evidence_requests` name a specialist tool and narrow arguments.
MOKS runs at most `followup_calls` of them per round, with the owning specialist's binding and checks, within the
crew's tool budget, then runs the review again, for at most `review_rounds` rounds. A round whose requests add no
evidence ends the loop. Evidence the reviewer cites for a `supported` or `contradicted` verdict joins the hypothesis's
evidence on that side.

**Short-term memory.** Later steps get MOKS's case file, built from what validation accepted: severity and its
criteria, affected resources, objectives, findings with ids, status and confidence, changes by window, graded
hypotheses, verdicts, conflicts, gaps, requests. It reaches the agent inside an untrusted-data block, because it
carries words agents wrote. CrewAI's own task context, memory, knowledge and planning are not used.

**MOKS's detectors.** `k8s_triage`'s automatic findings about the target (crash loops, OOM kills, image pulls…) join
the case as findings of task `detectors`, so they are on record even when an agent misses them.

## 4. Evidence and grading

Every finding: `id` (F1…), `observation` (the agent's words), `task`, `agent`, `resource` and `resource_verified`
(whether that name occurs in the cited evidence), `evidence_source` (MOKS tools), `citations` (evidence ids, all in
the registry), `timestamp` (when the newest cited evidence was read), `event_time` (the agent's time, kept only when
the cited evidence shows it to the minute), `time_range` (the query's window, or point in time), `confidence`,
`criteria`, `status`, `hypotheses` (those it supports), `review` (the reviewer's verdict).

A finding with no recorded citation is set aside as unsupported; one that cites only failed reads becomes a gap.
Confidence is the sum of the weights of the criteria that pass:

| Findings | Weight | Hypotheses | Weight |
|---|---|---|---|
| `live_evidence`: a successful live read is cited | 0.35 | `live_support` | 0.30 |
| `resource_in_evidence` | 0.25 | `independent_signals`: live support from two kinds (state, logs, metrics, changes, configuration, dependencies) | 0.20 |
| `complete_result`: not truncated or partial | 0.15 | `confirmed_findings`: a confirmed observation is cited | 0.15 |
| `fresh`: not historical after a resume | 0.10 | `no_contradiction`: no evidence, verdict or conflict against it | 0.15 |
| `not_challenged` by the reviewer | 0.15 | `reviewer_upheld` | 0.15 |
| | | `change_precedes`: a change before the symptoms is cited | 0.05 |

| Status | Findings | Hypotheses |
|---|---|---|
| confirmed observation | live evidence, resource verified, not challenged | never |
| strongly supported hypothesis | never | live support from two kinds of signal, uncontradicted, upheld by the reviewer |
| possible explanation | live but resource not verified, or documentation only | live or documentation support, uncontradicted |
| insufficient evidence | challenged, or nothing successful cited | contradicted, unsupported per the reviewer, or no support |

The report keeps confirmed observations as `observed_facts` and the hypotheses that are not insufficient as
`suspected_causes` (strongly supported: high; possible with live support: medium; documentation only: low). The root
cause is the RCA's choice only if it is not insufficient, else the top strongly supported hypothesis, else none. MOKS
never calls a root cause confirmed: an RCA summary that does is replaced by one MOKS writes. Invented citations are
rejected and listed.

**Changes.** Rollout revisions (with the image change), commits and Argo CD syncs in the lookback, each placed in the
smallest of 15m, 1h, 6h, 24h it falls in, measured from the end of the investigation window, and marked when it
precedes the first symptom MOKS found (a crashed container's end, a warning event, a firing alert).

**Severity.** From the evidence, not the commander: SEV1 production with no ready replica or a critical alert or 25%
errors; SEV2 an outage elsewhere, fewer ready replicas than desired, any firing alert in the namespace or 5% errors;
SEV3 restarts, p95 at 1 s or four times the window's lowest, or 1% errors; SEV4 when request metrics or alerts were read
and show no impact; `unknown` when nothing that measures impact could be read. The commander's proposal is kept beside it.

## 5. Reliability

| Limit (console-editable within its ceiling) | Default | Effect |
|---|---|---|
| `timeout_seconds` | 600 (≤ the CrewAI worker's `crewai.maxDurationSeconds`) | The whole crew; on expiry the report covers the steps that ran |
| `task_timeout_seconds` | 120 | A step past it gets MOKS's empty answer and ends; the run goes on (`task_timeout`) |
| `max_iterations` | 6 | CrewAI `max_iter`; past it + 2 model calls the step ends (`iteration_limit`) |
| `max_tool_calls` / `max_tool_calls_per_agent` | 32 (≤ `config.maxToolCalls`) / 8 | Crew-wide and per agent; the agent is told to answer with what it has |
| `token_budget` | 150 000 | Planning and specialists may use 75%; the rest is kept for hypotheses, review and RCA (`budget_exhausted`) |
| `review_rounds` / `followup_calls` | 2 / 4 | The evidence-request loop |
| `model_retries` | 1 | Provider `timeout`, `unavailable` or `bad_response` retried with backoff; budget refusals stop the crew |
| `output_retries` | 1 | A CrewAI guardrail sends a malformed answer back once with the reason; still malformed: `invalid_output` |
| (code) a failed step | 1 retry | Then `failed`, and the workflow goes on |

Cancellation closes the Bridge as in the workload flow. Every step is checkpointed through the investigation's fenced
checkpoint row; the reaper requeues a crashed run and the resumed attempt rebuilds the registry from the evidence table,
absorbs the finished steps' answers again and continues. Errors are structured: `report.crewai.errors[{code, task,
message}]`. Usage per agent (model calls, retries, tool calls, tokens, cost) is in `report.crewai.usage`.

**Fallback to LangGraph.** When the crew cannot start (no slot, the flow switched off, a configuration that no longer
passes, the wrong crewai version, no model) and `crewai.fallback` is `langgraph`, the engine runs its standard
investigation and records `crewai.status: fallback`. A console test run never falls back. When nothing the crew
answered survives validation, LangGraph's report writer runs over the evidence the crew collected (`langgraph_report`).

## 6. API, streaming and UI

- `POST /investigations/crewai` takes `flow` (`workload_investigation` by default, or `incident_investigation`) and,
  for the incident flow, `change_window` (`15m`, `1h`, `6h`, `24h`). The console's run and schedule forms take the same
  inputs (`GET /crewai/flows`).
- `GET /investigate/config` adds `crewai.flows[name]` with availability, limits, agent count and inputs, and
  `available_any`; the top-level fields stay the workload flow's.
- Events: a `phase` per step (`crewai_plan`, …, `crewai_evidence` for the review's requests) with
  `crewai: {task, agent, round, status}`, and a `note` when the step ends with its status, seconds and calls.
- The portal: a crew choice on the CrewAI form, the incident result (severity, root cause, hypotheses, findings,
  changes by window, timeline, review, what was set aside, usage), live steps in the progress card, and the crew
  editor for the incident crew (`/moks/crewai/crews/sre`).

## 7. Scenarios and metrics

`evals/datasets/crewai_incidents.yaml` records ten incidents for fixture Kubernetes, Prometheus, Loki and Git:
CrashLoopBackOff from a missing variable, a memory leak ending in OOMKilled, database connection exhaustion at the
HPA's maximum, latency after a deployment, HTTP 500s after a refactor, node CPU saturation, a failed rollout from a
missing ConfigMap, Redis connectivity, a RabbitMQ backlog behind a paused ScaledObject, and missing telemetry (no
root cause can be established). `crew/metrics.py` measures completeness, evidence quality, root-cause accuracy,
citation validity, unsupported conclusions, time and tokens; `evals/crew_eval.py` gates the dataset on them.

- `tests/test_crewai_scenarios.py` runs all ten with scripted agents (`tests/crew_script.py`). This measures MOKS's
  pipeline, not a model: the scripts know the answers, and several misbehave on purpose (an invented citation, a
  summary that calls the cause confirmed, an instruction injected in a commit message, a hypothesis the metrics
  contradict, a guess the reviewer sinks). All ten complete with the expected root cause; MOKS rejects the invented
  citation, rewrites the overclaiming summary and grades the contradicted hypotheses insufficient.
- A real model: `kubectl -n moks-sre exec deploy/moks-crewai -- moks eval crewai_incidents --mode llm` runs the same
  cases through the active chat model and prints each metric and gate. The eval page cannot start it: eval jobs run
  on workers without crewai.

## 8. Enable and roll back

The incident flow ships with the CrewAI worker: when `crewai.enabled` is true it is available, and an admin can switch
it off in the console like any flow. The worker's run time limit is `crewai.maxDurationSeconds` (600 by default); the
api reads it as `MOKS_CREWAI_MAX_DURATION_SECONDS` for the crews' timeout ceilings. KEDA ScaledObjects become readable
by `moks-reader` (`get`, `list`); TriggerAuthentications never are.

Roll back the flow alone by switching it off in the console; queued runs then run LangGraph (or fail, with
`crewai.fallback: none`). There is no migration.

## What is still unverified

- No real model has run the incident crew. Whether a given model follows seven roles' JSON contracts, uses evidence
  requests sensibly and stays within 150 000 tokens is what `moks eval crewai_incidents --mode llm` will show.
- Grading weights and severity thresholds are a starting point chosen for being explainable, not fitted to data.
- No trace backend: traces are followed through trace ids in logs only. Helm releases are seen through Argo CD and
  rollout history. Pull requests are not listed.
