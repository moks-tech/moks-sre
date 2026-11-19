# CrewAI console

**https://jithendar.com/moks/crewai** (Investigate → CrewAI console). One place to run, watch, schedule and change the
CrewAI workload investigation ([crewai-investigation.md](crewai-investigation.md)), modelled on the seo-agent's console in
moks-cms (`/admin/crewai`): runs read like GitHub Actions runs, flows and schedules like Jenkins jobs, and the crew's
prompts are versioned settings instead of code changes.

| Tab | What it shows | Who may change it |
|---|---|---|
| Overview | runs, success rate, median and p95 time, model cost (24 h and 7 days); running now; runs per day; each flow's last 10 runs; recent failures | — |
| Runs | every CrewAI run, numbered per flow (`#12`); filters (flow, status, trigger) in the URL; search by `#N`, run id, workload, namespace, error or person; 50 per page; refreshes while something runs | — |
| Run page | status, trigger, actor, target, time, cost; errors at the top; steps; summary with the report's counts and gaps; the **log**; cost by agent and model; inputs; **Re-run**, **Cancel**, **Save as new version** (test runs) | starting: `investigation.run` there |
| Flows | each flow with its health, schedules and crew version; **Run with parameters**; switch a flow **off or on** | on/off: `crewai.admin` |
| Schedules | cron schedules in the database: create, edit (live next-times preview), pause, resume, run now, delete; last start and its error | `crewai.admin` |
| Crews | the crew editor: every editable field against the code's default with a word diff, Check, Save as new version, Save and activate, **Test run**, versions with Activate and "Use the code's defaults" | `crewai.admin` |
| Settings | run history (transcripts on/off, retention); this deployment's limits; **Import / export** | `crewai.admin` |

Reading follows investigations: a run, its log and its transcripts are visible to whoever may read the investigation
(`investigation.read` in its environment and namespace); lists leave the rest out. `crewai.admin` is a new global
permission held only by the `admin` role. Every change is audited as `crewai.*` (`crewai.flow.disable`,
`crewai.schedule.create`, `crewai.crew.activate`, `crewai.config.import`, …).

## Runs, steps and the log

A CrewAI run is an investigation with a `crew_runs` row (migration 0036): its flow, its number (`crew_run_counters`, one
counter per flow), its trigger (`user`, `rerun`, `schedule`, `test`), the inputs it ran with, the crew configuration
version it recorded when it was queued, and the schedule or run it came from. `POST /investigations/crewai`, the form on
New investigation, is now the console's flow too, so those runs are numbered and logged like the rest.

The CrewAI worker writes what the run page shows (`moks/crew/history.py`):

| Table | Written | What |
|---|---|---|
| `crew_run_steps` | at each stage | *Set up run*, one step per agent (named by its role), *Validate the report*: running → succeeded · failed · cancelled, with model calls, tool calls, tokens and cost per step |
| `crew_run_logs` | buffered, every 0.5 s, at each step change and at the end | capability, model, configuration and limits; every model call (tokens, cost, seconds, with a link to its transcript); every tool call (arguments, evidence id, outcome, duration); refusals, gaps, limits reached; the validation (invented citations rejected, uncited claims removed, confidence lowered) |
| `crew_run_calls` | per model call, while *transcripts* is on | system prompt, the turns sent and the answer (each cut at 200,000 characters), tokens, cost, duration, outcome |

Writes are best effort in their own transactions: a failed write is logged once (`crewai_history_failed`) and never
fails or slows the run. A run keeps at most 5,000 log lines. A step still running when the investigation ended outside
the crew (a worker that died, a failure after the crew) is shown with the investigation's outcome; steps that never
started are *skipped*.

The log viewer groups lines by step (a failed or running group opens by itself), hides debug lines until asked, searches
with highlights, shows timestamps, links lines (`#L123`), follows a live run (it polls every 1.5 s), downloads the log
as text, shows a line's details (`{…}`) and opens a model call's transcript (answer, prompt, system) beside the log.

What lands in the log and transcripts is MOKS's own text and the evidence excerpts the model was shown, which the
executor redacted before they were stored (the same excerpts the report already carries). Nothing else is copied.

**Retention** (Settings → Run history): log lines 90 days, transcripts 14 days, transcripts on. The worker deletes older
rows once an hour; runs, steps and cost stay.

## Flows and Run with parameters

A flow is a crew plus its inputs (`moks/crew/config.py` `FLOWS`). Today there is one, *Workload investigation*:
environment, namespace, kind, workload, window (minutes) and an optional question. The form is built from that list and
the API checks the same list (`console.clean_inputs`), so a field the flow does not have, a bad Kubernetes name or a
window out of range is refused with the field named. Empty fields take the crew version's defaults (kind, window).

**Switching a flow off** (Flows → Switch off, with an optional reason) is immediate: nobody can start it (409, with the
reason), New investigation stops offering it, schedules skip their times and record why, and runs already queued fail
when they start. `MOKS_CREWAI_ENABLED` still decides whether CrewAI exists at all; the switch decides within that.

**Re-run** starts a finished run again with the same inputs, or edited ones, on the crew's **active** configuration
(not the one the first run used: that is what Test run is for). The new run links back to the old one.

## Schedules

`crew_schedules` replaces nothing on mk4 (there were no CrewAI CronJobs); it is how a CrewAI run is scheduled at all.

- Five-field cron in **UTC** (`moks/crew/cron.py`): numbers, ranges, steps, lists, month and weekday names, `@hourly`,
  `@daily`, `@weekly`, `@monthly`; both day fields restricted means either matches, as in cron. A schedule may start a run
  **at most every 15 minutes** (each run is a model-backed investigation), and one that never fires is refused.
- **Runs as the person who last saved it.** Saving needs `crewai.admin` and `investigation.run` where it runs; every start
  checks that person's access again, so a schedule never reaches further than its owner can today. When the owner has
  lost access, the start is refused and the schedule shows *"the schedule's owner may no longer run it"*.
- **Who fires it.** Every worker's housekeeping (every 30 s) runs the dispatcher; a compare-and-set on `next_run_at` lets
  one of them start the run. A time missed by more than 15 minutes (no worker ran) is **skipped, not caught up**, and
  recorded as the last error. A changed time or a resume counts from now, so it never fires a time already past.
- The worker may only record a firing: column grants give `moks_worker` `UPDATE (next_run_at, last_run_at, last_run_id,
  last_error)` on `crew_schedules` and nothing on crew versions or settings (`db_roles.py`).
- **Run now** runs the schedule's inputs once, started by and as the person who clicks.

## Crew versions and Test run

What a version may change, and what stays in code (`moks/crew/config.py`):

| Editable (a version) | Code-owned (never in a version) |
|---|---|
| each agent's role, goal and backstory | three agents in sequence; no delegation, memory, planning or reasoning |
| each task's description and expected output | the untrusted-data rule appended to every backstory |
| which of the agent's **allowlisted** tools it gets (a tool never moves to another agent; the reviewer keeps `evidence_list` only) | the tool allowlist, each tool's agent and argument binding, the read-only executor |
| limits: time, tool calls, iterations, output tokens per step and for the report, within ceilings (`MOKS_MAX_DURATION_SECONDS`, `MOKS_MAX_TOOL_CALLS`) | the reviewer's JSON output contract, citation and confidence validation, the evidence registry |
| default kind and window | |

A version stores only what differs from the code's defaults (an overlay), so a default that changes in code reaches every
field nobody edited, and a version whose base changed is marked **upstream changed**. Placeholders `{target}` and
`{question}` are filled with plain replacement (braces elsewhere are text); `{question}` is the person's question inside
an untrusted-data block. **Check** reports errors (block saving and running: unknown fields, a tool off the allowlist, an
empty role, two agents with one role, a limit out of range) and warnings (an unknown placeholder, a missing `{target}` or
`{question}`, an agent with no tools).

At most one version is active per crew; none means the code's defaults. A run records the version at queue time
(`config_version` on the run, `crewai.config_version` in its queued event), so activating another version never changes
a run already queued, and the worker checks the version again when it starts.

**Test run** (Jenkins' Replay) runs the unsaved draft on a finished run's inputs, as the admin who clicks. The draft
travels in the run's queued event (never in the audit log); the run page then offers **Save as new version**. Test runs
are never re-run: test again from the crew page.

## Import / export

One JSON document (`moks-crewai-console/1`): each crew's active version as an overlay, every schedule (flow, name, cron,
inputs, enabled), which flows are off, and the run-history settings. No runs, logs, transcripts or owners. Import shows
a plan first (add, change, same, per item), then applies the same plan. It only adds and changes: nothing is deleted, an
imported crew configuration becomes a new active version, imported schedules run as the importer, and every item is
checked as if it were entered by hand (a schedule needs the importer's `investigation.run` there).

## API

All under `/moks/api/crewai` (`moks/api/crewai.py`).

| Method and path | What |
|---|---|
| `GET /overview` | the Overview tab |
| `GET /runs?flow&status&trigger&q&environment&page` | runs (status also `active`, `partial`) |
| `GET /runs/{id}` · `GET /runs/{id}/logs?after&limit&format=json\|text` · `GET /calls/{id}` | a run, its log, a transcript |
| `POST /runs/{id}/rerun` `{inputs?}` · `POST /runs/{id}/cancel` · `POST /runs/{id}/save-version` `{note, activate}` | |
| `GET /flows` · `GET /flows/{name}` · `POST /flows/{name}/run` `{inputs}` · `PATCH /flows/{name}` `{enabled, reason}` | |
| `GET /schedules` · `POST /schedules` · `PATCH /schedules/{id}` · `DELETE /schedules/{id}` · `POST /schedules/{id}/run` · `GET /cron?expr` | |
| `GET /crews` · `GET /crews/{crew}` · `GET /crews/{crew}/versions/{n}` · `POST /crews/{crew}/check` · `POST /crews/{crew}/versions` · `POST /crews/{crew}/activate` `{version\|null}` · `POST /crews/{crew}/test` `{config, run_id}` | |
| `GET /settings` · `PATCH /settings` · `GET /export` · `POST /import` `{data, apply}` · `GET /environments` | |

## Rolling out

Migration 0036 and the new tables' grants are applied by the migrate job on deploy (`templates/migrate.yaml` runs
`moks migrate && moks db-grants`). Nothing needs configuring: transcripts are on with the
retention above, every flow is on, the crew runs the code's defaults until a version is activated. Both images (`moks`
and `moks-crewai`) must run this commit: the API writes runs and versions, the CrewAI worker writes steps, logs and
transcripts. Runs queued before the upgrade have no `crew_runs` row; they run as before and are not in the console.

Roll back: switch the flow off in the console, or `crewai.enabled: false` as before. Migration 0036's downgrade drops
the eight tables.

## Also in this change

`Investigator._emit` took an investigation event's number after an await, so two events in flight (an agent's posted
"finished" note and the next model call's usage) could take the same number; the second insert failed and the crew
stopped. The number is now taken before the await (`test_two_events_in_flight_never_share_a_number`).
