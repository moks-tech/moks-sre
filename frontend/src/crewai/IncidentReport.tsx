import type { ReactNode } from "react";
import { Badge } from "../components";
import { ClaimLabel } from "../evidence";
import type { Ev } from "../investigationStream";

// The CrewAI incident investigation's result (docs/crewai-sre-engine.md): what MOKS graded, not what the agents claimed.
// Every status, confidence and criterion here was decided in code from the evidence; agents supplied the words.

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";
export const STATUS: Record<string, [string, Tone]> = {
  confirmed_observation: ["Confirmed observation", "ok"],
  strongly_supported: ["Strongly supported hypothesis", "info"],
  possible_explanation: ["Possible explanation", "warn"],
  insufficient_evidence: ["Insufficient evidence", "neutral"],
};
const SEVERITY: Record<string, Tone> = { SEV1: "bad", SEV2: "bad", SEV3: "warn", SEV4: "info", unknown: "neutral" };
const TASK: Record<string, Tone> = { done: "ok", restored: "ok", partial: "warn", invalid_output: "warn", failed: "bad", skipped: "neutral", running: "info", pending: "neutral" };
const VERDICT: Record<string, Tone> = { supported: "ok", unsupported: "bad", contradicted: "bad", insufficient: "warn" };
const REQUEST: Record<string, Tone> = { collected: "ok", already_collected: "neutral", refused: "bad", not_run: "warn" };
const WINDOWS = ["15m", "1h", "6h", "24h"];
const TASK_LABEL: Record<string, string> = { plan: "Plan", kubernetes: "Kubernetes", observability: "Observability", change: "Changes", knowledge: "History",
  hypotheses: "Hypotheses", review: "Review", rca: "Root cause", evidence: "Evidence for the review" };
const MARK: Record<Tone, string> = { ok: "✓", warn: "!", bad: "✕", info: "•", neutral: "–" };

function Refs({ refs, onCite }: { refs?: string[]; onCite: (r: string) => void }) {
  return <>{(refs ?? []).map((r) => <button type="button" key={r} className="cite" onClick={() => onCite(r)}>{r}</button>)}</>;
}

function Mark({ tone, children, live }: { tone: Tone; children: ReactNode; live?: boolean }) {
  return <span className={`cw-status ${tone}${live ? " live" : ""}`}><span className="cw-mark" aria-hidden="true">{MARK[tone]}</span>{children}</span>;
}

function Confidence({ value }: { value: number }) {
  return <span className="row tight nowrap" title="Sum of the weights of the criteria that passed"><span className="bar" style={{ minWidth: 60 }}><span style={{ width: `${Math.round(value * 100)}%` }} /></span>
    <span className="small">{value.toFixed(2)}</span></span>;
}

function Criteria({ items }: { items: any[] }) {
  return <span className="row tight wrap">{items.map((c) => <span key={c.name} className="chip" title={c.detail} data-met={c.met ? "yes" : "no"}>
    <span aria-hidden="true">{c.met ? "✓" : "✕"}</span> {c.name.replace(/_/g, " ")}<span className="sr-only">{c.met ? " met" : " not met"}: {c.detail}</span></span>)}</span>;
}

const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");

function Part({ title, count, open = true, children }: { title: string; count?: number; open?: boolean; children: ReactNode }) {
  return <details className="rs" open={open}><summary>{title}{count !== undefined && <span className="count">{count}</span>}</summary>{children}</details>;
}

export function IncidentReport({ crew, onCite }: { crew: any; onCite: (ref: string) => void }) {
  const r = crew.investigation;
  const sev = r.severity ?? { level: "unknown", criteria: [] };
  const top = (r.hypotheses ?? []).find((h: any) => h.status === "possible_explanation");
  const usage = crew.usage ?? {};
  return <div className="stack" aria-label="CrewAI incident investigation">
    <div className="rs static">
      <h3 className="row tight wrap">Incident investigation <Badge kind={crew.status === "completed" ? "ok" : "warn"}>{crew.status}</Badge>
        <Badge kind={SEVERITY[sev.level] ?? "neutral"}>{sev.level === "unknown" ? "severity unknown" : sev.level}</Badge>
        <span className="small muted">crewai {crew.version} · {crew.tool_calls} tool calls · {usage.total?.tokens ?? 0} tokens</span></h3>
      <div className="small muted">Severity graded by MOKS: {sev.criteria.join("; ")}{sev.proposed && sev.proposed !== sev.level ? `. The incident commander proposed ${sev.proposed}.` : "."}</div>
      {crew.stopped && <div className="alert warn small">Stopped early: {crew.stopped}. The report covers the steps that ran.</div>}
      {crew.fallback === "langgraph_report" && <div className="alert warn small">No finding or hypothesis survived validation, so MOKS's standard report writer wrote this report from the evidence the crew collected.</div>}
      <div className="stack" style={{ marginTop: 8 }}>
        {r.root_cause ? <div><ClaimLabel kind="hypothesis" /> <strong>{STATUS[r.root_cause.status]?.[0]} ({r.root_cause.hypothesis}):</strong> {r.root_cause.statement}
          <div className="small muted">Not confirmed: MOKS never confirms a root cause. Confidence {r.root_cause.confidence.toFixed(2)} from the criteria below.</div></div>
          : <div><ClaimLabel kind="missing" /> <strong>No root cause is established.</strong>{top && <> The most likely explanation is {top.id}: {top.statement}</>}</div>}
        {r.impact && <div className="small"><strong>Impact:</strong> {r.impact}</div>}
        {r.symptom_onset && <div className="small"><strong>First symptom in the evidence:</strong> {when(r.symptom_onset)}</div>}
      </div>
    </div>

    <Part title="Workflow" count={(r.tasks ?? []).length} open={false}>
      <ol className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {(r.tasks ?? []).map((t: any) => <li key={`${t.task}-${t.round}`} className="row tight wrap">
          <Mark tone={TASK[t.status] ?? "neutral"}>{t.label}</Mark>
          <span className="small muted">{t.status.replace(/_/g, " ")}{t.ended_by ? ` (${t.ended_by})` : ""} · {t.seconds}s · {t.model_calls} model / {t.tool_calls} tool calls{t.attempts > 1 ? ` · ${t.attempts} attempts` : ""}</span>
        </li>)}
      </ol>
    </Part>

    <Part title="Hypotheses" count={(r.hypotheses ?? []).length}>
      {(r.hypotheses ?? []).length === 0 ? <div className="small muted">No hypothesis cited recorded evidence.</div> :
        <div className="table-wrap"><table>
          <thead><tr><th>#</th><th>Hypothesis</th><th>Status</th><th>Confidence</th><th>Evidence</th></tr></thead>
          <tbody>{r.hypotheses.map((h: any) => <tr key={h.id}>
            <td className="mono">{h.id}</td>
            <td>{h.statement}{h.would_confirm && <div className="small muted">Would settle it: {h.would_confirm}</div>}
              <div style={{ marginTop: 4 }}><Criteria items={h.criteria} /></div>
              {h.review && <div className="small">Reviewer: <Badge kind={VERDICT[h.review.verdict] ?? "neutral"}>{h.review.verdict}</Badge> {h.review.reason}</div>}</td>
            <td><Badge kind={STATUS[h.status]?.[1] ?? "neutral"}>{STATUS[h.status]?.[0] ?? h.status}</Badge></td>
            <td><Confidence value={h.confidence} /></td>
            <td className="nowrap"><Refs refs={h.supporting} onCite={onCite} />{h.contradicting?.length > 0 && <div className="small">against <Refs refs={h.contradicting} onCite={onCite} /></div>}</td>
          </tr>)}</tbody></table></div>}
    </Part>

    <Part title="Findings" count={(r.findings ?? []).length}>
      <div className="table-wrap"><table>
        <thead><tr><th>#</th><th>Observation</th><th>Status</th><th>Confidence</th><th>When</th><th>Evidence</th></tr></thead>
        <tbody>{(r.findings ?? []).map((f: any) => <tr key={f.id}>
          <td className="mono">{f.id}</td>
          <td>{f.observation}<div className="small muted"><span className="mono">{f.resource || "no resource named"}</span> {f.resource && (f.resource_verified ? "· named in the evidence" : "· not named in the evidence")}
            {" · "}{f.task === "detectors" ? "MOKS detector" : TASK_LABEL[f.task] ?? f.task} · {f.evidence_source.join(", ")}{f.hypotheses?.length > 0 && ` · supports ${f.hypotheses.join(", ")}`}</div></td>
          <td><Badge kind={STATUS[f.status]?.[1] ?? "neutral"}>{STATUS[f.status]?.[0] ?? f.status}</Badge>{f.review && <div className="small">reviewer: {f.review}</div>}</td>
          <td><Confidence value={f.confidence} /></td>
          <td className="small nowrap">{f.event_time ? when(f.event_time) : `read ${when(f.timestamp)}`}
            <div className="muted">{f.time_range?.point_in_time ? "point in time" : `${when(f.time_range?.from)} – ${when(f.time_range?.to)}`}</div></td>
          <td className="nowrap"><Refs refs={f.citations} onCite={onCite} /></td>
        </tr>)}</tbody></table></div>
    </Part>

    <Part title={`Changes in the last ${r.change_window}`} count={(r.changes ?? []).length}>
      {(r.changes ?? []).length === 0 ? <div className="small muted">No change was recorded in the window by the sources that could be read.</div> :
        WINDOWS.map((w) => {
          const rows = r.changes.filter((c: any) => c.window === w);
          return rows.length > 0 && <div key={w}><div className="small"><strong>Within {w}</strong></div>
            <ul className="small">{rows.map((c: any) => <li key={c.id}><span className="mono">{c.id}</span> {c.kind.replace(/_/g, " ")} <span className="mono">{c.resource}</span>, {c.minutes_before} min before: {c.detail}
              {c.before_symptoms === true && <> <Badge kind="warn">before the symptoms</Badge></>}<Refs refs={c.evidence} onCite={onCite} /></li>)}</ul></div>;
        })}
    </Part>

    {(r.timeline ?? []).length > 0 && <Part title="Timeline" count={r.timeline.length} open={false}>
      <ul className="timeline">{r.timeline.map((e: any, i: number) => <li key={i}><span className="small muted">{when(e.at)}</span><span className={`dot ${e.source === "rca" ? "run" : "ok"}`} />
        <span>{e.event}{e.source === "rca" && <span className="small muted"> (from the RCA, checked against its citations)</span>}<Refs refs={e.evidence} onCite={onCite} /></span></li>)}</ul>
    </Part>}

    <Part title="Independent review" count={r.review?.rounds ?? 0} open={false}>
      {(r.review?.conflicts ?? []).length > 0 && <div className="small"><strong>Conflicting observations</strong><ul>{r.review.conflicts.map((c: any, i: number) => <li key={i}>{c.ids.join(" vs ")}: {c.description}</li>)}</ul></div>}
      {(r.review?.missing_telemetry ?? []).length > 0 && <div className="small"><strong>Missing telemetry</strong><ul>{r.review.missing_telemetry.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>}
      {(r.requests ?? []).length > 0 && <div className="small"><strong>Evidence the reviewer asked for</strong>
        <ul>{r.requests.map((q: any, i: number) => <li key={i}>round {q.round}: <span className="mono">{q.tool}</span> {JSON.stringify(q.args)} <Badge kind={REQUEST[q.status] ?? "neutral"}>{q.status.replace(/_/g, " ")}</Badge>
          {q.evidence && <Refs refs={[q.evidence]} onCite={onCite} />}{q.why && <span className="muted"> {q.why}</span>}</li>)}</ul></div>}
      {!r.review?.rounds && <div className="small muted">The review did not run.</div>}
    </Part>

    {((r.unsupported ?? []).length > 0 || (crew.validation?.citations_rejected ?? []).length > 0) && <Part title="Set aside by MOKS" count={(r.unsupported ?? []).length} open={false}>
      {(crew.validation?.citations_rejected ?? []).length > 0 && <div className="small">Invented citations rejected: <span className="mono">{crew.validation.citations_rejected.join(", ")}</span></div>}
      <ul className="small">{(r.unsupported ?? []).map((u: any, i: number) => <li key={i}>{u.statement} <span className="muted">({TASK_LABEL[u.task] ?? u.task}: {u.reason})</span></li>)}</ul>
    </Part>}

    <Part title="Usage" open={false}>
      <div className="table-wrap"><table>
        <thead><tr><th>Agent</th><th>Model calls</th><th>Tool calls</th><th>Tokens in</th><th>Tokens out</th><th>Cost</th></tr></thead>
        <tbody>{Object.entries(usage.by_agent ?? {}).map(([k, u]: [string, any]) => <tr key={k}><td>{k}</td><td>{u.model_calls}{u.retries ? ` (${u.retries} retried)` : ""}</td><td>{u.tool_calls}</td>
          <td>{u.tokens_in}</td><td>{u.tokens_out}</td><td>${Number(u.cost_usd).toFixed(4)}</td></tr>)}</tbody></table></div>
      <div className="small muted">{usage.total?.tokens ?? 0} of a {usage.token_budget} token budget{usage.budget_reached ? ", which was reached" : ""}.</div>
      {(crew.errors ?? []).length > 0 && <ul className="small">{crew.errors.map((e: any, i: number) => <li key={i}><span className="mono">{e.code}</span>{e.task ? ` (${e.task})` : ""}: {e.message}</li>)}</ul>}
    </Part>
  </div>;
}

/** The incident flow's steps as they happen, from the investigation's events. */
export function CrewProgress({ events }: { events: Ev[] }) {
  const steps: { key: string; label: string; status: string; detail?: string }[] = [];
  for (const e of events) {
    const c = e.crewai;
    if (!c?.task) continue;
    const key = `${c.task}-${c.round ?? 1}`;
    const known = steps.find((s) => s.key === key);
    if (e.type === "phase") {
      steps.forEach((s) => { if (s.status === "running") s.status = "done"; });
      if (!known) steps.push({ key, label: String(e.label ?? "").replace(/^CrewAI: /, ""), status: "running" });
    } else if (e.type === "note" && known) {
      known.status = c.status;
      known.detail = `${c.seconds}s · ${c.tool_calls} tool calls`;
    }
  }
  if (!steps.length) return null;
  const over = events.some((e) => e.type === "done" || e.type === "report");
  return <ol className="stack" aria-label="CrewAI steps" style={{ listStyle: "none", padding: 0, margin: "0 0 10px" }}>
    {steps.map((s) => {
      const status = over && s.status === "running" ? "done" : s.status;
      return <li key={s.key}><Mark tone={TASK[status] ?? "neutral"} live={status === "running"}>{s.label}</Mark> {s.detail && <span className="small muted">{s.detail}</span>}</li>;
    })}
  </ol>;
}
