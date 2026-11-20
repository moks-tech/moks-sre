import { useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox, Time } from "../components";
import { useFeedback } from "../ui";
import { Section, StatusIcon, TRIGGERS, type RunRow } from "./shared";
import { WORKLOAD_KINDS } from "../targets";

// The crew editor (docs/crewai-console.md "Crews"): what a version may change, each field against the code's default
// with a word diff, Check, Save as a new version or Save and activate, Test run on a finished run (Jenkins' Replay),
// and the versions with Activate and "Use the code's defaults". What stays in code is shown, read-only, beside it.

type Config = { agents: Record<string, Record<string, any>>; tasks: Record<string, Record<string, string>>; limits: Record<string, number>; defaults: Record<string, any> };
interface Issue { field: string; message: string }

const AGENTS = ["workload", "change", "review"] as const;
const AGENT_LABEL: Record<string, string> = { workload: "Workload agent", change: "Change agent", review: "Reviewer" };
const LIMIT_LABEL: Record<string, string> = { timeout_seconds: "Time limit (s)", max_tool_calls: "Tool calls", max_iterations: "Iterations per agent", step_tokens: "Output tokens per step",
  report_tokens: "Output tokens for the report" };
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Word-level diff (longest common subsequence), for the edited fields. */
export function wordDiff(a: string, b: string): { kind: "same" | "del" | "add"; text: string }[] {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  if (x.length * y.length > 4_000_000) return [{ kind: "del", text: a }, { kind: "add", text: b }];
  const dp = Array.from({ length: x.length + 1 }, () => new Uint32Array(y.length + 1));
  for (let i = x.length - 1; i >= 0; i--) for (let j = y.length - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: { kind: "same" | "del" | "add"; text: string }[] = [];
  const push = (kind: "same" | "del" | "add", text: string) => { const l = out[out.length - 1]; if (l && l.kind === kind) l.text += text; else out.push({ kind, text }); };
  let i = 0, j = 0;
  while (i < x.length && j < y.length) {
    if (x[i] === y[j]) { push("same", x[i]); i++; j++; } else if (dp[i + 1][j] >= dp[i][j + 1]) { push("del", x[i]); i++; } else { push("add", y[j]); j++; }
  }
  while (i < x.length) push("del", x[i++]);
  while (j < y.length) push("add", y[j++]);
  return out;
}

export function Diff({ before, after }: { before: string; after: string }) {
  return <div className="cw-diff mono" aria-label="Changes against the code's default">{wordDiff(before, after).map((p, i) =>
    p.kind === "same" ? <span key={i}>{p.text}</span> : p.kind === "del" ? <del key={i}>{p.text}</del> : <ins key={i}>{p.text}</ins>)}</div>;
}

function Field({ label, path, value, base, issues, onChange, multiline, max, readOnly, hint }: {
  label: string; path: string; value: string; base: string; issues: Issue[]; onChange: (v: string) => void; multiline?: boolean; max?: number; readOnly?: boolean; hint?: ReactNode;
}) {
  const [diff, setDiff] = useState(false);
  const edited = value !== base;
  const mine = issues.filter((i) => i.field === path);
  return <div className="cw-field">
    <div className="row between">
      <label htmlFor={path} className="cw-flabel">{label} {edited && <Badge kind="info">edited</Badge>}</label>
      {edited && <span className="row tight">
        <button type="button" className="ghost small" aria-pressed={diff} onClick={() => setDiff(!diff)}>{diff ? "Hide diff" : "Diff"}</button>
        {!readOnly && <button type="button" className="ghost small" onClick={() => onChange(base)}>Reset</button>}
      </span>}
    </div>
    {multiline ? <textarea id={path} value={value} readOnly={readOnly} maxLength={max} rows={Math.min(10, Math.max(3, Math.ceil(value.length / 90)))} onChange={(e) => onChange(e.target.value)} />
      : <input id={path} value={value} readOnly={readOnly} maxLength={max} onChange={(e) => onChange(e.target.value)} />}
    <div className="row between small muted">{hint ?? <span />}{max ? <span>{value.length}/{max}</span> : null}</div>
    {diff && edited && <Diff before={base} after={value} />}
    {mine.map((m, i) => <div key={i} className={`small ${m.message.includes("not a placeholder") || m.message.includes("without") || m.message.includes("no task") || m.message.includes("has no tools") ? "warn-text" : "danger-text"}`}>{m.message}</div>)}
  </div>;
}

export function CrewEditor({ crew, reload }: { crew: any; reload: () => void }) {
  const nav = useNavigate();
  const { confirm, toast } = useFeedback();
  const defaults: Config = crew.defaults;
  const [draft, setDraft] = useState<Config>(() => clone(crew.effective));
  const [loaded, setLoaded] = useState<string>(crew.active ? `version ${crew.active.version}` : "the code's defaults");
  const [errors, setErrors] = useState<Issue[]>([]);
  const [warnings, setWarnings] = useState<Issue[]>([]);
  const [checked, setChecked] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [testRun, setTestRun] = useState<string>(crew.runs_to_test?.[0]?.id ?? "");
  const canEdit = !!crew.can_admin;
  const issues = [...errors, ...warnings];
  const dirty = !same(draft, crew.effective);
  const edits = useMemo(() => {
    let n = 0;
    for (const s of ["agents", "tasks"] as const) for (const k of Object.keys(defaults[s])) for (const f of Object.keys(defaults[s][k])) if (!same(draft[s][k]?.[f], defaults[s][k][f])) n++;
    for (const s of ["limits", "defaults"] as const) for (const k of Object.keys(defaults[s])) if (!same(draft[s][k], defaults[s][k])) n++;
    return n;
  }, [draft, defaults]);

  const set = (section: "agents" | "tasks", key: string, field: string, value: any) => { setChecked(null); setDraft((d) => ({ ...d, [section]: { ...d[section], [key]: { ...d[section][key], [field]: value } } })); };
  const setTop = (section: "limits" | "defaults", key: string, value: any) => { setChecked(null); setDraft((d) => ({ ...d, [section]: { ...d[section], [key]: value } })); };

  async function check(): Promise<boolean> {
    setError(undefined);
    const r = await api<{ errors: Issue[]; warnings: Issue[] }>(`/crewai/crews/${crew.crew}/check`, { method: "POST", body: { config: draft } });
    setErrors(r.errors); setWarnings(r.warnings); setChecked(JSON.stringify(draft));
    return r.errors.length === 0;
  }
  async function act(fn: () => Promise<void>) {
    setBusy(true); setError(undefined);
    try { await fn(); } catch (e) { setError(e); } finally { setBusy(false); }
  }
  const save = (activate: boolean) => act(async () => {
    if (!(await check())) return;
    if (activate && !(await confirm({ title: "Save and activate", body: "New runs will use this version straight away. Runs already queued keep the version they recorded.", action: "Save and activate" }))) return;
    const v = await api(`/crewai/crews/${crew.crew}/versions`, { method: "POST", body: { config: draft, note, activate } });
    toast(`Saved version ${v.version}${activate ? " and made it active" : ""}`);
    setNote(""); setLoaded(`version ${v.version}`); reload();
  });
  const test = () => act(async () => {
    if (!(await check())) return;
    const r = await api<{ id: string }>(`/crewai/crews/${crew.crew}/test`, { method: "POST", body: { config: draft, run_id: testRun } });
    nav(`/crewai/runs/${r.id}`);
  });
  const activate = (version: number | null) => act(async () => {
    const what = version === null ? "the code's defaults" : `version ${version}`;
    if (!(await confirm({ title: `Activate ${what}`, body: `New runs will use ${what}. Runs already queued keep what they recorded.`, action: "Activate" }))) return;
    await api(`/crewai/crews/${crew.crew}/activate`, { method: "POST", body: { version } });
    toast(`${what[0].toUpperCase()}${what.slice(1)} active`);
    reload();
  });
  const load = (version: number | null) => act(async () => {
    if (dirty && !(await confirm({ title: "Discard your edits?", body: "The editor will show the version you chose instead.", action: "Discard" }))) return;
    if (version === null) { setDraft(clone(defaults)); setLoaded("the code's defaults"); }
    else { const v = await api(`/crewai/crews/${crew.crew}/versions/${version}`); setDraft(clone(v.effective)); setLoaded(`version ${version}`); }
    setErrors([]); setWarnings([]); setChecked(null);
  });

  return <div className="stack">
    {crew.active?.upstream_changed && <div className="alert warn small">The code's default prompts changed since version {crew.active.version} was saved. Fields it edits keep its text; compare them with Diff.</div>}
    <div className="split-3-2 grid">
      <div className="stack">
        {AGENTS.map((k) => <Section key={k} title={<>{AGENT_LABEL[k]} <span className="muted small">· {draft.agents[k].role}</span></>}>
          <Field label="Role" path={`agents.${k}.role`} value={draft.agents[k].role} base={defaults.agents[k].role} issues={issues} max={crew.text_limits.role} readOnly={!canEdit}
            onChange={(v) => set("agents", k, "role", v)} hint="Also the step's name in the log." />
          <Field label="Goal" path={`agents.${k}.goal`} value={draft.agents[k].goal} base={defaults.agents[k].goal} issues={issues} multiline max={crew.text_limits.goal} readOnly={!canEdit}
            onChange={(v) => set("agents", k, "goal", v)} />
          <Field label="Backstory" path={`agents.${k}.backstory`} value={draft.agents[k].backstory} base={defaults.agents[k].backstory} issues={issues} multiline max={crew.text_limits.backstory}
            readOnly={!canEdit} onChange={(v) => set("agents", k, "backstory", v)} hint="MOKS appends its untrusted-data rule to every backstory." />
          {crew.tools[k] && <fieldset className="cw-tools" disabled={!canEdit}>
            <legend className="cw-flabel">Tools {!same(draft.agents[k].tools, defaults.agents[k].tools) && <Badge kind="info">edited</Badge>}</legend>
            {crew.tools[k].map((t: any) => <label key={t.name} className="cw-check">
              <input type="checkbox" checked={draft.agents[k].tools.includes(t.name)}
                onChange={(e) => set("agents", k, "tools", e.target.checked ? crew.tools[k].map((x: any) => x.name).filter((n: string) => n === t.name || draft.agents[k].tools.includes(n)) : draft.agents[k].tools.filter((n: string) => n !== t.name))} />
              <span><span className="mono">{t.name}</span> <span className="muted small">({t.moks_tool}) {t.description}</span></span>
            </label>)}
            {issues.filter((i) => i.field === `agents.${k}.tools`).map((m, i) => <div key={i} className="small warn-text">{m.message}</div>)}
          </fieldset>}
          <Field label="Task" path={`tasks.${k}.description`} value={draft.tasks[k].description} base={defaults.tasks[k].description} issues={issues} multiline max={crew.text_limits.description}
            readOnly={!canEdit} onChange={(v) => set("tasks", k, "description", v)} hint={<span>Placeholders: {crew.placeholders.map((p: string) => <code key={p}>{p} </code>)}{k === "review" ? " · MOKS adds the JSON output contract after it." : ""}</span>} />
          <Field label="Expected output" path={`tasks.${k}.expected_output`} value={draft.tasks[k].expected_output} base={defaults.tasks[k].expected_output} issues={issues} multiline
            max={crew.text_limits.expected_output} readOnly={!canEdit} onChange={(v) => set("tasks", k, "expected_output", v)} />
        </Section>)}
        <Section title="Limits and defaults">
          <div className="cw-inputs">
            {Object.keys(defaults.limits).map((k) => <label key={k}>{LIMIT_LABEL[k] ?? k} {draft.limits[k] !== defaults.limits[k] && <Badge kind="info">edited</Badge>}
              <input type="number" min={crew.ceilings[k][0]} max={crew.ceilings[k][1]} value={draft.limits[k]} readOnly={!canEdit} onChange={(e) => setTop("limits", k, Number(e.target.value))} />
              <span className="small muted">{crew.ceilings[k][0]}–{crew.ceilings[k][1]}, code {defaults.limits[k]}</span></label>)}
            <label>Default kind<select value={draft.defaults.kind} disabled={!canEdit} onChange={(e) => setTop("defaults", "kind", e.target.value)}>
              {WORKLOAD_KINDS.map((x) => <option key={x}>{x}</option>)}</select></label>
            <label>Default window (min)<input type="number" min={5} max={10080} placeholder="deployment default" value={draft.defaults.minutes ?? ""} readOnly={!canEdit}
              onChange={(e) => setTop("defaults", "minutes", e.target.value === "" ? null : Number(e.target.value))} /></label>
          </div>
          {issues.filter((i) => i.field.startsWith("limits") || i.field.startsWith("defaults") || i.field === "agents" || i.field === "tasks").map((m, i) =>
            <div key={i} className={`small ${errors.includes(m) ? "danger-text" : "warn-text"}`}><code>{m.field}</code>: {m.message}</div>)}
        </Section>
      </div>
      <div className="stack cw-sticky">
        <Section title="This draft">
          <div className="small">Started from {loaded}. {edits ? `${edits} field(s) differ from the code's defaults.` : "Same as the code's defaults."} {dirty && <Badge kind="warn">unsaved</Badge>}</div>
          {checked === JSON.stringify(draft) && !errors.length && <div className="alert info small">Check passed{warnings.length ? ` with ${warnings.length} warning(s)` : ""}.</div>}
          {errors.length > 0 && <div className="alert error small">{errors.length} problem(s) to fix before saving or testing.</div>}
          <ErrorBox error={error} />
          {canEdit ? <>
            <div className="row"><button type="button" className="ghost" disabled={busy} onClick={() => act(async () => { await check(); })}>Check</button></div>
            <label>Note for this version<input value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} placeholder="what changed and why" /></label>
            <div className="row"><button type="button" className="ghost" disabled={busy} onClick={() => save(false)}>Save as new version</button>
              <button type="button" disabled={busy} onClick={() => save(true)}>Save and activate</button></div>
            <div className="form-section">
              <div className="cw-flabel">Test run</div>
              <div className="small muted">Runs the draft on a finished run's inputs without saving it, like Jenkins' Replay. Afterwards the run page offers "Save as new version".</div>
              {crew.runs_to_test?.length ? <>
                <label>On the inputs of<select value={testRun} onChange={(e) => setTestRun(e.target.value)}>
                  {crew.runs_to_test.map((r: RunRow) => <option key={r.id} value={r.id}>#{r.number} {r.namespace}/{r.workload} ({r.environment}) · {r.status}</option>)}</select></label>
                <div className="row"><button type="button" disabled={busy || !testRun} onClick={test}>Test run</button></div>
              </> : <div className="small muted">No finished run to test on yet.</div>}
            </div>
          </> : <div className="small muted">Read only: changing the crew needs crewai.admin.</div>}
        </Section>
        <Section title="Versions" actions={canEdit && crew.active ? <button type="button" className="ghost small" disabled={busy} onClick={() => activate(null)}>Use the code's defaults</button> : undefined}>
          <ul className="cw-versions">
            <li className={!crew.active ? "active" : ""}>
              <div className="row between"><span><strong>Code defaults</strong> {!crew.active && <Badge kind="ok">active</Badge>}</span>
                <button type="button" className="ghost small" onClick={() => load(null)}>Open</button></div>
            </li>
            {crew.versions.map((v: any) => <li key={v.version} className={v.active ? "active" : ""}>
              <div className="row between">
                <span><strong>Version {v.version}</strong> {v.active && <Badge kind="ok">active</Badge>} {v.upstream_changed && <Badge kind="warn">upstream changed</Badge>}</span>
                <span className="row tight">
                  <button type="button" className="ghost small" onClick={() => load(v.version)}>Open</button>
                  {canEdit && !v.active && <button type="button" className="ghost small" disabled={busy} onClick={() => activate(v.version)}>Activate</button>}
                </span>
              </div>
              <div className="small muted">{v.fields} edited field(s) · {v.created_by ?? "unknown"} · <Time iso={v.created_at} /></div>
              {v.note && <div className="small">{v.note}</div>}
            </li>)}
          </ul>
        </Section>
        <Section title="Stays in code">
          <div className="small">{crew.code_owned.structure}</div>
          <details><summary className="small">Untrusted-data rule (appended to every backstory)</summary><pre className="cw-data mono">{crew.code_owned.rules}</pre></details>
          <details><summary className="small">Reviewer's output contract</summary><pre className="cw-data mono">{crew.code_owned.output_contract}</pre></details>
        </Section>
        {crew.runs_to_test?.length > 0 && <Section title="Recent runs">
          <ul className="cw-mini">{crew.runs_to_test.slice(0, 5).map((r: RunRow) => <li key={r.id}><StatusIcon status={r.status} crew_status={r.crew_status} />{" "}
            <Link to={`/crewai/runs/${r.id}`}>#{r.number}</Link> <span className="small muted">{r.namespace}/{r.workload} · {TRIGGERS[r.trigger] ?? r.trigger} · {r.config_version ? `v${r.config_version}` : "defaults"}</span></li>)}</ul>
        </Section>}
      </div>
    </div>
  </div>;
}
