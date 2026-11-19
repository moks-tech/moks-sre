import { useEffect, useState, type FormEvent } from "react";
import { Link, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, qs } from "../api";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Stat, Time, useInterval, useLoad } from "../components";
import { CrewEditor } from "../crewai/CrewEditor";
import { LogViewer } from "../crewai/LogViewer";
import { ConsoleTabs, duration, InputsForm, money, pct, RunFlow, type RunRow, Section, Squares, StatusIcon, type Step, StepIcon, TRIGGERS, When } from "../crewai/shared";
import { useUrlFilters } from "../filters";
import { Dialog, Empty, useFeedback } from "../ui";

// The CrewAI console (docs/crewai-console.md): CrewAI runs as GitHub Actions shows workflow runs, flows and schedules as
// Jenkins shows jobs, and the crew's configuration as versions. Reading needs investigation.read where a run ran;
// changing the console needs crewai.admin. The backend checks both; this page only hides what would be refused.

const target = (r: RunRow) => `${r.kind ?? ""} ${r.namespace}/${r.workload ?? "?"}`.trim();

function Stats({ title, s }: { title: string; s: any }) {
  return <div className="card stack">
    <h3>{title}</h3>
    <div className="cw-stats">
      <Stat value={s.runs} label="runs" />
      <Stat value={pct(s.success_rate)} label="succeeded" />
      <Stat value={duration(s.median_seconds)} label="median time" />
      <Stat value={duration(s.p95_seconds)} label="p95 time" />
      <Stat value={money(s.cost_usd)} label="model cost" />
    </div>
  </div>;
}

function RunTable({ rows, label, empty }: { rows: RunRow[]; label: string; empty?: string }) {
  const nav = useNavigate();
  return <DataTable label={label} rows={rows} rowKey={(r) => r.id} onRow={(r) => nav(`/crewai/runs/${r.id}`)} empty={<Empty title={empty ?? "No runs."} />} cols={[
    { key: "s", head: "", status: true, cell: (r) => <StatusIcon status={r.status} crew_status={r.crew_status} /> },
    { key: "n", head: "Run", primary: true, cell: (r) => <span><strong>{r.flow_label} #{r.number}</strong><span className="cw-sub">{target(r)} · {r.environment}</span></span> },
    { key: "t", head: "Trigger", cell: (r) => <span className="small">{TRIGGERS[r.trigger] ?? r.trigger}{r.schedule ? `: ${r.schedule}` : ""}<span className="cw-sub">{r.actor ?? ""}</span></span> },
    { key: "c", head: "Config", wide: true, cell: (r) => <span className="small muted">{r.config_version ? `v${r.config_version}` : r.trigger === "test" ? "draft" : "defaults"}</span> },
    { key: "at", head: "Started", cell: (r) => <Time iso={r.started_at ?? r.created_at} /> },
    { key: "d", head: "Duration", cell: (r) => <span className="small">{duration(r.seconds)}</span> },
    { key: "$", head: "Cost", wide: true, cell: (r) => <span className="small">{money(r.cost_usd)}</span> },
  ]} />;
}

// ---- overview -------------------------------------------------------------------------------

function PerDay({ days }: { days: any[] }) {
  const max = Math.max(1, ...days.map((d) => d.completed + d.failed + d.cancelled + d.running + d.queued));
  return <div className="cw-bars" role="img" aria-label={`Runs per day: ${days.map((d) => `${d.date} ${d.completed + d.failed + d.cancelled + d.running + d.queued}`).join(", ")}`}>
    {days.map((d) => <div key={d.date} className="cw-bar-col" title={`${d.date}: ${d.completed} succeeded, ${d.failed} failed, ${d.cancelled} cancelled`}>
      <div className="cw-bar">
        {(["failed", "cancelled", "completed", "running", "queued"] as const).map((k) => d[k] ? <span key={k} className={`cw-seg ${k}`} style={{ height: `${(d[k] / max) * 100}%` }} /> : null)}
      </div>
      <span className="cw-bar-label">{new Date(d.date + "T00:00:00Z").toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" })}</span>
    </div>)}
  </div>;
}

function Overview() {
  const load = useLoad(() => api("/crewai/overview"), []);
  const d = load.data;
  useInterval(load.reload, 5000, !!d?.running?.length);
  if (!d) return <Loading load={load} rows={6} />;
  return <div className="stack">
    <div className="grid cols-2"><Stats title="Last 24 hours" s={d.day} /><Stats title="Last 7 days" s={d.week} /></div>
    <Section title={<>Running now {d.running.length > 0 && <Badge kind="info">{d.running.length}</Badge>}</>}>
      <RunTable rows={d.running} label="Running now" empty="Nothing is queued or running." />
    </Section>
    <div className="grid cols-2">
      <Section title="Runs per day"><PerDay days={d.per_day} /></Section>
      <Section title="Recent failures"><RunTable rows={d.failures} label="Recent failures" empty="No failed runs." /></Section>
    </div>
    <Section title="Flows" actions={<Link className="btn ghost small" to="/crewai/flows">All flows</Link>}>
      <DataTable label="Flows" rows={d.flows} rowKey={(f: any) => f.name} cols={[
        { key: "n", head: "Flow", primary: true, cell: (f: any) => <span><Link to={`/crewai/flows/${f.name}`}><strong>{f.label}</strong></Link> {!f.enabled && <Badge kind="warn">off</Badge>}</span> },
        { key: "h", head: "Last 10", cell: (f: any) => <Squares runs={f.last} /> },
        { key: "l", head: "Last run", cell: (f: any) => f.last_run ? <span className="small"><Link to={`/crewai/runs/${f.last_run.id}`}>#{f.last_run.number}</Link> <Time iso={f.last_run.created_at} /></span> : <span className="muted">—</span> },
        { key: "m", head: "Median", cell: (f: any) => <span className="small">{duration(f.week.median_seconds)}</span> },
        { key: "r", head: "Runs (7 d)", cell: (f: any) => <span className="small">{f.week.runs} · {pct(f.week.success_rate)}</span> },
        { key: "c", head: "Cost (7 d)", wide: true, cell: (f: any) => <span className="small">{money(f.week.cost_usd)}</span> },
      ]} />
    </Section>
  </div>;
}

// ---- runs -----------------------------------------------------------------------------------

function Runs() {
  const [f, setF] = useUrlFilters({ flow: "", status: "", trigger: "", q: "", page: 1 });
  const [q, setQ] = useState(f.q);
  const load = useLoad(() => api(`/crewai/runs${qs({ flow: f.flow || undefined, status: f.status || undefined, trigger: f.trigger || undefined, q: f.q || undefined, page: f.page })}`), [f.flow, f.status, f.trigger, f.q, f.page]);
  const d = load.data;
  useInterval(load.reload, 4000, !!d?.active);
  function search(e: FormEvent) { e.preventDefault(); setF({ q, page: 1 }); }
  return <div className="stack">
    <form className="filters card" onSubmit={search} aria-label="Filter runs">
      <label className="grow">Search<input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="#12, a run id, a workload, namespace, error or person" /></label>
      <label>Flow<select value={f.flow} onChange={(e) => setF({ flow: e.target.value, page: 1 })}><option value="">all</option><option value="workload_investigation">Workload investigation</option></select></label>
      <label>Status<select value={f.status} onChange={(e) => setF({ status: e.target.value, page: 1 })}>
        <option value="">any</option><option value="active">queued or in progress</option><option value="completed">succeeded</option><option value="partial">completed, partial</option>
        <option value="failed">failed</option><option value="cancelled">cancelled</option></select></label>
      <label>Trigger<select value={f.trigger} onChange={(e) => setF({ trigger: e.target.value, page: 1 })}>
        <option value="">any</option>{Object.entries(TRIGGERS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <button className="ghost">Search</button>
    </form>
    <Loading load={load} rows={8} />
    {d && <>
      <div className="small muted">{d.total} run{d.total === 1 ? "" : "s"}{d.capped ? " (newest 5000)" : ""}{d.active ? ` · ${d.active} queued or in progress, refreshing` : ""}</div>
      <RunTable rows={d.runs} label="Runs" empty="No run matches." />
      {d.pages > 1 && <div className="row end">
        <button type="button" className="ghost small" disabled={f.page <= 1} onClick={() => setF({ page: f.page - 1 })}>Newer</button>
        <span className="small">Page {d.page} of {d.pages}</span>
        <button type="button" className="ghost small" disabled={f.page >= d.pages} onClick={() => setF({ page: f.page + 1 })}>Older</button>
      </div>}
    </>}
  </div>;
}

function RerunDialog({ run, onClose }: { run: any; onClose: () => void }) {
  const nav = useNavigate();
  const flows = useLoad(() => api("/crewai/flows"), []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const flow = flows.data?.flows?.find((x: any) => x.name === run.run.flow);
  async function go(inputs: Record<string, any>) {
    setBusy(true); setError(undefined);
    try { const r = await api<{ id: string }>(`/crewai/runs/${run.run.id}/rerun`, { method: "POST", body: { inputs } }); onClose(); nav(`/crewai/runs/${r.id}`); }
    catch (e) { setError(e); setBusy(false); }
  }
  return <Dialog title={`Re-run #${run.run.number}`} onClose={onClose} wide>
    <div className="stack">
      <div className="small muted">The same inputs, or edited ones, on the crew's active configuration.</div>
      <Loading load={flows} />
      {flow && <InputsForm spec={flow.inputs} environments={flows.data.environments} values={run.inputs} submitLabel="Re-run" busy={busy} onSubmit={go} />}
      <ErrorBox error={error} />
    </div>
  </Dialog>;
}

function RunPage() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { confirm, toast } = useFeedback();
  const load = useLoad(() => api(`/crewai/runs/${id}`), [id]);
  const d = load.data;
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [rerun, setRerun] = useState(false);
  const [error, setError] = useState<unknown>();
  const live = d && (d.run.status === "queued" || d.run.status === "running");
  useEffect(() => setSteps(null), [id]);
  useInterval(load.reload, 3000, !!live);
  if (!d) return <Loading load={load} rows={8} />;
  const r: RunRow = d.run;
  const shownSteps: Step[] = steps ?? d.steps;
  async function cancel() {
    if (!(await confirm({ title: `Cancel #${r.number}?`, body: "The crew stops at its next step. What it collected so far stays in the report.", action: "Cancel run", danger: true }))) return;
    try { await api(`/crewai/runs/${id}/cancel`, { method: "POST", body: {} }); toast("Cancelling"); load.reload(); } catch (e) { setError(e); }
  }
  async function saveVersion() {
    try { const v = await api(`/crewai/runs/${id}/save-version`, { method: "POST", body: { activate: false } }); toast(`Saved as version ${v.version}`); nav("/crewai/crews/workload"); }
    catch (e) { setError(e); }
  }
  const crew = d.report?.crewai;
  return <div className="stack">
    <div className="cw-runhead card">
      <div className="row between">
        <div className="stack tight">
          <div className="row"><StatusIcon status={r.status} crew_status={r.crew_status} label /><h2 className="cw-h2">{r.flow_label} #{r.number}</h2>
            {r.trigger === "test" && <Badge kind="info">test run</Badge>}</div>
          <div className="small muted">
            {TRIGGERS[r.trigger] ?? r.trigger}{r.schedule ? ` (${r.schedule})` : ""} by {r.actor ?? "unknown"} · {target(r)} in {r.environment} · <Time iso={r.created_at} /> · {duration(r.seconds)} · {money(r.cost_usd)}
          </div>
          <div className="small">{d.prev && <Link to={`/crewai/runs/${d.prev.id}`}>← #{d.prev.number}</Link>} {d.next && <Link to={`/crewai/runs/${d.next.id}`}>#{d.next.number} →</Link>}</div>
        </div>
        <div className="row">
          {r.conversation_id && <Link className="btn ghost" to={`/conversations/${r.conversation_id}`}>Open report</Link>}
          {d.can.save_version && <button type="button" className="ghost" onClick={saveVersion}>Save as new version</button>}
          {d.can.rerun && <button type="button" onClick={() => setRerun(true)}>Re-run</button>}
          {d.can.cancel && <button type="button" className="danger" onClick={cancel}>Cancel</button>}
        </div>
      </div>
      <ErrorBox error={error} />
      {d.errors.length > 0 && <div className="alert error" role="alert"><strong>{d.errors.length === 1 ? "Error" : "Errors"}</strong><ul className="small">{d.errors.map((e: string, i: number) => <li key={i}>{e}</li>)}</ul></div>}
    </div>
    <div className="cw-runbody">
      <aside className="cw-steps card" aria-label="Steps">
        <h3>Steps</h3>
        <ol>{shownSteps.map((s: Step) => <li key={s.step} className={s.status}><StepIcon status={s.status} /><span className="grow">{s.label}</span><span className="small muted">{s.status === "pending" || s.status === "skipped" ? "" : duration(s.seconds)}</span></li>)}</ol>
        <h3>Configuration</h3>
        <div className="small">{d.config.draft ? "Test draft (not saved)" : d.config.version ? <Link to="/crewai/crews/workload">Version {d.config.version}</Link> : "The code's defaults"}</div>
        {d.rerun_of && <><h3>{r.trigger === "test" ? "Tested on" : "Re-run of"}</h3><Link className="small" to={`/crewai/runs/${d.rerun_of.id}`}>#{d.rerun_of.number}</Link></>}
        {d.reruns.length > 0 && <><h3>Runs started from this one</h3><ul className="cw-mini">{d.reruns.map((x: any) => <li key={x.id}><StatusIcon status={x.status} /> <Link to={`/crewai/runs/${x.id}`}>#{x.number}</Link> <span className="small muted">{TRIGGERS[x.trigger]}</span></li>)}</ul></>}
      </aside>
      <div className="stack cw-runmain">
        <Section title="Summary">
          {d.report.summary ? <p className="cw-summary">{d.report.summary}</p> : <div className="small muted">{live ? "The report appears when the run finishes." : "No report."}</div>}
          {!live && <div className="row small">
            <Badge kind="neutral">{d.report.findings} finding{d.report.findings === 1 ? "" : "s"}</Badge>
            <Badge kind="neutral">{d.report.causes} likely cause{d.report.causes === 1 ? "" : "s"}</Badge>
            {crew && <Badge kind={crew.status === "completed" ? "ok" : "warn"}>crew {crew.status}</Badge>}
            {d.report.mode && <Badge kind="neutral">{d.report.mode}</Badge>}
            {r.conversation_id && <Link to={`/conversations/${r.conversation_id}`}>Read the full report and evidence</Link>}
          </div>}
          {crew?.stopped && <div className="alert warn small">Stopped early: {crew.stopped}. The report covers the evidence collected before that.</div>}
          {crew?.validation && <div className="small muted">Checked by MOKS: {(crew.validation.citations_rejected ?? []).length} invented citation(s) rejected, {(crew.validation.claims_removed ?? []).length} uncited claim(s) removed, {(crew.validation.confidence_lowered ?? []).length} confidence level(s) lowered.</div>}
          {crew?.gaps?.length > 0 && <details><summary className="small">{crew.gaps.length} gap(s) in the evidence</summary><ul className="small">{crew.gaps.map((g: any, i: number) => <li key={i}><span className="mono">{g.source}</span>: {g.reason}</li>)}</ul></details>}
        </Section>
        <LogViewer runId={id} initialSteps={d.steps} onSteps={(s, status) => { setSteps(s); if (status !== r.status) load.reload(); }} />
        <div className="grid cols-2">
          <Section title="Cost by agent">
            {d.cost.by_agent.length ? <DataTable label="Cost by agent" rows={d.cost.by_agent} rowKey={(x: any) => x.step} cols={[
              { key: "a", head: "Agent", primary: true, cell: (x: any) => x.label },
              { key: "m", head: "Model calls", cell: (x: any) => x.model_calls },
              { key: "t", head: "Tool calls", cell: (x: any) => x.tool_calls },
              { key: "k", head: "Tokens", cell: (x: any) => `${x.tokens_in} / ${x.tokens_out}` },
              { key: "c", head: "Cost", cell: (x: any) => money(x.cost_usd) },
            ]} /> : <div className="small muted">No model calls recorded.</div>}
            {d.cost.by_model.map((m: any) => <div key={m.model} className="small muted">{m.model}: {m.calls} call(s), {m.tokens_in} in / {m.tokens_out} out, {money(m.cost_usd)}</div>)}
          </Section>
          <Section title="Inputs">
            <dl className="kv">{Object.entries(d.inputs).map(([k, v]) => <div key={k} className="kv-row"><dt>{k}</dt><dd className="mono">{String(v)}</dd></div>)}</dl>
            {crew?.limits && <div className="small muted">Limits: {crew.limits.timeout_seconds}s, {crew.limits.max_tool_calls} tool calls, {crew.limits.max_iterations_per_agent} iterations per agent.</div>}
          </Section>
        </div>
      </div>
    </div>
    {rerun && <RerunDialog run={d} onClose={() => setRerun(false)} />}
  </div>;
}

// ---- flows ----------------------------------------------------------------------------------

function FlowSwitch({ flow, onDone }: { flow: any; onDone: () => void }) {
  const { confirm, toast } = useFeedback();
  const [error, setError] = useState<unknown>();
  async function flip() {
    const off = flow.enabled;
    if (off && !(await confirm({ title: `Switch off ${flow.label}?`, body: "Nobody can start it, its schedules skip their times (and say why), and runs already queued fail when they start.", action: "Switch off", danger: true }))) return;
    try { await api(`/crewai/flows/${flow.name}`, { method: "PATCH", body: { enabled: !off } }); toast(off ? "Switched off" : "Switched on"); onDone(); } catch (e) { setError(e); }
  }
  return <><button type="button" className={flow.enabled ? "ghost" : ""} onClick={flip}>{flow.enabled ? "Switch off" : "Switch on"}</button><ErrorBox error={error} /></>;
}

function FlowCard({ f, admin, why, reload }: { f: any; admin: boolean; why: string | null; reload: () => void }) {
  return <Section title={<><Link to={`/crewai/flows/${f.name}`}>{f.label}</Link> {f.enabled ? <Badge kind="ok">on</Badge> : <Badge kind="warn">off</Badge>}</>}
    actions={admin ? <FlowSwitch flow={f} onDone={reload} /> : undefined}>
    <div className="small">{f.description}</div>
    {!f.enabled && <div className="alert warn small">Switched off{f.reason ? `: ${f.reason}` : ""}{f.changed_at ? <> · <Time iso={f.changed_at} /></> : null}.</div>}
    <div className="row small"><Squares runs={f.last} /><span className="muted">{f.week.runs} run(s) in 7 days · {pct(f.week.success_rate)} succeeded · median {duration(f.week.median_seconds)} · {money(f.week.cost_usd)}</span></div>
    <div className="small muted">Crew <Link to={`/crewai/crews/${f.crew}`}>{f.config_version ? `version ${f.config_version}` : "code defaults"}</Link> · {f.schedules} schedule(s)</div>
    <RunFlow flow={f} disabled={!f.enabled || !!why} why={!f.enabled ? "The flow is switched off." : why} />
  </Section>;
}

function Flows() {
  const load = useLoad(() => api("/crewai/flows"), []);
  const d = load.data;
  if (!d) return <Loading load={load} />;
  return <div className="stack">
    {!d.crewai_enabled && <div className="alert warn">CrewAI is off in this deployment (MOKS_CREWAI_ENABLED). Runs and schedules start nothing until it is on.</div>}
    {d.crewai_enabled && d.unavailable && <div className="alert warn small">No run can start right now: {d.unavailable}.</div>}
    {d.flows.map((f: any) => <FlowCard key={f.name} f={f} admin={d.can_admin} why={d.unavailable} reload={load.reload} />)}
  </div>;
}

function FlowPage() {
  const { name = "" } = useParams();
  const load = useLoad(() => Promise.all([api(`/crewai/flows/${name}`), api("/crewai/flows")]), [name]);
  if (!load.data) return <Loading load={load} />;
  const [f, all] = load.data;
  return <div className="stack">
    <FlowCard f={f} admin={all.can_admin} why={all.unavailable} reload={load.reload} />
    <Section title="Parameters"><DataTable label="Parameters" rows={f.inputs} rowKey={(i: any) => i.name} cols={[
      { key: "n", head: "Input", primary: true, cell: (i: any) => <span className="mono">{i.name}</span> },
      { key: "l", head: "Label", cell: (i: any) => i.label },
      { key: "t", head: "Type", cell: (i: any) => i.type === "choice" ? (i.choices ?? []).join(" | ") : i.type },
      { key: "r", head: "Required", cell: (i: any) => i.required ? "yes" : "no" },
      { key: "d", head: "Default", cell: (i: any) => <span className="small">{f.defaults[i.name] ?? i.default ?? "—"}</span> },
    ]} /></Section>
    <Section title="Schedules" actions={<Link className="btn ghost small" to="/crewai/schedules">Manage</Link>}>
      {f.schedule_list.length ? <ul className="cw-mini">{f.schedule_list.map((s: any) => <li key={s.id}><strong>{s.name}</strong> <code>{s.cron}</code> {s.enabled ? <span className="small muted">next <When iso={s.next_run_at} /></span> : <Badge kind="neutral">paused</Badge>}</li>)}</ul>
        : <div className="small muted">No schedules.</div>}
    </Section>
    <Section title="Run history" actions={<Link className="btn ghost small" to={`/crewai/runs?flow=${name}`}>All runs</Link>}><RunTable rows={f.runs} label="Run history" /></Section>
  </div>;
}

// ---- schedules --------------------------------------------------------------------------------

function CronPreview({ expr }: { expr: string }) {
  const [p, setP] = useState<any>();
  useEffect(() => {
    if (!expr.trim()) { setP(undefined); return; }
    const t = setTimeout(() => api(`/crewai/cron${qs({ expr })}`).then(setP, () => setP(undefined)), 250);
    return () => clearTimeout(t);
  }, [expr]);
  if (!p) return <div className="small muted">Five fields, UTC: minute hour day-of-month month day-of-week (or @hourly, @daily, @weekly).</div>;
  if (!p.valid) return <div className="small danger-text">{p.error}</div>;
  return <div className="small">Next: {p.next.map((t: string) => new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })).join(" · ")}</div>;
}

function ScheduleDialog({ schedule, flows, onClose, onSaved }: { schedule?: any; flows: any; onClose: () => void; onSaved: () => void }) {
  const flow = flows.flows.find((x: any) => x.name === (schedule?.flow ?? "workload_investigation")) ?? flows.flows[0];
  const [name, setName] = useState(schedule?.name ?? "");
  const [expr, setExpr] = useState(schedule?.cron ?? "0 * * * *");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  async function save(inputs: Record<string, any>) {
    setBusy(true); setError(undefined);
    try {
      if (schedule) await api(`/crewai/schedules/${schedule.id}`, { method: "PATCH", body: { name, cron: expr, inputs } });
      else await api("/crewai/schedules", { method: "POST", body: { flow: flow.name, name, cron: expr, inputs } });
      onSaved(); onClose();
    } catch (e) { setError(e); setBusy(false); }
  }
  return <Dialog title={schedule ? `Edit ${schedule.name}` : "New schedule"} onClose={onClose} wide>
    <div className="stack">
      <div className="small muted">A schedule runs {flow.label.toLowerCase()} as the person who saves it, with that person's access checked at every start. A changed time counts from now.</div>
      <label>Name<input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required /></label>
      <label>When (cron, UTC)<input className="mono" value={expr} onChange={(e) => setExpr(e.target.value)} maxLength={120} /></label>
      <CronPreview expr={expr} />
      <InputsForm spec={flow.inputs} environments={flows.environments} values={schedule?.inputs} submitLabel={schedule ? "Save schedule" : "Create schedule"} busy={busy || !name.trim()} onSubmit={save} />
      <ErrorBox error={error} />
    </div>
  </Dialog>;
}

function Schedules() {
  const load = useLoad(() => Promise.all([api("/crewai/schedules"), api("/crewai/flows")]), []);
  const nav = useNavigate();
  const { confirm, toast } = useFeedback();
  const [editing, setEditing] = useState<any | null | undefined>(undefined);
  const [error, setError] = useState<unknown>();
  if (!load.data) return <Loading load={load} />;
  const [d, flows] = load.data;
  const admin = d.can_admin;
  async function act(fn: () => Promise<any>, done?: string) { setError(undefined); try { await fn(); if (done) toast(done); load.reload(); } catch (e) { setError(e); } }
  return <div className="stack">
    <div className="row between"><div className="small muted">Schedules live in MOKS's database; the worker starts them (UTC, at most every {d.min_interval_minutes} minutes). A missed time is skipped, not caught up.</div>
      {admin && <button type="button" onClick={() => setEditing(null)}>New schedule</button>}</div>
    <ErrorBox error={error} />
    <DataTable label="Schedules" rows={d.schedules} rowKey={(s: any) => s.id} empty={<Empty title="No schedules yet." />} menu={admin ? (s: any) => ({ label: `Actions for ${s.name}`, items: [
      { label: "Edit", run: () => setEditing(s) },
      { label: s.enabled ? "Pause" : "Resume", run: () => act(() => api(`/crewai/schedules/${s.id}`, { method: "PATCH", body: { enabled: !s.enabled } }), s.enabled ? "Paused" : "Resumed") },
      { label: "Run now", run: () => act(async () => { const r = await api(`/crewai/schedules/${s.id}/run`, { method: "POST", body: {} }); nav(`/crewai/runs/${r.id}`); }) },
      { label: "Delete", danger: true, run: async () => { if (await confirm({ title: `Delete ${s.name}?`, body: "Its runs stay in the history.", action: "Delete", danger: true })) act(() => api(`/crewai/schedules/${s.id}`, { method: "DELETE" }), "Deleted"); } },
    ] }) : undefined} cols={[
      { key: "n", head: "Schedule", primary: true, cell: (s: any) => <span><strong>{s.name}</strong><span className="cw-sub">{s.flow_label}</span></span> },
      { key: "w", head: "When", cell: (s: any) => <span><code>{s.cron}</code><span className="cw-sub">{s.enabled && s.next_run_at ? <>next <When iso={s.next_run_at} /></> : "paused"}</span></span> },
      { key: "t", head: "Target", cell: (s: any) => <span className="small">{s.inputs.kind ?? "Deployment"} {s.inputs.namespace}/{s.inputs.workload}<span className="cw-sub">{s.inputs.environment}</span></span> },
      { key: "s", head: "State", status: true, cell: (s: any) => s.enabled ? <Badge kind="ok">active</Badge> : <Badge kind="neutral">paused</Badge> },
      { key: "l", head: "Last start", cell: (s: any) => <span className="small">{s.last_run ? <><StatusIcon status={s.last_run.status ?? "queued"} /> <Link to={`/crewai/runs/${s.last_run.id}`}>#{s.last_run.number}</Link> </> : null}
        <Time iso={s.last_run_at} />{s.last_error && <span className="cw-sub danger-text">{s.last_error}</span>}</span> },
      { key: "o", head: "Runs as", wide: true, cell: (s: any) => <span className="small">{s.owner}<span className="cw-sub">v{s.version}</span></span> },
    ]} />
    {editing !== undefined && <ScheduleDialog schedule={editing ?? undefined} flows={flows} onClose={() => setEditing(undefined)} onSaved={() => { toast("Saved"); load.reload(); }} />}
  </div>;
}

// ---- crews ------------------------------------------------------------------------------------

function Crews() {
  const load = useLoad(() => api("/crewai/crews"), []);
  if (!load.data) return <Loading load={load} />;
  return <div className="stack">{load.data.crews.map((c: any) => <Section key={c.crew} title={<Link to={`/crewai/crews/${c.crew}`}>{c.label}</Link>}
    actions={<Link className="btn ghost small" to={`/crewai/crews/${c.crew}`}>{load.data.can_admin ? "Edit" : "View"}</Link>}>
    <div className="small">{c.agents.join(" → ")}</div>
    <div className="small muted">{c.active_version ? `Version ${c.active_version} active` : "The code's defaults are active"} · {c.versions} saved version(s){c.updated_at ? <> · last saved <Time iso={c.updated_at} /></> : null}
      {c.upstream_changed && <> · <Badge kind="warn">upstream changed</Badge></>}</div>
  </Section>)}</div>;
}

function CrewPage() {
  const { crew = "" } = useParams();
  const load = useLoad(() => api(`/crewai/crews/${crew}`), [crew]);
  if (!load.data) return <Loading load={load} rows={10} />;
  return <CrewEditor key={`${load.data.active?.version ?? "d"}-${load.data.versions.length}`} crew={load.data} reload={load.reload} />;
}

// ---- settings, import and export -----------------------------------------------------------------

function Transfer() {
  const [text, setText] = useState("");
  const [plan, setPlan] = useState<any>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const { confirm, toast } = useFeedback();
  async function exportIt() {
    setError(undefined);
    try {
      const data = await api("/crewai/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const a = Object.assign(document.createElement("a"), { href: url, download: `moks-crewai-${new Date().toISOString().slice(0, 10)}.json` });
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(e); }
  }
  async function run(apply: boolean) {
    setBusy(true); setError(undefined);
    try {
      const data = JSON.parse(text);
      if (apply && !(await confirm({ title: "Import", body: "Adds and changes what the plan lists. Nothing is deleted; imported schedules run as you.", action: "Import" }))) return;
      const r = await api("/crewai/import", { method: "POST", body: { data, apply } });
      setPlan(r);
      if (r.applied) toast("Imported");
    } catch (e) { setError(e instanceof SyntaxError ? new Error(`Not JSON: ${e.message}`) : e); } finally { setBusy(false); }
  }
  return <Section title="Import / export">
    <div className="small muted">One JSON file: each crew's active version, every schedule, which flows are off, and these settings. No runs, logs or transcripts.</div>
    <div className="row"><button type="button" className="ghost" onClick={exportIt}>Export</button></div>
    <label>Import<textarea className="mono" rows={6} value={text} onChange={(e) => { setText(e.target.value); setPlan(undefined); }} placeholder="Paste an exported file" /></label>
    <input type="file" accept="application/json,.json" aria-label="Import file" onChange={async (e) => { const f = e.target.files?.[0]; if (f) { setText(await f.text()); setPlan(undefined); } }} />
    <div className="row"><button type="button" className="ghost" disabled={busy || !text.trim()} onClick={() => run(false)}>Plan</button>
      <button type="button" disabled={busy || !plan || plan.errors.length > 0 || plan.applied} onClick={() => run(true)}>Apply plan</button></div>
    <ErrorBox error={error} />
    {plan && <div className="stack">
      {plan.errors.length > 0 && <div className="alert error small"><ul>{plan.errors.map((e: string, i: number) => <li key={i}>{e}</li>)}</ul></div>}
      <DataTable label="Import plan" rows={plan.plan} rowKey={(p: any) => `${p.kind}:${p.name}`} cols={[
        { key: "k", head: "What", primary: true, cell: (p: any) => <span>{p.kind} <strong>{p.name}</strong></span> },
        { key: "a", head: "Change", status: true, cell: (p: any) => <Badge kind={p.action === "same" ? "neutral" : p.action === "add" ? "ok" : "info"}>{p.action}</Badge> },
        { key: "d", head: "Detail", cell: (p: any) => <span className="small">{p.detail}</span> },
      ]} />
      {plan.applied && <div className="alert info small">Applied.</div>}
    </div>}
  </Section>;
}

function Settings() {
  const load = useLoad(() => api("/crewai/settings"), []);
  const [v, setV] = useState<any>();
  const [error, setError] = useState<unknown>();
  const { toast } = useFeedback();
  useEffect(() => { if (load.data) setV(load.data.history); }, [load.data]);
  if (!load.data || !v) return <Loading load={load} />;
  const d = load.data;
  async function save(e: FormEvent) {
    e.preventDefault(); setError(undefined);
    try { await api("/crewai/settings", { method: "PATCH", body: v }); toast("Saved"); load.reload(); } catch (x) { setError(x); }
  }
  return <div className="grid cols-2">
    <div className="stack">
      <Section title="Run history">
        <form className="stack" onSubmit={save}>
          <label className="cw-check"><input type="checkbox" checked={!!v.transcripts} disabled={!d.can_admin} onChange={(e) => setV({ ...v, transcripts: e.target.checked })} /> Keep model-call transcripts (system prompt, prompt, answer)</label>
          <div className="cw-inputs">
            <label>Keep log lines (days)<input type="number" min={1} max={365} value={v.log_days} readOnly={!d.can_admin} onChange={(e) => setV({ ...v, log_days: Number(e.target.value) })} /></label>
            <label>Keep transcripts (days)<input type="number" min={1} max={90} value={v.transcript_days} readOnly={!d.can_admin} onChange={(e) => setV({ ...v, transcript_days: Number(e.target.value) })} /></label>
          </div>
          <div className="small muted">Runs, steps and cost stay; the worker deletes older log lines and transcripts once an hour. Transcripts hold the redacted evidence the model saw.</div>
          {d.can_admin && <div className="row end"><button>Save</button></div>}
          <ErrorBox error={error} />
        </form>
      </Section>
      <Section title="This deployment">
        <dl className="kv">
          <div className="kv-row"><dt>CrewAI</dt><dd>{d.deployment.enabled ? "on" : "off (MOKS_CREWAI_ENABLED)"}{d.unavailable ? ` · not available: ${d.unavailable}` : ""}</dd></div>
          <div className="kv-row"><dt>Crews per worker</dt><dd>{d.deployment.max_concurrent_per_worker}</dd></div>
          <div className="kv-row"><dt>Tool timeout</dt><dd>{d.deployment.tool_timeout_seconds}s</dd></div>
          <div className="kv-row"><dt>Ceilings</dt><dd>{d.deployment.max_tool_calls} tool calls, {d.deployment.max_duration_seconds}s per investigation</dd></div>
        </dl>
        <div className="small muted">Set in the Helm values (MOKS_CREWAI_*); a crew version can lower these limits, never raise them.</div>
      </Section>
    </div>
    {d.can_admin ? <Transfer /> : <Section title="Import / export"><div className="small muted">Needs crewai.admin.</div></Section>}
  </div>;
}

export function CrewAIConsole() {
  return <div className="stack cw">
    <PageHead title="CrewAI console" sub="CrewAI runs, their logs, flows, schedules and crew versions." />
    <ConsoleTabs />
    <Routes>
      <Route index element={<Overview />} />
      <Route path="runs" element={<Runs />} />
      <Route path="runs/:id" element={<RunPage />} />
      <Route path="flows" element={<Flows />} />
      <Route path="flows/:name" element={<FlowPage />} />
      <Route path="schedules" element={<Schedules />} />
      <Route path="crews" element={<Crews />} />
      <Route path="crews/:crew" element={<CrewPage />} />
      <Route path="settings" element={<Settings />} />
      <Route path="*" element={<Empty title="Not found"><Link to="/crewai">Back to the console</Link></Empty>} />
    </Routes>
  </div>;
}
