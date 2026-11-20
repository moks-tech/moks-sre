import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, Loading, Time, useLoad, type Col } from "../components";
import { Drawer, Empty, useFeedback } from "../ui";
import { SimilarIncidents } from "./MLParts";

export const FEATURE: Record<string, string> = {
  request_rate: "request rate", error_ratio: "error ratio", latency_p50: "p50 latency", latency_p95: "p95 latency", latency_p99: "p99 latency",
  cpu_usage: "CPU used", cpu_utilization: "CPU use of limit", cpu_throttling: "CPU throttling", memory_working_set: "memory working set",
  memory_utilization: "memory use of limit", restart_rate: "restarts per hour", network_errors: "network errors", queue_depth: "queue depth",
  db_connections: "database connections", node_cpu_utilization: "node CPU", node_memory_utilization: "node memory", node_pressure: "node pressure",
};
export const feature = (f: string) => FEATURE[f] ?? f.replace(/_/g, " ");

const LINK: Record<string, string> = {
  calls: "one calls the other", calls_via: "two calls apart", shared_dependency: "call the same service", node: "runs on the node", same_node: "share a node",
  rollout: "a rollout touched both", same_namespace: "same namespace", same_cohort: "same workload",
};
const HYPOTHESIS: Record<string, string> = { node: "node", rollout: "rollout", dependency: "dependency", shared_dependency: "shared dependency" };

const member = (m: any) => (m.namespace === "_node" ? `node/${m.workload}` : `${m.namespace}/${m.workload}`);

function GroupDrawer({ id, onClose, onSaved }: { id: string; onClose: () => void; onSaved: () => void }) {
  const { can } = useAuth();
  const { toast } = useFeedback();
  const g = useLoad(() => api<any>(`/ml/correlations/${id}`), [id]);
  const send = async (verdict: string) => {
    try {
      await api(`/ml/correlations/${id}/feedback`, { method: "POST", body: { verdict } });
      toast("Recorded for review.");
      g.reload();
      onSaved();
    } catch (e: any) {
      toast(e?.message ?? "Could not record that", "bad");
    }
  };
  const d = g.data;
  return <Drawer title="Correlated problem" heading={d ? <span className="row tight">{d.members.length} anomalies<Badge kind={d.status === "open" ? "warn" : "neutral"}>{d.status}</Badge></span> : "Correlated problem"}
    onClose={onClose}
    footer={d && can("ml.feedback") ? <div className="row wrap" role="group" aria-label="Do these belong together?">
      {[["correct", "Belong together"], ["partly", "Partly"], ["wrong_grouping", "Wrong grouping"], ["unsure", "Unsure"]].map(([v, l]) =>
        <button key={v} type="button" className={`ghost${d.feedback?.verdict === v ? " selected" : ""}`} aria-pressed={d.feedback?.verdict === v} onClick={() => send(v)}>{l}</button>)}
    </div> : undefined}>
    {!d ? <Loading load={g} /> : <div className="stack">
      <div className="alert info small">{d.note}</div>
      <div className="small">First flagged <Time iso={d.first_seen} />, last <Time iso={d.last_seen} /> · {d.environment} · {d.namespaces.join(", ")}</div>
      <h3>Anomalies</h3>
      <ul className="small" aria-label="Anomalies in this group">
        {d.members.map((m: any) => <li key={m.cohort}><span className="mono">{member(m)}</span>: {m.features.map(feature).join(", ") || "several signals"}, from <Time iso={m.first} />
          {" "}<span className="muted">({m.algorithm.replace(/_/g, " ")} v{m.model_version})</span></li>)}
      </ul>
      <h3>Candidate explanations</h3>
      {d.hypotheses.length ? <ol className="small" aria-label="Candidate explanations">
        {d.hypotheses.map((h: any) => <li key={h.rank}><Badge kind="neutral">{HYPOTHESIS[h.kind] ?? h.kind}</Badge> {h.statement} <Badge kind="info">unvalidated</Badge></li>)}
      </ol> : <div className="small muted">None: the anomalies are linked, but nothing in the context explains them.</div>}
      <h3>Why these are grouped</h3>
      <ul className="small" aria-label="Links">
        {d.links.map((l: any, i: number) => <li key={i}><span className="mono">{l.a[0].split("/").slice(1).join("/")}</span> and <span className="mono">{l.b[0].split("/").slice(1).join("/")}</span>:{" "}
          {l.reasons.filter((r: any) => r.type !== "time").map((r: any) => LINK[r.type] ?? r.type).join(", ")} (weight {l.weight})</li>)}
        {!d.links.length && <li>One anomaly on its own.</li>}
      </ul>
      {(d.context?.alerts?.length > 0 || d.context?.events?.length > 0 || Object.keys(d.context?.rollouts ?? {}).length > 0) && <>
        <h3>Context</h3>
        <ul className="small">
          {Object.entries(d.context.rollouts ?? {}).map(([w, ts]: any) => <li key={w}>Rollouts of <span className="mono">{w}</span>: {ts.length} recent, last <Time iso={ts[ts.length - 1]} /></li>)}
          {d.context.alerts?.length > 0 && <li>{d.context.alerts.length} alert event(s): {[...new Set(d.context.alerts.map((a: any) => a.alertname))].join(", ")}</li>}
          {d.context.events?.length > 0 && <li>{d.context.events.length} warning event(s): {[...new Set(d.context.events.map((e: any) => e.reason))].join(", ")}</li>}
          {Object.entries(d.context.logs ?? {}).map(([w, l]: any) => <li key={w}>{l.error_lines} error log line(s) from <span className="mono">{w}</span></li>)}
        </ul>
      </>}
      {d.context?.gaps?.length > 0 && <div className="alert warn small">Not read: {d.context.gaps.map((x: any) => `${x.source} (${x.reason})`).join("; ")}. Links from those sources may be missing.</div>}
      {d.similar_incidents && <SimilarIncidents s={d.similar_incidents} />}
      {d.history?.what_worked?.length > 0 && <>
        <h3>What worked before here</h3>
        <ul className="small">{d.history.what_worked.map((w: any, i: number) => <li key={i}>{w.resolution} <span className="muted">({w.times}×, cause verified)</span></li>)}</ul>
      </>}
    </div>}
  </Drawer>;
}

/** Anomalies grouped as one probable problem each: what to investigate, instead of one alert per metric. */
export function CorrelatedProblems({ env, ns }: { env: string; ns: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const groups = useLoad(() => api<any>(`/ml/correlations${qs({ environment: env === "all" ? undefined : env })}`), [env]);
  const rows = (groups.data?.groups ?? []).filter((g: any) => ns === "all" || g.namespaces.includes(ns));
  const cols: Col<any>[] = [
    { key: "what", head: "Affected", primary: true, cell: (g) => <span className="mono">{g.members.slice(0, 3).map(member).join(", ")}{g.members.length > 3 ? ` +${g.members.length - 3}` : ""}</span> },
    { key: "status", head: "Status", status: true, cell: (g) => <Badge kind={g.status === "open" ? "warn" : "neutral"}>{g.status}</Badge> },
    { key: "n", head: "Anomalies", cell: (g) => g.members.length },
    { key: "lead", head: "Leading candidate (unvalidated)", cell: (g) => g.hypotheses[0]?.statement ?? "" },
    { key: "first", head: "First flagged", cell: (g) => <Time iso={g.first_seen} /> },
    { key: "fb", head: "Feedback", cell: (g) => g.feedback?.verdict?.replace(/_/g, " ") ?? "" },
  ];
  return <>
    <h2>Correlated problems</h2>
    {groups.data ? <DataTable label="Correlated problems" rows={rows} cols={cols} rowKey={(g) => g.id} onRow={(g) => setOpen(g.id)}
      empty={<Empty title="No correlated problems in the last 48 hours.">Anomalies that share a dependency, a node or a rollout and happen together are grouped here.</Empty>} />
      : <Loading load={groups} />}
    {open && <GroupDrawer id={open} onClose={() => setOpen(null)} onSaved={groups.reload} />}
  </>;
}

/** What keeps failing, from reviewed incidents the reader may read. */
export function RecurringProblems({ env, ns }: { env: string; ns: string }) {
  const h = useLoad(() => api<any>(`/ml/history${qs({ environment: env === "all" ? undefined : env, namespace: ns === "all" ? undefined : ns })}`), [env, ns]);
  if (h.error) return null;  // history is a side panel: when it cannot be read, the page goes on without it
  if (!h.data) return <Loading load={h} />;
  const d = h.data;
  if (typeof d.records !== "number") return null;
  return <section className="card tight stack" aria-label="Recurring problems">
    <h2>Recurring problems</h2>
    <div className="small muted">{d.note} {d.records} reviewed incident(s) in {d.scope?.days ?? 180} days.</div>
    {d.records === 0 ? <div className="small">{d.reason ?? "Nothing yet."} Incidents count here once their postmortem is approved.</div> : <div className="grid cols-3">
      <div><h3>Recurring</h3><ul className="small">{(d.recurring ?? []).map((r: any, i: number) => <li key={i}>
        {r.occurrences}× {(r.pattern.symptoms.join(", ") || r.pattern.signature.slice(0, 3).join(" ")) || "similar signature"} on {Object.keys(r.services).join(", ")}
        {r.causes[0] && <span className="muted"> · usually: {r.causes[0].cause}</span>}
        {r.examples[0]?.url && <> · <Link to={`/incidents/${r.examples[0].incident}`}>latest</Link></>}</li>)}
        {!d.recurring?.length && <li className="muted">No pattern seen twice.</li>}</ul></div>
      <div><h3>Most affected</h3><ul className="small">{(d.services ?? []).slice(0, 6).map((s: any) => <li key={s.service}><span className="mono">{s.service}</span>: {s.incidents}
        {s.median_hours_to_resolve != null && <span className="muted"> · median {s.median_hours_to_resolve} h to resolve</span>}</li>)}</ul></div>
      <div><h3>What worked</h3><ul className="small">{(d.what_worked ?? []).map((w: any, i: number) => <li key={i}>{w.resolution} <span className="muted">({w.times}×)</span></li>)}
        {(d.runbooks ?? []).slice(0, 3).map((r: any, i: number) => <li key={`r${i}`}>{r.kind === "skill" ? `Skill ${r.skill} (helpful ${r.helpful}×)` : r.kind === "cited_document" ? `${r.title} (cited ${r.used}×)` : `Runbook: ${r.runbook}`}</li>)}
        {!d.what_worked?.length && !d.runbooks?.length && <li className="muted">No verified resolution yet.</li>}</ul></div>
    </div>}
    {d.infrastructure?.length > 0 && <div className="small">Repeated infrastructure problems: {d.infrastructure.map((x: any) => `${x.problem} ${x.occurrences}×`).join(", ")}.</div>}
  </section>;
}

/** A model's health in shadow, with what to review. Recommendations only. */
export function ModelHealth({ id, onClose }: { id: string; onClose: () => void }) {
  const h = useLoad(() => api<any>(`/ml/models/${id}/health`), [id]);
  const d = h.data;
  return <Drawer title="Model health" heading={d ? <span className="row tight"><span className="mono">{d.model.cohort}</span><Badge kind={d.healthy ? "ok" : "warn"}>{d.healthy ? "healthy" : "review"}</Badge></span> : "Model health"} onClose={onClose}>
    {!d ? <Loading load={h} /> : <div className="stack small">
      <div>{d.model.algorithm.replace(/_/g, " ")} v{d.model.version} · {d.model.status} · last {d.days} days: {d.observations} observation(s), {d.flags_per_day ?? "no"} alarm(s) per day (gate {d.flag_gate})</div>
      <div>Validation: {d.validation.status.replace(/_/g, " ")}{d.validation.judged_precision != null ? `, judged precision ${d.validation.judged_precision} on ${d.validation.reviewed_flags} reviewed flags` : ""}
        {d.validation.synthetic_recall != null ? `, recall on injected faults ${d.validation.synthetic_recall}` : ""}</div>
      {Object.keys(d.drift_psi).length > 0 && <div>Input drift (median PSI): {Object.entries(d.drift_psi).map(([f, v]) => `${feature(f)} ${v}`).join(", ")}</div>}
      {d.recommendations.length ? <ul aria-label="Recommendations">{d.recommendations.map((r: any) => <li key={r.reason}><strong>{r.action}</strong>: {r.detail}</li>)}</ul> : <div>Nothing to review.</div>}
      <div className="muted">{d.note}</div>
    </div>}
  </Drawer>;
}
