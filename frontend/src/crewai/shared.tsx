import { useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../api";
import { ErrorBox, Time } from "../components";
import { KindOptions, NamespaceSelect, WorkloadSelect } from "../targets";

// The CrewAI console (docs/crewai-console.md). Shared parts: run status, durations, the console's tabs, and the
// Run with parameters form built from a flow's own input list.

export interface RunRow {
  id: string; number: number; flow: string; flow_label: string; trigger: string; status: string; crew_status: string | null;
  environment: string; namespace: string; workload: string | null; kind: string | null; question: string; actor: string | null;
  created_at: string | null; started_at: string | null; finished_at: string | null; seconds: number | null; cost_usd: string;
  tokens_in: number; tokens_out: number; tool_calls: number; error: string | null; summary: string | null; config_version: number | null;
  schedule_id: string | null; schedule: string | null; rerun_of: string | null; conversation_id: string | null;
}
export interface Step { step: string; label: string; status: string; started_at: string | null; finished_at: string | null; seconds: number | null; error: string | null;
  model_calls: number; tool_calls: number; tokens_in: number; tokens_out: number; cost_usd: string }
export interface InputSpec { name: string; label: string; type: "environment" | "namespace" | "workload" | "string" | "choice" | "integer" | "text"; required?: boolean; pattern?: string;
  max?: number; min?: number; choices?: string[]; default?: string; help?: string }

/** One status for a run, as GitHub Actions shows it: a finished run whose crew stopped early is a warning, not a pass. */
export function runState(r: { status: string; crew_status?: string | null }): { tone: "ok" | "warn" | "bad" | "info" | "neutral"; text: string } {
  if (r.status === "completed") return r.crew_status === "partial" ? { tone: "warn", text: "completed, partial" } : { tone: "ok", text: "succeeded" };
  if (r.status === "failed") return { tone: "bad", text: "failed" };
  if (r.status === "cancelled") return { tone: "neutral", text: "cancelled" };
  if (r.status === "running") return { tone: "info", text: "in progress" };
  return { tone: "info", text: "queued" };
}

const MARKS: Record<string, string> = { ok: "✓", warn: "!", bad: "✕", info: "●", neutral: "–" };

export function StatusIcon({ status, crew_status, label }: { status: string; crew_status?: string | null; label?: boolean }) {
  const s = runState({ status, crew_status });
  return <span className={`cw-status ${s.tone}${status === "running" ? " live" : ""}`} title={s.text}>
    <span className="cw-mark" aria-hidden="true">{MARKS[s.tone]}</span>{label ? <span>{s.text}</span> : <span className="sr-only">{s.text}</span>}
  </span>;
}

export function StepIcon({ status }: { status: string }) {
  const tone = { succeeded: "ok", failed: "bad", cancelled: "neutral", skipped: "neutral", running: "info", pending: "neutral" }[status] ?? "neutral";
  const mark = { succeeded: "✓", failed: "✕", cancelled: "–", skipped: "↷", running: "●", pending: "○" }[status] ?? "○";
  return <span className={`cw-status ${tone}${status === "running" ? " live" : ""}`} title={status}><span className="cw-mark" aria-hidden="true">{mark}</span><span className="sr-only">{status}</span></span>;
}

/** The last runs of a flow as squares, newest on the right (Jenkins' build history strip). */
export function Squares({ runs }: { runs: { id: string; number: number; status: string; crew_status: string | null }[] }) {
  if (!runs.length) return <span className="muted small">no runs yet</span>;
  return <span className="cw-squares" aria-label="Last runs">{[...runs].reverse().map((r) => {
    const s = runState(r);
    return <Link key={r.id} to={`/crewai/runs/${r.id}`} className={`cw-sq ${s.tone}`} title={`#${r.number} ${s.text}`}><span className="sr-only">#{r.number} {s.text}</span></Link>;
  })}</span>;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** A time that may be to come (a schedule's next start): "in 3h", or "2m ago" once it has passed. */
export function When({ iso }: { iso?: string | null }) {
  if (!iso) return <span className="muted">—</span>;
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (s <= 0) return <Time iso={iso} />;
  const text = s < 60 ? `in ${s}s` : s < 3600 ? `in ${Math.round(s / 60)}m` : s < 86400 ? `in ${Math.round(s / 3600)}h` : `in ${Math.round(s / 86400)}d`;
  return <time dateTime={iso} title={new Date(iso).toLocaleString(undefined, { timeZoneName: "short" })}>{text}</time>;
}

export function money(v: string | number | null | undefined): string {
  const n = Number(v ?? 0);
  if (!n) return "$0";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;
}

export const TRIGGERS: Record<string, string> = { user: "manual", rerun: "re-run", schedule: "schedule", test: "test run" };

export function ConsoleTabs() {
  const tabs = [["/crewai", "Overview", true], ["/crewai/runs", "Runs"], ["/crewai/flows", "Flows"], ["/crewai/schedules", "Schedules"], ["/crewai/crews", "Crews"],
    ["/crewai/settings", "Settings"]] as const;
  return <nav className="tabs cw-tabs" aria-label="CrewAI console">
    {tabs.map(([to, label, end]) => <NavLink key={to} to={to} end={!!end} className={({ isActive }) => isActive ? "active" : ""}>{label}</NavLink>)}
  </nav>;
}

export function Section({ title, actions, children }: { title: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return <section className="card stack"><div className="row between"><h2 className="cw-h2">{title}</h2>{actions && <div className="row">{actions}</div>}</div>{children}</section>;
}

/** What changing one input empties: a namespace belongs to its environment, and a workload to its namespace and kind. */
function clears(spec: InputSpec[], name: string): Record<string, string> {
  const type = spec.find((f) => f.name === name)?.type;
  const of = (t: string) => spec.filter((f) => f.type === t).map((f) => f.name);
  const names = type === "environment" ? [...of("namespace"), ...of("workload")] : type === "namespace" || name === "kind" ? of("workload") : [];
  return Object.fromEntries(names.map((n) => [n, ""]));
}

/** The form for a flow's inputs. `values` seeds it (a re-run, a schedule); `onSubmit` gets only what was filled in. */
export function InputsForm({ spec, environments, values = {}, submitLabel, busy, onSubmit, children }: {
  spec: InputSpec[]; environments: string[]; values?: Record<string, any>; submitLabel: string; busy?: boolean;
  onSubmit: (inputs: Record<string, any>) => void; children?: ReactNode;
}) {
  const [v, setV] = useState<Record<string, any>>(() => {
    const out: Record<string, any> = {};
    for (const f of spec) out[f.name] = values[f.name] ?? (f.type === "environment" ? environments[0] ?? "" : f.default ?? "");
    return out;
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    const out: Record<string, any> = {};
    for (const f of spec) {
      const x = v[f.name];
      if (x === "" || x === undefined || x === null) continue;
      out[f.name] = f.type === "integer" ? Number(x) : x;
    }
    onSubmit(out);
  }
  return <form className="stack" onSubmit={submit}>
    <div className="cw-inputs">
      {spec.map((f) => {
        const id = `in-${f.name}`;
        const set = (value: string) => setV({ ...v, [f.name]: value, ...clears(spec, f.name) });
        const common = { id, value: v[f.name] ?? "", onChange: (e: any) => set(e.target.value), required: f.required };
        const env = String(v[spec.find((x) => x.type === "environment")?.name ?? "environment"] ?? "");
        const ns = String(v[spec.find((x) => x.type === "namespace")?.name ?? "namespace"] ?? "");
        let ctl: ReactNode;
        if (f.type === "environment") ctl = <select {...common}>{environments.map((n) => <option key={n}>{n}</option>)}</select>;
        else if (f.type === "namespace") ctl = <NamespaceSelect id={id} environment={env} value={v[f.name] ?? ""} onChange={set} />;
        else if (f.type === "workload") ctl = <WorkloadSelect id={id} environment={env} namespace={ns} kind={String(v.kind ?? "Deployment")} value={v[f.name] ?? ""} onChange={set} />;
        else if (f.type === "choice" && f.name === "kind") ctl = <select {...common}><KindOptions environment={env} namespace={ns} kinds={f.choices} /></select>;
        else if (f.type === "choice") ctl = <select {...common}>{(f.choices ?? []).map((c) => <option key={c}>{c}</option>)}</select>;
        else if (f.type === "integer") ctl = <input {...common} type="number" inputMode="numeric" min={f.min} max={f.max} placeholder="default" />;
        else if (f.type === "text") ctl = <textarea {...common} rows={2} maxLength={f.max} placeholder={f.help} />;
        else ctl = <input {...common} maxLength={f.max} pattern={f.pattern?.replace(/^\^|\$$/g, "")} autoComplete="off" spellCheck={false} />;
        return <label key={f.name} htmlFor={id} className={f.type === "text" ? "cw-wide" : undefined}>{f.label}{f.required ? " *" : ""}{ctl}</label>;
      })}
    </div>
    {children}
    <div className="row end"><button disabled={busy}>{busy ? "Starting…" : submitLabel}</button></div>
  </form>;
}

/** Run with parameters, as a button that opens the flow's form. Starting a run goes to its page. */
export function RunFlow({ flow, label = "Run flow", values, disabled, why }: { flow: { name: string; label: string; inputs: InputSpec[]; defaults?: Record<string, any> };
  label?: string; values?: Record<string, any>; disabled?: boolean; why?: string | null }) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [envs, setEnvs] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  async function show() {
    setOpen(!open);
    if (!envs) setEnvs(await api<string[]>("/crewai/environments").catch(() => []));
  }
  async function run(inputs: Record<string, any>) {
    setBusy(true); setError(undefined);
    try {
      const r = await api<{ id: string }>(`/crewai/flows/${flow.name}/run`, { method: "POST", body: { inputs } });
      nav(`/crewai/runs/${r.id}`);
    } catch (e) { setError(e); setBusy(false); }
  }
  return <div className="stack">
    <div className="row"><button type="button" onClick={show} disabled={disabled} title={disabled ? why ?? undefined : undefined} aria-expanded={open}>{label}</button>
      {disabled && why && <span className="small muted">{why}</span>}</div>
    {open && envs && <div className="card cw-runform">
      <div className="small muted">Runs {flow.label.toLowerCase()} with the crew's active configuration. Empty fields use the defaults{flow.defaults && Object.keys(flow.defaults).length ? ` (${Object.entries(flow.defaults).map(([k, x]) => `${k} ${x}`).join(", ")})` : ""}.</div>
      {envs.length ? <InputsForm spec={flow.inputs} environments={envs} values={{ ...(flow.defaults ?? {}), ...(values ?? {}) }} submitLabel="Run" busy={busy} onSubmit={run} />
        : <div className="alert warn small">You cannot run investigations in any environment.</div>}
      <ErrorBox error={error} />
    </div>}
  </div>;
}
