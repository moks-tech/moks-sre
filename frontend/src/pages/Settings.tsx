import { Suspense, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { Badge, ErrorBox, FormDrawer, FormSection, Loading, PageHead, Time, useLoad } from "../components";
import { settingsFor } from "../nav";
import { Empty, Icon, Skeleton, useFeedback } from "../ui";
import { Settings as CrewAISettings } from "./CrewAIConsole";
import { Capability, Train } from "./ML";

/** Settings: one page with its own submenu, so every administrative screen is in one place. Each page still
 *  checks its own permission, and the backend enforces it again. */
export function SettingsLayout() {
  const { can } = useAuth();
  const groups = useMemo(() => settingsFor(can), [can]);
  return (
    <div className="settings">
      <nav className="settings-nav" aria-label="Settings">
        {groups.map((g) => (
          <div key={g.label} className="settings-group" role="group" aria-label={g.label}>
            <div className="settings-group-label" aria-hidden="true">{g.label}</div>
            {g.items.map((i) => (
              <NavLink key={i.to} to={i.to} end={i.end}><Icon name={i.icon} size={16} /><span>{i.label}</span></NavLink>
            ))}
          </div>
        ))}
      </nav>
      <div className="settings-body">
        <Suspense fallback={<Skeleton rows={6} />}><Outlet /></Suspense>
      </div>
    </div>
  );
}

const DESCRIBE: Record<string, string> = {
  "/settings/features": "Switch ML capabilities such as anomaly detection between off, shadow and on.",
  "/settings/runtime": "LLM budgets, automatic investigation caps, CrewAI limits and ML schedules, changed without a deploy.",
  "/settings/deployment": "Every value this deployment started with. Read-only; secrets show only whether they are set.",
  "/settings/users": "People, their roles and where each role applies.",
  "/settings/environments": "Clusters, allowed namespaces, detection and investigation limits.",
  "/settings/connectors": "Prometheus, Loki, Kubernetes, Git and the other sources MOKS reads.",
  "/settings/backup-storage": "Where database backups are written.",
  "/settings/llm": "Chat and embedding models, their budgets, and which one is active.",
  "/settings/ml": "Models, training, promotion and rollback; confirmed outcomes and datasets.",
  "/settings/crewai": "CrewAI run history, transcripts, import and export.",
  "/settings/evaluation": "Benchmark investigations and retrieval against labelled datasets.",
  "/settings/audit": "Who changed what, in a chain that can be verified.",
};

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-");

const MODE_TONE: Record<string, "ok" | "info" | "neutral"> = { enabled: "ok", shadow: "info", disabled: "neutral" };

export function SettingsOverview() {
  const { can } = useAuth();
  const groups = settingsFor(can);
  const ml = useLoad(() => (can("ml.read") || can("ml.admin") ? api<any>("/ml/status") : Promise.resolve(null)), []);
  const runtime = useLoad(() => (can("admin.settings") ? api<any>("/admin/settings") : Promise.resolve(null)), []);
  const anomaly = ml.data?.capabilities?.anomaly_detection;
  const changed = runtime.data?.runtime?.filter((r: any) => r.overridden).length;
  return <div className="stack">
    <PageHead title="Settings" sub="Everything an administrator manages in MOKS. Every change is audited." />
    {(anomaly || changed !== undefined) && <div className="settings-status" aria-label="At a glance">
      {anomaly && <Link to={can("ml.admin") ? "/settings/features" : "/ml"} className="card tight settings-glance">
        <span className="small muted">Anomaly detection</span>
        <span className="row tight"><Badge kind={MODE_TONE[anomaly.mode] ?? "neutral"}>{anomaly.mode}</Badge>{anomaly.forced_off && <Badge kind="warn">forced off</Badge>}</span>
      </Link>}
      {changed !== undefined && <Link to="/settings/runtime" className="card tight settings-glance">
        <span className="small muted">Limits & schedules</span>
        <span>{changed === 0 ? "All from Helm" : `${changed} changed here`}</span>
      </Link>}
    </div>}
    {groups.map((g) => {
      const items = g.items.filter((i) => i.to !== "/settings");
      if (!items.length) return null;
      return <section key={g.label} className="stack tight" aria-labelledby={`settings-${slug(g.label)}`}>
        <h2 id={`settings-${slug(g.label)}`}>{g.label}</h2>
        <div className="grid cols-3">
          {items.map((i) => <Link key={i.to} to={i.to} className="card tight settings-card">
            <span className="row tight"><Icon name={i.icon} size={16} /><strong>{i.label}</strong></span>
            <span className="small muted">{DESCRIBE[i.to]}</span>
          </Link>)}
        </div>
      </section>;
    })}
  </div>;
}

/** Optional capabilities and their modes. Anomaly detection also says what it needs before shadow mode shows anything. */
export function Features() {
  const { can } = useAuth();
  const status = useLoad(() => api<any>("/ml/status"), []);
  const caps: Record<string, any> | undefined = status.data?.capabilities;
  const order = (name: string) => (name === "anomaly_detection" ? 0 : 1);  // the one people come here for
  return <div className="stack">
    <PageHead title="Features" sub="Switch optional capabilities. Each change asks for a reason and is audited; switched off, MOKS does what it did without the capability." />
    {status.error ? <ErrorBox error={status.error} onRetry={status.reload} /> : caps ? <div className="grid cols-2">
      {Object.entries(caps).sort(([a], [b]) => order(a) - order(b)).map(([name, c]) => <Capability key={name} name={name} c={c} onSaved={status.reload}>
        {name === "anomaly_detection" && <AnomalyReadiness c={c} models={status.data.models ?? []} canTune={can("admin.settings")} />}
      </Capability>)}
    </div> : <Loading load={status} />}
    <div className="small muted">Machine learning in MOKS is advisory: it never grants access, opens incidents, changes severity or runs actions.</div>
  </div>;
}

function AnomalyReadiness({ c, models, canTune }: { c: any; models: { capability: string; status: string; count: number }[]; canTune: boolean }) {
  const [training, setTraining] = useState(false);
  const count = (s: string) => models.filter((m) => m.capability === "anomaly_detection" && m.status === s).reduce((n, m) => n + m.count, 0);
  const champions = count("champion"), challengers = count("shadow"), candidates = count("candidate");
  return <div className="stack tight settings-readiness">
    <div className="small"><strong>{champions}</strong> champion · <strong>{challengers}</strong> challenger · <strong>{candidates}</strong> candidate model{candidates === 1 ? "" : "s"}</div>
    {champions + challengers === 0 && <div className="alert info small">
      {c.mode === "disabled" ? "Shadow mode" : "Shadow mode is on, but it"} scores only workloads with an active model. Train models for a namespace, then make one champion under <Link to="/settings/ml">Machine learning</Link>.
    </div>}
    <div className="row tight">
      <button type="button" className="ghost small" onClick={() => setTraining(true)}>Train models</button>
      {canTune && <Link className="small" to="/settings/runtime">Scoring interval and retention</Link>}
    </div>
    {training && <Train onClose={() => setTraining(false)} />}
  </div>;
}

type Runtime = { key: string; group: string; group_label: string; label: string; help: string; kind: "int" | "float" | "bool"; unit: string; optional: boolean; limit: boolean;
  min: number | null; max: number | null; value: any; helm_value: any; overridden: boolean; ignored: string | null; env: string; helm: string; reason: string | null;
  updated_at: string | null; updated_by: string | null };

export function showValue(r: Pick<Runtime, "kind" | "unit">, v: any): string {
  if (r.kind === "bool") return v ? "On" : "Off";
  if (v === null || v === undefined || v === "") return "No limit";
  if (r.unit === "USD") return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  return r.unit ? `${v} ${r.unit}` : String(v);
}

/** The curated values an administrator may change while MOKS runs. */
export function RuntimeSettings() {
  const load = useLoad(() => api<any>("/admin/settings"), []);
  const [edit, setEdit] = useState<Runtime | null>(null);
  const rows: Runtime[] = load.data?.runtime ?? [];
  const groups = [...new Set(rows.map((r) => r.group))];
  return <div className="stack">
    <PageHead title="Limits & schedules" sub={<>Change these without a deploy. A limit can be tightened here but not loosened: its Helm value is the most it can be.
      Other MOKS processes apply a change within {load.data?.apply_seconds ?? 30} seconds.</>} />
    {load.data ? groups.map((g) => {
      const items = rows.filter((r) => r.group === g);
      return <section key={g} className="card stack" aria-labelledby={`rt-${g}`}>
        <h2 id={`rt-${g}`}>{items[0].group_label}</h2>
        <ul className="setting-list">
          {items.map((r) => <li key={r.key} className="setting-row">
            <div className="setting-main">
              <div className="row tight"><strong>{r.label}</strong>
                {r.overridden ? <Badge kind="info">changed here</Badge> : <Badge>Helm</Badge>}
                {r.limit && <Badge>limit</Badge>}
                {r.ignored && <Badge kind="warn">ignored</Badge>}
              </div>
              <div className="small muted">{r.help}</div>
              {r.overridden && <div className="small muted">Helm value {showValue(r, r.helm_value)} · changed by {r.updated_by ?? "unknown"} <Time iso={r.updated_at} />{r.reason && <> · “{r.reason}”</>}</div>}
              {r.ignored && <div className="small warn-text">Saved value not used: {r.ignored}</div>}
            </div>
            <div className="setting-value">
              <span className="mono">{showValue(r, r.value)}</span>
              <button type="button" className="ghost small" onClick={() => setEdit(r)} aria-label={`Change ${r.label}`}>Change</button>
            </div>
          </li>)}
        </ul>
      </section>;
    }) : <Loading load={load} />}
    {load.data && <div className="small muted">Everything else this deployment uses is on <Link to="/settings/deployment">Deployment</Link>, read-only: those values change in the Helm values.</div>}
    {edit && <EditSetting s={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); load.reload(); }} />}
  </div>;
}

function EditSetting({ s, onClose, onSaved }: { s: Runtime; onClose: () => void; onSaved: () => void }) {
  const { toast } = useFeedback();
  const [text, setText] = useState(s.kind === "bool" ? "" : s.value === null || s.value === undefined ? "" : String(s.value));
  const [on, setOn] = useState<boolean>(!!s.value);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const value = s.kind === "bool" ? on : text.trim() === "" ? null : Number(text);
  const same = s.kind === "bool" ? on === !!s.value : value === s.value;
  const run = async (path: string, body: any, done: string) => {
    setBusy(true);
    setError(null);
    try {
      await api(path, { method: path.endsWith("/reset") ? "POST" : "PUT", body });
      toast(done);
      onSaved();
    } catch (err) { setError(err); setBusy(false); }
  };
  const save = (e: FormEvent) => { e.preventDefault(); run(`/admin/settings/${s.key}`, { value, reason }, `${s.label}: ${showValue(s, value)}.`); };
  const low = s.unit === "USD" ? showValue(s, s.min) : String(s.min);  // "1 to 6 per hour", "$0.01 to $10,000"
  const range = s.min !== null && s.max !== null ? `${low} to ${showValue(s, s.max)}` : s.min !== null ? `at least ${showValue(s, s.min)}` : "";
  return <FormDrawer title={`Change ${s.label.toLowerCase()}`} submit="Save" busy={busy} blocked={same || reason.trim().length < 3} error={error} onSubmit={save} onClose={onClose}>
    <FormSection title="Value" help={s.help}>
      {s.kind === "bool"
        ? <div className="stack tight" role="radiogroup" aria-label={s.label}>
            {[true, false].map((v) => <label key={String(v)} className="inline"><input type="radio" name="rt-bool" checked={on === v} onChange={() => setOn(v)} /> {v ? "On" : "Off"}{!!s.value === v && <span className="muted"> (current)</span>}</label>)}
          </div>
        : <label>{s.label}{s.unit ? ` (${s.unit})` : ""}
            <input type="number" inputMode={s.kind === "int" ? "numeric" : "decimal"} value={text} onChange={(e) => setText(e.target.value)} autoFocus
              min={s.min ?? undefined} max={s.max ?? undefined} step={s.kind === "int" ? 1 : 0.01} required={!s.optional} placeholder={s.optional ? "No limit" : undefined} />
          </label>}
      {s.kind !== "bool" && <div className="small muted">
        {range && <>Allowed: {range}. </>}{s.limit && s.helm_value !== null && s.max === s.helm_value && "Helm sets the ceiling. "}{s.optional && "Leave empty for no limit. "}
        Helm value: {showValue(s, s.helm_value)} <span className="mono">({s.helm ? `${s.helm}, ` : ""}{s.env})</span>
      </div>}
    </FormSection>
    <FormSection title="Why">
      <label>Reason (audited)<input value={reason} onChange={(e) => setReason(e.target.value)} minLength={3} maxLength={500} required autoFocus={s.kind === "bool"} /></label>
    </FormSection>
    {s.overridden && <FormSection title="Helm value" help={`Removes the value saved here, so ${showValue(s, s.helm_value)} from Helm applies again.`}>
      <div><button type="button" className="ghost small" disabled={busy || reason.trim().length < 3}
        onClick={() => run(`/admin/settings/${s.key}/reset`, { reason }, `${s.label} is back to its Helm value.`)}>Use the Helm value</button></div>
    </FormSection>}
  </FormDrawer>;
}

function deployValue(i: any): ReactNode {
  if (i.secret) return i.set ? <Badge kind="ok">set</Badge> : <Badge>not set</Badge>;
  const v = i.value;
  if (v === null || v === undefined) return <span className="muted">none</span>;
  if (v === "") return <span className="muted">empty</span>;
  if (typeof v === "object") return <span className="mono">{JSON.stringify(v)}</span>;
  return <span className="mono">{String(v)}</span>;
}

/** What this deployment started with, read-only. */
export function Deployment() {
  const load = useLoad(() => api<any>("/admin/settings"), []);
  const [q, setQ] = useState("");
  const needle = q.trim().toLowerCase();
  const sections: any[] = (load.data?.deployment ?? []).map((s: any) => ({
    ...s, items: s.items.filter((i: any) => !needle || i.env.toLowerCase().includes(needle) || (!i.secret && String(typeof i.value === "object" ? JSON.stringify(i.value) : i.value).toLowerCase().includes(needle))),
  })).filter((s: any) => s.items.length);
  return <div className="stack">
    <PageHead title="Deployment" sub="The MOKS_* values this deployment started with, from Helm and the environment. Read-only: change them in the Helm values and roll out. Secrets show only whether they are set." />
    <div className="filters row wrap"><label className="grow">Filter<input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name or value, e.g. crewai" /></label></div>
    {load.data ? (sections.length ? sections.map((s) => <section key={s.section} className="card stack" aria-labelledby={`dep-${slug(s.section)}`}>
      <h2 id={`dep-${slug(s.section)}`}>{s.section}</h2>
      {s.note && <div className="small muted">{s.note}</div>}
      <dl className="kv settings-kv">
        {s.items.map((i: any) => <div key={i.key} className="kv-row">
          <dt className="mono">{i.env}</dt>
          <dd>{deployValue(i)}{i.editable && <> · <Link to="/settings/runtime">changeable in Limits & schedules</Link></>}</dd>
        </div>)}
      </dl>
    </section>) : <Empty title={`Nothing matches “${q.trim()}”.`} />) : <Loading load={load} />}
  </div>;
}

export function CrewAISettingsPage() {
  return <div className="stack">
    <PageHead title="CrewAI" sub={<>Run history, transcripts, import and export. Runs, flows, schedules and crews are in the <Link to="/crewai">CrewAI console</Link>.</>} />
    <CrewAISettings />
  </div>;
}

export function SettingsNotFound() {
  return <Empty title="No such settings page"><Link to="/settings">Back to Settings</Link></Empty>;
}
