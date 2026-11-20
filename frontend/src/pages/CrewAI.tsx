import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox } from "../components";
import { KindOptions, NamespaceSelect, WorkloadSelect } from "../targets";

// The opt-in CrewAI workload investigation (docs/crewai-investigation.md). Shown only when
// /investigate/config reports it available, so with the flag off none of this renders.

export type CrewCapability = { enabled: boolean; available: boolean; reason: string | null; version: string | null; llm?: string;
  limits: { timeout_seconds: number; max_tool_calls: number; max_iterations_per_agent: number } };

export type UnifiedCapability = { flow: string; available: boolean; reason: string | null; stages: { key: string; label: string; description: string }[] };

export function CrewAIForm({ capability, unified, environments, minutes }: { capability: CrewCapability; unified?: UnifiedCapability; environments: string[]; minutes: string }) {
  const nav = useNavigate();
  const [pipeline, setPipeline] = useState<"workload" | "unified">(unified?.available ? "unified" : "workload");
  const isUnified = pipeline === "unified" && !!unified?.available;
  const [environment, setEnvironment] = useState(environments[0] ?? "");
  const [namespace, setNamespace] = useState("");
  const [workload, setWorkload] = useState("");
  const [kind, setKind] = useState("Deployment");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const inv = await api<{ conversation_id: string }>(isUnified ? "/investigations/unified" : "/investigations/crewai", {
        method: "POST", body: { environment: environment || undefined, namespace, workload: workload || undefined, kind, minutes: minutes ? Number(minutes) : undefined } });
      nav(`/conversations/${inv.conversation_id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={submit} aria-label="CrewAI investigation">
      {unified?.available && <div className="row small" role="radiogroup" aria-label="CrewAI pipeline">
        <label className="inline"><input type="radio" name="pipeline" checked={isUnified} onChange={() => setPipeline("unified")} /> Unified investigation</label>
        <label className="inline"><input type="radio" name="pipeline" checked={!isUnified} onChange={() => setPipeline("workload")} /> Workload crew</label>
      </div>}
      <div className="small muted">
        {isUnified
          ? <>{unified!.stages.length} stages over Kubernetes, metrics, logs, traces, deployments, Git, ML anomalies, past incidents and runbooks. An analyst agent proposes hypotheses, a reviewer challenges them, and MOKS scores each by stated factors, using {capability.llm}. The workload is optional: MOKS finds the unhealthy one.</>
          : <>Three agents (workload, changes, review) investigate one workload with read-only tools, using {capability.llm}. Every finding cites recorded evidence.</>}
        {" "}Limits: {capability.limits.timeout_seconds}s, {capability.limits.max_tool_calls} tool calls, {capability.limits.max_iterations_per_agent} iterations per agent.
      </div>
      <div className="filters">
        <label>Environment<select value={environment} onChange={(e) => { setEnvironment(e.target.value); setNamespace(""); setWorkload(""); }}>{environments.map((n) => <option key={n}>{n}</option>)}</select></label>
        <label htmlFor="crew-ns">Namespace<NamespaceSelect id="crew-ns" environment={environment} value={namespace} onChange={(v) => { setNamespace(v); setWorkload(""); }} /></label>
        <label>Kind<select value={kind} onChange={(e) => { setKind(e.target.value); setWorkload(""); }}><KindOptions environment={environment} namespace={namespace} /></select></label>
        <label htmlFor="crew-wl" style={{ flex: 1 }}>Workload<WorkloadSelect id="crew-wl" environment={environment} namespace={namespace} kind={kind} value={workload} onChange={setWorkload} /></label>
        <button disabled={busy || !namespace || (!workload && !isUnified)}>{busy ? "Starting…" : isUnified ? "Run unified investigation" : "Run CrewAI investigation"}</button>
      </div>
      <ErrorBox error={error} />
    </form>
  );
}

export function CrewAIPanel({ crew, onCite }: { crew?: any; onCite: (ref: string) => void }) {
  if (!crew) return null;
  const v = crew.validation ?? {};
  return (
    <div className="rs static" aria-label="CrewAI run">
      <h3>CrewAI run <Badge kind={crew.status === "completed" ? "ok" : "warn"}>{crew.status}</Badge> <span className="small muted">crewai {crew.version} · {crew.tool_calls} tool calls</span></h3>
      <div className="row small">{(crew.agents ?? []).map((a: any) => <Badge key={a.agent} kind={a.status === "done" ? "ok" : "warn"}>{a.label}: {a.status}</Badge>)}</div>
      {crew.stopped && <div className="alert warn small">Stopped early: {crew.stopped}. The report covers the evidence collected before that.</div>}
      {(crew.gaps ?? []).length > 0 && <div className="small"><strong>Gaps</strong><ul>{crew.gaps.map((g: any, i: number) => <li key={i}><span className="mono">{g.source}</span>: {g.reason}</li>)}</ul></div>}
      {((v.citations_rejected ?? []).length > 0 || (v.claims_removed ?? []).length > 0 || (v.confidence_lowered ?? []).length > 0) && (
        <div className="small muted">
          Checked by MOKS: {(v.citations_rejected ?? []).length} invented citation(s) rejected{(v.citations_rejected ?? []).length ? ` (${v.citations_rejected.join(", ")})` : ""},{" "}
          {(v.claims_removed ?? []).length} uncited claim(s) removed, {(v.confidence_lowered ?? []).length} confidence level(s) lowered.
        </div>)}
      {(crew.evidence ?? []).length > 0 && <details><summary className="small">Cited evidence excerpts ({crew.evidence.length})</summary>
        <ul className="small">{crew.evidence.map((e: any) => <li key={e.id}><button type="button" className="cite" style={{ marginLeft: 0 }} onClick={() => onCite(e.id)}>{e.id}</button>{" "}
          <span className="mono">{e.source}</span> {e.truncated && <Badge kind="warn">truncated</Badge>}<pre className="mono small">{e.excerpt}</pre></li>)}</ul>
        <div className="small muted">Excerpts are cut from the stored evidence, not written by the model.</div>
      </details>}
    </div>
  );
}
