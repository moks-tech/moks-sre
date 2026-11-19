import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { API, api } from "../api";
import { ErrorBox, useInterval } from "../components";
import { CopyButton, Drawer } from "../ui";
import { duration, money, StepIcon, type Step } from "./shared";

// A run's log as GitHub Actions shows one: a group per step, open by itself when it failed or is running, search with
// highlights, a level filter (debug hidden at first), timestamps, #L<id> line links, follow while live, download as
// text, the details of a line, and a model call's transcript one click away.

export interface Line { id: number; step: string; at: string; level: string; message: string; data: Record<string, any> | null; call_id: number | null }

const LEVELS: Record<string, number> = { debug: 0, info: 1, warning: 2, error: 3 };
const POLL_MS = 1500;

function Highlight({ text, q }: { text: string; q: string }) {
  if (!q) return <>{text}</>;
  const parts: ReactNode[] = [];
  const low = text.toLowerCase(), needle = q.toLowerCase();
  let at = 0, i = low.indexOf(needle);
  while (i >= 0) {
    parts.push(text.slice(at, i), <mark key={i}>{text.slice(i, i + q.length)}</mark>);
    at = i + q.length;
    i = low.indexOf(needle, at);
  }
  parts.push(text.slice(at));
  return <>{parts}</>;
}

export function Transcript({ id, onClose }: { id: number; onClose: () => void }) {
  const [t, setT] = useState<any>();
  const [error, setError] = useState<unknown>();
  const [tab, setTab] = useState<"response" | "prompt" | "system">("response");
  useEffect(() => { api(`/crewai/calls/${id}`).then(setT, setError); }, [id]);
  const body = !t ? "" : tab === "response" ? t.response || (t.error ? `(no answer: ${t.error})` : "") : tab === "system" ? t.system
    : (t.prompt ?? []).map((m: any) => `── ${m.role} ──\n${m.content}`).join("\n\n");
  return <Drawer title={`Model call transcript${t ? ` · ${t.step}` : ""}`} onClose={onClose}>
    <ErrorBox error={error} />
    {t && <div className="stack">
      <div className="small muted">{t.model} · {t.tokens_in} in / {t.tokens_out} out · {money(t.cost_usd)} · {duration(t.duration_ms / 1000)} · {t.outcome}</div>
      <div className="tabs" role="tablist" aria-label="Transcript parts">
        {(["response", "prompt", "system"] as const).map((k) => <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? "active" : ""} onClick={() => setTab(k)}>
          {k === "response" ? "Answer" : k === "prompt" ? "Prompt" : "System"}</button>)}
      </div>
      <div className="row end"><CopyButton text={body} /></div>
      <pre className="cw-transcript mono">{body}</pre>
      <div className="small muted">Kept for the console's transcript retention. Evidence inside it was redacted before the model saw it.</div>
    </div>}
  </Drawer>;
}

export function LogViewer({ runId, initialSteps, onSteps }: { runId: string; initialSteps: Step[]; onSteps?: (steps: Step[], status: string) => void }) {
  const [lines, setLines] = useState<Line[]>([]);
  const [steps, setSteps] = useState<Step[]>(initialSteps);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<unknown>();
  const [q, setQ] = useState("");
  const [level, setLevel] = useState("info");
  const [stamps, setStamps] = useState(false);
  const [follow, setFollow] = useState(true);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [details, setDetails] = useState<Record<number, boolean>>({});
  const [transcript, setTranscript] = useState<number | null>(null);
  const last = useRef(0);
  const busy = useRef(false);
  const box = useRef<HTMLDivElement>(null);
  const loc = useLocation();
  const target = /^#L(\d+)$/.exec(loc.hash)?.[1];

  async function pull() {
    if (busy.current) return;
    busy.current = true;
    try {
      let more = true;
      while (more) {
        const page = await api<{ lines: Line[]; last: number; done: boolean; status: string; steps: Step[] }>(`/crewai/runs/${runId}/logs?after=${last.current}&limit=2000`);
        if (page.lines.length) setLines((l) => [...l, ...page.lines]);
        last.current = page.last;
        setSteps(page.steps);
        setDone(page.done);
        onSteps?.(page.steps, page.status);
        more = page.lines.length >= 2000;
      }
      setError(undefined);
    } catch (e) { setError(e); } finally { busy.current = false; }
  }
  useEffect(() => { last.current = 0; setLines([]); pull(); }, [runId]); // eslint-disable-line react-hooks/exhaustive-deps
  useInterval(pull, POLL_MS, !done);

  const min = LEVELS[level] ?? 1;
  const shown = useMemo(() => lines.filter((l) => (LEVELS[l.level] ?? 1) >= min && (!q || l.message.toLowerCase().includes(q.toLowerCase()) || l.step.includes(q.toLowerCase()))), [lines, min, q]);
  const byStep = useMemo(() => {
    const m: Record<string, Line[]> = {};
    for (const l of shown) (m[l.step] ??= []).push(l);
    return m;
  }, [shown]);
  const counts = useMemo(() => {
    const m: Record<string, { warn: number; err: number }> = {};
    for (const l of lines) { const c = (m[l.step] ??= { warn: 0, err: 0 }); if (l.level === "warning") c.warn++; if (l.level === "error") c.err++; }
    return m;
  }, [lines]);
  const isOpen = (s: Step) => open[s.step] ?? (s.status === "failed" || s.status === "running" || !!q || (target ? lines.some((l) => String(l.id) === target && l.step === s.step) : false));

  useEffect(() => {
    if (follow && !done && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [shown.length, follow, done]);
  useEffect(() => {
    if (!target) return;
    document.getElementById(`L${target}`)?.scrollIntoView({ block: "center" });
  }, [target, lines.length]);

  const extra = lines.filter((l) => !steps.some((s) => s.step === l.step)).length;
  return <div className="cw-log card">
    <div className="cw-log-bar">
      <input type="search" aria-label="Search the log" placeholder="Search the log" value={q} onChange={(e) => setQ(e.target.value)} />
      <label className="cw-inline">Level
        <select value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="debug">everything</option><option value="info">info and above</option><option value="warning">warnings and errors</option><option value="error">errors</option>
        </select></label>
      <label className="cw-check"><input type="checkbox" checked={stamps} onChange={(e) => setStamps(e.target.checked)} /> Timestamps</label>
      {!done && <label className="cw-check"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Follow</label>}
      <span className="grow" />
      {q && <span className="small muted">{shown.length} matching line{shown.length === 1 ? "" : "s"}</span>}
      <a className="btn ghost small" href={`${API}/crewai/runs/${runId}/logs?format=text`} download>Download log</a>
    </div>
    <ErrorBox error={error} onRetry={pull} />
    <div className="cw-log-body" ref={box} role="log" aria-live={done ? "off" : "polite"} aria-label="Run log">
      {steps.map((s) => {
        const rows = byStep[s.step] ?? [];
        const c = counts[s.step];
        return <details key={s.step} className={`cw-group ${s.status}`} open={isOpen(s)} onToggle={(e) => {
          const now = (e.target as HTMLDetailsElement).open;
          if (now !== isOpen(s)) setOpen((o) => ({ ...o, [s.step]: now }));
        }}>
          <summary>
            <StepIcon status={s.status} />
            <span className="cw-group-name">{s.label}</span>
            {c?.err ? <span className="badge bad">{c.err} error{c.err === 1 ? "" : "s"}</span> : null}
            {c?.warn ? <span className="badge warn">{c.warn} warning{c.warn === 1 ? "" : "s"}</span> : null}
            <span className="grow" />
            <span className="small muted">{s.status === "pending" || s.status === "skipped" ? s.status : duration(s.seconds)}</span>
          </summary>
          {s.error && <div className="cw-line error"><span className="cw-ln" /><span className="cw-msg">{s.error}</span></div>}
          {rows.length === 0 && !s.error && <div className="cw-line muted"><span className="cw-ln" /><span className="cw-msg">{s.status === "pending" ? "Waiting to start." : s.status === "skipped" ? "Did not run." : "No lines at this level."}</span></div>}
          {rows.map((l) => <Fragment key={l.id}>
            <div id={`L${l.id}`} className={`cw-line ${l.level}${String(l.id) === target ? " target" : ""}`}>
              <a className="cw-ln" href={`#L${l.id}`} aria-label={`Line ${l.id}`}>{l.id}</a>
              <span className="cw-ts">{stamps ? new Date(l.at).toISOString().slice(11, 23) : null}</span>
              <span className="cw-msg"><Highlight text={l.message} q={q} /></span>
              <span className="cw-acts">
                {l.call_id && <button type="button" className="ghost small" onClick={() => setTranscript(l.call_id!)}>Transcript</button>}
                {l.data && <button type="button" className="ghost small" aria-expanded={!!details[l.id]} onClick={() => setDetails((d) => ({ ...d, [l.id]: !d[l.id] }))}
                  title="Details">{"{…}"}</button>}
              </span>
            </div>
            {details[l.id] && l.data && <pre className="cw-data mono">{JSON.stringify(l.data, null, 2)}</pre>}
          </Fragment>)}
        </details>;
      })}
      {extra > 0 && <div className="small muted">{extra} line(s) belong to no step.</div>}
      {!lines.length && done && <div className="cw-line muted"><span className="cw-ln" /><span className="cw-msg">No log was recorded for this run (it may predate the console, or its lines are past their retention).</span></div>}
    </div>
    {transcript !== null && <Transcript id={transcript} onClose={() => setTranscript(null)} />}
  </div>;
}
