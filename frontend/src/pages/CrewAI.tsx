import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Badge, ErrorBox } from "../components";
import { KindOptions, NamespaceSelect, WorkloadSelect } from "../targets";
import { IncidentReport } from "../crewai/IncidentReport";

// The opt-in CrewAI investigations: the workload flow (docs/crewai-investigation.md) and the incident flow
// (docs/crewai-sre-engine.md). Shown only when /investigate/config reports one available, so with the flag off none of this renders.

type FlowInfo = { label: string; description: string; enabled: boolean; available: boolean; reason: string | null; agents: number; limits: Record<string, number>;
  inputs: { name: string; default?: string; choices?: string[] }[] };
export type CrewCapability = { enabled: boolean; available: boolean; available_any?: boolean; reason: string | null; version: string | null; llm?: string;
  limits: { timeout_seconds: number; max_tool_calls: number; max_iterations_per_agent: number }; flows?: Record<string, FlowInfo> };

const INCIDENT = "incident_investigation";

export function CrewAIForm({ capability, environments, minutes }: { capability: CrewCapability; environments: string[]; minutes: string }) {
  const nav = useNavigate();
  const incidentFlow = capability.flows?.[INCIDENT]?.available ? capability.flows[INCIDENT] : null;
  const [flow, setFlow] = useState(capability.available ? "workload_investigation" : INCIDENT);
  const [environment, setEnvironment] = useState(environments[0] ?? "");
  const [namespace, setNamespace] = useState("");
  const [workload, setWorkload] = useState("");
  const [kind, setKind] = useState("Deployment");
  const windows = incidentFlow?.inputs.find((i) => i.name === "change_window");
  const [changeWindow, setChangeWindow] = useState(windows?.default ?? "24h");
  const [incident, setIncident] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const isIncident = flow === INCIDENT;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const base = { environment: environment || undefined, namespace, workload, kind, minutes: minutes ? Number(minutes) : undefined };
    try {
      const inv = await api<{ conversation_id: string }>("/investigations/crewai", {
        method: "POST", body: isIncident ? { flow: INCIDENT, ...base, change_window: changeWindow, question: incident.trim() || undefined } : base });
      nav(`/conversations/${inv.conversation_id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  const lim = incidentFlow?.limits ?? {};
  return (
    <form className="card stack" onSubmit={submit} aria-label="CrewAI investigation">
      {capability.available && incidentFlow && <div className="row small" role="radiogroup" aria-label="Crew">
        <label className="inline"><input type="radio" name="crew" checked={!isIncident} onChange={() => setFlow("workload_investigation")} /> Workload investigation (3 agents)</label>
        <label className="inline"><input type="radio" name="crew" checked={isIncident} onChange={() => setFlow(INCIDENT)} /> Incident investigation ({incidentFlow.agents} agents)</label>
      </div>}
      {isIncident ? <div className="small muted">
        An incident commander plans, Kubernetes, observability, change and history specialists collect evidence with read-only tools, an independent reviewer
        challenges every finding and hypothesis, and a root-cause analyst writes the RCA, using {capability.llm}. MOKS grades every finding and hypothesis from the evidence.
        Limits: {lim.timeout_seconds}s, {lim.max_tool_calls} tool calls, {lim.review_rounds} review rounds, {lim.token_budget} tokens.
      </div> : <div className="small muted">
        Three agents (workload, changes, review) investigate one workload with read-only tools, using {capability.llm}. Every finding cites recorded evidence.
        Limits: {capability.limits.timeout_seconds}s, {capability.limits.max_tool_calls} tool calls, {capability.limits.max_iterations_per_agent} iterations per agent.
      </div>}
      <div className="filters">
        <label>Environment<select value={environment} onChange={(e) => { setEnvironment(e.target.value); setNamespace(""); setWorkload(""); }}>{environments.map((n) => <option key={n}>{n}</option>)}</select></label>
        <label htmlFor="crew-ns">Namespace<NamespaceSelect id="crew-ns" environment={environment} value={namespace} onChange={(v) => { setNamespace(v); setWorkload(""); }} /></label>
        <label>Kind<select value={kind} onChange={(e) => { setKind(e.target.value); setWorkload(""); }}><KindOptions environment={environment} namespace={namespace} /></select></label>
        <label htmlFor="crew-wl" style={{ flex: 1 }}>Workload<WorkloadSelect id="crew-wl" environment={environment} namespace={namespace} kind={kind} value={workload} onChange={setWorkload} /></label>
        {isIncident && <label>Change window<select value={changeWindow} onChange={(e) => setChangeWindow(e.target.value)}>
          {(windows?.choices ?? ["15m", "1h", "6h", "24h"]).map((w) => <option key={w}>{w}</option>)}</select></label>}
        <button disabled={busy || !namespace || !workload}>{busy ? "Starting…" : isIncident ? "Run incident investigation" : "Run CrewAI investigation"}</button>
      </div>
      {isIncident && <textarea aria-label="Incident" rows={2} maxLength={2000} value={incident} onChange={(e) => setIncident(e.target.value)}
        placeholder="Optional: the symptoms, when they started, who noticed" />}
      <ErrorBox error={error} />
    </form>
  );
}

export function CrewAIPanel({ crew, onCite }: { crew?: any; onCite: (ref: string) => void }) {
  if (!crew) return null;
  if (crew.status === "fallback") return <div className="alert warn small" aria-label="CrewAI run">The CrewAI incident investigation could not start ({crew.stopped}).
    This is MOKS's standard investigation instead.</div>;
  if (crew.flow === INCIDENT && crew.investigation) return <IncidentReport crew={crew} onCite={onCite} />;
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
