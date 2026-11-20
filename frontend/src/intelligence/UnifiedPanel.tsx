import { useMemo, useState } from "react";
import { Badge } from "../components";
import type { Ev } from "../investigationStream";
import "./intelligence.css";

// The unified investigation (docs/UNIFIED_INVESTIGATION.md): fifteen stages over every source MOKS reads, hypotheses
// scored by stated factors, and every claim one click from the stored evidence behind it (onCite opens the evidence drawer).

type Cite = (ref: string) => void;
type Kind = "ok" | "warn" | "bad" | "info" | "neutral";

const STATUS_MARK: Record<string, string> = { completed: "✓", partial: "!", skipped: "–", failed: "✕", cancelled: "■", running: "…", pending: "" };
const BAND: Record<string, Kind> = { strong: "ok", moderate: "info", weak: "warn", insufficient: "neutral" };
const VERDICT: Record<string, Kind> = { supported: "ok", partially_supported: "info", contradicted: "bad", unsupported: "neutral" };
const VERDICT_TEXT: Record<string, string> = { supported: "supported", partially_supported: "partly supported", contradicted: "contradicted", unsupported: "unsupported" };
const TIMELINE: Record<string, Kind> = { change: "info", symptom: "bad", alert: "warn", anomaly: "warn", event: "neutral" };
const FACTOR: Record<string, string> = {
  evidence_strength: "Evidence strength", source_reliability: "Source reliability", temporal_correlation: "Temporal correlation", dependency_relevance: "Dependency relevance",
  historical_similarity: "Historical similarity", contradictions: "Contradictory evidence", missing_evidence: "Missing evidence", explained_by_stronger: "Explained by a stronger hypothesis",
};

export function Refs({ refs, onCite }: { refs?: string[]; onCite: Cite }) {
  if (!refs?.length) return null;
  return <span className="un-refs">{refs.map((r) => <button type="button" key={r} className="cite" onClick={() => onCite(r)} aria-label={`Open evidence ${r}`}>{r}</button>)}</span>;
}

export function PipelineStrip({ stages, onPick, picked }: { stages: any[]; onPick?: (key: string) => void; picked?: string | null }) {
  return (
    <ol className="un-strip" aria-label="Investigation stages">
      {stages.map((s, i) => (
        <li key={s.key}>
          <button type="button" className={`un-stage ${s.status}${picked === s.key ? " picked" : ""}`} onClick={() => onPick?.(s.key)} aria-pressed={picked === s.key}
            title={`${i + 1}. ${s.label}: ${s.status}${s.reason ? ` (${s.reason})` : ""}`} aria-label={`Stage ${i + 1}, ${s.label}: ${s.status}`}>
            <span className="un-stage-n">{STATUS_MARK[s.status] || i + 1}</span><span className="un-stage-label">{s.label}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

/** Live progress from the `pipeline` events, while the run is going. */
export function LivePipeline({ events }: { events: Ev[] }) {
  const last = [...events].reverse().find((e) => e.type === "pipeline");
  if (!last) return null;
  const stages: any[] = last.stages ?? [];
  const running = stages.find((s) => s.status === "running");
  const done = stages.filter((s) => !["pending", "running"].includes(s.status)).length;
  return (
    <div className="card un-live" aria-live="polite">
      <div className="row between"><h2>Unified investigation</h2>
        <span className="small muted">{done}/{stages.length} stages · {last.budget?.tool_calls?.used ?? 0}/{last.budget?.tool_calls?.limit ?? "?"} tool calls</span></div>
      <PipelineStrip stages={stages} />
      {running && <div className="small">Now: <strong>{running.label}</strong></div>}
    </div>
  );
}

function ScoreBar({ score, band }: { score: number; band: string }) {
  return (
    <span className="un-score" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={score} aria-label={`Score ${score} of 100, ${band}`}>
      <span className={`un-score-fill ${band}`} style={{ width: `${Math.max(2, score)}%` }} />
      <span className="un-score-text">{score}/100</span>
    </span>
  );
}

function Factors({ score, onCite }: { score: any; onCite: Cite }) {
  return (
    <div className="un-factors">
      <table>
        <thead><tr><th>Factor</th><th className="num">Weight</th><th className="num">Value</th><th className="num">Points</th><th>Why</th></tr></thead>
        <tbody>
          {(score.factors ?? []).map((f: any) => (
            <tr key={f.name}><td>{FACTOR[f.name] ?? f.name}</td><td className="num">{f.weight}</td><td className="num">{Number(f.value).toFixed(2)}</td>
              <td className="num">+{Math.round(f.contribution * 100)}</td><td className="small">{f.explanation}<Refs refs={f.evidence} onCite={onCite} /></td></tr>
          ))}
          {(score.penalties ?? []).map((p: any) => (
            <tr key={p.name} className={p.value > 0 ? "penalty" : "muted"}><td>{FACTOR[p.name] ?? p.name}</td><td className="num">–</td><td className="num">{Number(p.value).toFixed(2)}</td>
              <td className="num">{p.value > 0 ? `−${Math.round(p.value * 100)}` : "0"}</td><td className="small">{p.explanation}<Refs refs={p.evidence} onCite={onCite} /></td></tr>
          ))}
        </tbody>
      </table>
      {(score.caps ?? []).map((c: any, i: number) => <div key={i} className="small un-cap">Capped at {c.cap}: {c.reason}</div>)}
      <div className="small muted">A ranking within this investigation by the rules above, not a probability.</div>
    </div>
  );
}

function Hypothesis({ h, onCite, open }: { h: any; onCite: Cite; open: boolean }) {
  const r = h.review ?? {};
  return (
    <details className="un-hyp" open={open}>
      <summary>
        <span className="un-rank">#{h.rank}</span>
        <span className="un-hyp-title">{h.title}</span>
        <ScoreBar score={h.score?.score ?? 0} band={h.score?.band ?? "insufficient"} />
        <Badge kind={BAND[h.score?.band] ?? "neutral"}>{h.score?.band}</Badge>
        {r.verdict && <Badge kind={VERDICT[r.verdict] ?? "neutral"}>{VERDICT_TEXT[r.verdict] ?? r.verdict}</Badge>}
        {h.origin !== "rules" && <Badge kind="info">{h.origin === "agent" ? "proposed by the analyst agent" : "rules + analyst agent"}</Badge>}
        {h.explained_by && <Badge kind="neutral">symptom of {h.explained_by.id}</Badge>}
      </summary>
      <div className="un-hyp-body stack">
        <p>{h.statement}{h.model_written && <span className="small muted"> (written by a model, checked against the evidence)</span>}</p>
        {h.groups?.length > 0 && <div><h4>What it needs</h4><ul className="un-groups">{h.groups.map((g: any) => (
          <li key={g.label} className={g.satisfied ? "met" : g.observed_absent ? "absent" : "unmet"}>
            <span aria-hidden="true">{g.satisfied ? "✓" : g.observed_absent ? "✕" : "?"}</span> {g.label}
            <span className="small muted"> {g.satisfied ? "observed" : g.observed_absent ? "checked and absent" : "not shown by any evidence"}</span>
            <Refs refs={g.satisfied ? g.evidence : g.absent_evidence} onCite={onCite} /></li>))}</ul></div>}
        <div className="small"><strong>Supporting evidence</strong> <Refs refs={h.supporting} onCite={onCite} /></div>
        {h.contradicting?.length > 0 && <div className="small"><strong>Against it</strong><ul>{h.contradicting.map((c: any, i: number) => <li key={i}>{c.detail} <Refs refs={[c.ref]} onCite={onCite} /></li>)}</ul></div>}
        {(r.unsupported_assumptions?.length > 0 || r.refuted?.length > 0 || (r.citations ?? []).some((c: any) => !c.valid || c.relevant === false)) && (
          <div className="small un-review"><strong>Independent review</strong> <span className="muted">({r.by === "moks" ? "by MOKS's rules" : "by MOKS's rules and the reviewer agent"})</span>
            <ul>
              {(r.refuted ?? []).map((x: string, i: number) => <li key={`r${i}`}>{x}</li>)}
              {(r.unsupported_assumptions ?? []).map((x: string, i: number) => <li key={`a${i}`}>{x[0].toUpperCase() + x.slice(1)}</li>)}
              {(r.citations ?? []).filter((c: any) => !c.valid || c.relevant === false).map((c: any) => <li key={c.ref}><span className="mono">{c.ref}</span>: {c.reason}</li>)}
              {(r.agent?.notes ?? []).map((x: string, i: number) => <li key={`n${i}`}><em>Reviewer agent:</em> {x}</li>)}
            </ul></div>)}
        {h.explained_by && <div className="small muted">Everything it rests on is also evidence for {h.explained_by.id} ({h.explained_by.title}), which explains it.</div>}
        <Factors score={h.score ?? {}} onCite={onCite} />
        {h.verification?.length > 0 && <div className="small"><strong>How to verify</strong><ol>{h.verification.map((v: string, i: number) => <li key={i}>{v}</li>)}</ol></div>}
      </div>
    </details>
  );
}

function Ledger({ items, onCite }: { items: any[]; onCite: Cite }) {
  const [type, setType] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const types = useMemo(() => [...new Set(items.map((i) => i.source_type))].sort(), [items]);
  const shown = type ? items.filter((i) => i.source_type === type) : items;
  return (
    <div className="stack tight">
      <label className="small">Source <select value={type} onChange={(e) => setType(e.target.value)}><option value="">all ({items.length})</option>
        {types.map((t) => <option key={t} value={t}>{t} ({items.filter((i) => i.source_type === t).length})</option>)}</select></label>
      <div className="un-table-wrap"><table className="un-ledger">
        <thead><tr><th>Id</th><th>Source</th><th>Resource</th><th>Status</th><th>Confidence</th><th>Stage</th></tr></thead>
        <tbody>{shown.map((i) => [
          <tr key={i.id} className="un-ledger-row">
            <td><button type="button" className="cite" onClick={() => onCite(i.id)} aria-label={`Open evidence ${i.id}`}>{i.id}</button></td>
            <td>{i.source_type}</td><td className="mono small un-uri">{i.resource?.uri}</td>
            <td><Badge kind={i.status === "ok" ? "ok" : i.status === "partial" ? "warn" : "bad"}>{i.status}</Badge></td>
            <td className="small">{i.confidence?.level} {Number(i.confidence?.score ?? 0).toFixed(2)}</td>
            <td className="small">{i.stage ?? "–"} <button type="button" className="ghost small" aria-expanded={open === i.id} onClick={() => setOpen(open === i.id ? null : i.id)}>{open === i.id ? "Hide" : "Details"}</button></td>
          </tr>,
          open === i.id && <tr key={`${i.id}-d`} className="un-ledger-detail"><td colSpan={6}>
            <dl className="kv small">
              <div className="kv-row"><dt>Observation</dt><dd>{i.observation}</dd></div>
              <div className="kv-row"><dt>Source</dt><dd className="mono">{i.source_ref?.tool}{i.source_ref?.connector_type ? ` · ${i.source_ref.connector_type}` : ""}</dd></div>
              {i.source_ref?.query && <div className="kv-row"><dt>Exact query</dt><dd className="mono">{Array.isArray(i.source_ref.query) ? i.source_ref.query.join("\n") : String(i.source_ref.query)}</dd></div>}
              <div className="kv-row"><dt>Service</dt><dd>{i.service ?? "–"}</dd></div>
              <div className="kv-row"><dt>Time range</dt><dd>{i.time_range?.start} → {i.time_range?.end} (collected {i.timestamp})</dd></div>
              <div className="kv-row"><dt>Confidence basis</dt><dd>{(i.confidence?.basis ?? []).join("; ")}</dd></div>
              <div className="kv-row"><dt>Access</dt><dd>{i.access?.decision} · {i.access?.scope} · {i.access?.permission} · {i.access?.realm}{i.access?.redacted ? " · redacted" : ""}{i.access?.truncated ? " · truncated" : ""}{i.access?.untrusted_content ? " · untrusted content" : ""}</dd></div>
              {i.signals?.length > 0 && <div className="kv-row"><dt>Signals</dt><dd className="mono">{i.signals.join(", ")}</dd></div>}
              <div className="kv-row"><dt>Stored row</dt><dd className="mono">{i.raw_ref?.api}</dd></div>
            </dl>
            <button type="button" className="small" onClick={() => onCite(i.id)}>Open the stored evidence</button>
          </td></tr>,
        ])}</tbody>
      </table></div>
    </div>
  );
}

export function UnifiedPanel({ unified: u, onCite }: { unified?: any; onCite: Cite }) {
  const [picked, setPicked] = useState<string | null>(null);
  if (!u) return null;
  const stage = picked ? (u.pipeline ?? []).find((s: any) => s.key === picked) : null;
  const lim = u.limitations ?? {};
  const cls = u.classification ?? {};
  return (
    <section className="un-panel stack" aria-label="Unified investigation">
      <div className="row between">
        <h3>Unified investigation</h3>
        <span className="row tight small">
          <Badge kind={u.status === "completed" ? "ok" : "warn"}>{u.status}</Badge>
          <Badge>{u.agents?.mode === "crewai" ? "CrewAI agents" : "rule-based reasoning"}</Badge>
          {u.budget && <span className="muted">{u.budget.tool_calls?.used}/{u.budget.tool_calls?.limit} tool calls · {u.budget.seconds?.used}s</span>}
        </span>
      </div>
      <PipelineStrip stages={u.pipeline ?? []} onPick={(k) => setPicked(picked === k ? null : k)} picked={picked} />
      {stage && <div className="un-stage-detail small"><strong>{stage.label}</strong>: {stage.status}{stage.reason ? ` (${stage.reason})` : ""}
        {stage.notes?.length > 0 && <ul>{stage.notes.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul>}
        {stage.evidence?.length > 0 && <div>Evidence <Refs refs={stage.evidence} onCite={onCite} /></div>}</div>}

      <div className="un-rca">
        <h4>Root-cause analysis</h4>
        <p>{u.executive_summary}</p>
        <div className="row tight small">
          {(cls.categories ?? []).map((c: string) => <Badge key={c}>{c.replace("_", " ")}</Badge>)}
          {cls.severity && <span className="muted" title={cls.note}>estimated severity <strong>{cls.severity}</strong> ({(cls.severity_basis ?? []).join("; ")})</span>}
          {u.target?.workload && <span className="muted">target {u.target.kind} <span className="mono">{u.target.workload}</span>{u.target.how && u.target.how !== "requested" ? ` (${u.target.how})` : ""}</span>}
        </div>
      </div>

      <details className="rs" open><summary>Root-cause hypotheses<span className="count">{(u.hypotheses ?? []).length}</span></summary>
        {(u.hypotheses ?? []).length ? <div className="stack tight">{u.hypotheses.map((h: any, i: number) => <Hypothesis key={h.id} h={h} onCite={onCite} open={i === 0} />)}</div>
          : <div className="small muted">No failure pattern matched the evidence, and no agent proposed one it could back.</div>}
      </details>

      {(u.timeline ?? []).length > 0 && <details className="rs" open><summary>Timeline<span className="count">{u.timeline.length}</span></summary>
        <ol className="un-timeline">{u.timeline.map((t: any, i: number) => (
          <li key={i} className={t.onset ? "onset" : ""}><span className="small muted mono">{new Date(t.at).toLocaleTimeString()}</span>
            <Badge kind={TIMELINE[t.kind] ?? "neutral"}>{t.onset ? "onset" : t.kind}</Badge> <span>{t.title}</span><Refs refs={t.evidence} onCite={onCite} /></li>))}</ol>
      </details>}

      <details className="rs" open><summary>Affected services and impact</summary>
        <ul>{(u.affected_services ?? []).map((a: any, i: number) => <li key={i}><strong>{a.service}</strong> <span className="small muted">{a.role}</span><Refs refs={a.evidence} onCite={onCite} /></li>)}</ul>
        {(u.impact?.technical ?? []).length > 0 && <div className="small"><strong>Technical</strong><ul>{u.impact.technical.map((t: any, i: number) => <li key={i}>{t.statement}<Refs refs={t.evidence} onCite={onCite} /></li>)}</ul></div>}
        {u.impact?.business && <div className="small"><strong>Business</strong> {u.impact.business.statement}<Refs refs={u.impact.business.evidence} onCite={onCite} /></div>}
      </details>

      {(u.observations ?? []).length > 0 && <details className="rs"><summary>Confirmed observations<span className="count">{u.observations.length}</span></summary>
        <ul>{u.observations.map((o: any, i: number) => <li key={i}>{o.statement}<Refs refs={o.evidence} onCite={onCite} /></li>)}</ul></details>}

      {(u.anomaly_correlations ?? []).length > 0 && <details className="rs" open><summary>Anomaly correlations<span className="count">{u.anomaly_correlations.length}</span></summary>
        <div className="un-table-wrap"><table><thead><tr><th>Service</th><th>Feature</th><th>Against the onset</th><th>Model</th><th /></tr></thead>
          <tbody>{u.anomaly_correlations.map((a: any, i: number) => <tr key={i}><td>{a.service}</td><td className="mono">{a.feature}</td>
            <td>{a.lag_minutes == null ? "no time" : `${a.relation} (${a.lag_minutes > 0 ? "+" : ""}${a.lag_minutes} min)`}</td>
            <td>{a.shadow ? <Badge kind="warn">shadow</Badge> : <Badge>enabled</Badge>}</td><td><Refs refs={a.evidence} onCite={onCite} /></td></tr>)}</tbody></table></div>
        <div className="small muted">{lim.ml}</div></details>}

      {((u.historical_references?.similar_incidents ?? []).length > 0 || (u.historical_references?.documents ?? []).length > 0 || (u.runbooks ?? []).length > 0) &&
        <details className="rs"><summary>Related incidents and runbooks</summary>
          <ul>
            {(u.historical_references?.similar_incidents ?? []).map((s: any, i: number) => <li key={`s${i}`}><Badge>past incident</Badge> {s.title}<Refs refs={s.evidence} onCite={onCite} /></li>)}
            {(u.historical_references?.documents ?? []).map((d: any, i: number) => <li key={`d${i}`}><Badge>postmortem</Badge> {d.title ?? d.path} <span className="mono small muted">{d.path}</span><Refs refs={d.evidence} onCite={onCite} /></li>)}
            {(u.runbooks ?? []).map((d: any, i: number) => <li key={`r${i}`}><Badge kind="info">runbook</Badge> {d.title ?? d.path} <span className="mono small muted">{d.path}</span><Refs refs={d.evidence} onCite={onCite} /></li>)}
          </ul>
          <div className="small muted">Leads to check against this investigation's live evidence, never proof of the current cause.</div></details>}

      {(u.verification_steps ?? []).length > 0 && <details className="rs"><summary>Recommended verification<span className="count">{u.verification_steps.length}</span></summary>
        <ol>{u.verification_steps.map((v: any, i: number) => <li key={i}>{v.step} <span className="small muted">({v.hypothesis})</span></li>)}</ol></details>}

      {(u.remediation ?? []).length > 0 && <details className="rs"><summary>Recommended remediation<span className="count">{u.remediation.length}</span></summary>
        <ul>{u.remediation.map((r: any, i: number) => <li key={i}>{r.action} <span className="small muted">for {r.hypothesis}</span>
          {r.action_kind && <> <Badge kind="info">{r.action_kind}</Badge></>}<div className="small muted">Risk: {r.risk}. Rollback: {r.rollback}. Needs an approved action plan; nothing runs from here.</div></li>)}</ul></details>}

      <details className="rs"><summary>Evidence ledger<span className="count">{(u.evidence_items ?? []).length}</span></summary>
        <Ledger items={u.evidence_items ?? []} onCite={onCite} /></details>

      <details className="rs" open={u.status !== "completed"}><summary>Confidence and limitations<span className="count">{(lim.gaps ?? []).length}</span></summary>
        <p className="small">{lim.scoring}</p>
        {(lim.gaps ?? []).length > 0 && <ul className="small">{lim.gaps.map((g: any, i: number) => <li key={i}><span className="mono">{g.source}</span> ({g.stage}): {g.reason}</li>)}</ul>}
        {(lim.stages_not_completed ?? []).length > 0 && <div className="small">Stages not completed: {lim.stages_not_completed.map((s: any) => `${s.stage} (${s.status}${s.reason ? `: ${s.reason}` : ""})`).join("; ")}</div>}
        {(lim.hypotheses_not_shown ?? []).length > 0 && <div className="small">Also considered: {lim.hypotheses_not_shown.map((h: any) => `${h.title} (${h.score}/100, ${h.why})`).join("; ")}</div>}
        {(lim.refused ?? []).length > 0 && <div className="small">Refused requests: {lim.refused.map((r: any) => `${r.probe} from the ${r.by} (${r.reason})`).join("; ")}</div>}
      </details>
    </section>
  );
}
