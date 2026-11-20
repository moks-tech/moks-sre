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
