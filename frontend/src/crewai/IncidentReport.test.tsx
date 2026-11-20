import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CrewProgress, IncidentReport } from "./IncidentReport";
import { CrewAIPanel } from "../pages/CrewAI";

const criteria = (met: string[]) => ["live_support", "independent_signals", "reviewer_upheld"].map((name) => ({ name, met: met.includes(name), weight: 0.2, detail: `${name} detail` }));
const crew = (over: any = {}) => ({
  version: "1.15.41", flow: "incident_investigation", status: "completed", tool_calls: 9, stopped: null, fallback: null,
  validation: { citations_rejected: ["E99"] }, errors: [{ code: "task_timeout", task: "observability", message: "the step's time limit of 120s was reached" }],
  usage: { total: { tokens: 4200 }, token_budget: 150000, budget_reached: false, by_agent: { commander: { model_calls: 3, tool_calls: 2, tokens_in: 900, tokens_out: 300, cost_usd: "0.01", retries: 1 } } },
  investigation: {
    severity: { level: "SEV2", criteria: ["Deployment/payments: 0 of 1 replicas ready", "not a production environment"], proposed: "SEV1" },
    root_cause: { hypothesis: "H1", statement: "QUEUE_URL is not set", status: "strongly_supported", confidence: 0.85 },
    impact: "payments has no ready replica", symptom_onset: "2026-11-20T10:02:00Z", change_window: "6h",
    hypotheses: [{ id: "H1", rank: 1, statement: "QUEUE_URL is not set", status: "strongly_supported", confidence: 0.85, supporting: ["E2", "E3"], contradicting: [],
                   criteria: criteria(["live_support", "independent_signals", "reviewer_upheld"]), review: { verdict: "supported", reason: "logs agree" }, would_confirm: "env vars" },
                 { id: "H2", rank: 2, statement: "The node is out of memory", status: "insufficient_evidence", confidence: 0.3, supporting: ["E2"], contradicting: ["E4"],
                   criteria: criteria(["live_support"]), review: { verdict: "unsupported", reason: "exit 1" } }],
    findings: [{ id: "F1", observation: "payments-7d9-a1 is in CrashLoopBackOff", task: "kubernetes", resource: "pod/payments-7d9-a1", resource_verified: true, evidence_source: ["k8s_describe_pod"],
                 citations: ["E2"], timestamp: "2026-11-20T10:20:00Z", event_time: null, time_range: { from: null, to: null, point_in_time: true }, confidence: 1, status: "confirmed_observation", hypotheses: ["H1"] }],
    changes: [{ id: "C1", kind: "rollout", resource: "Deployment/payments", at: "2026-11-20T10:00:00Z", minutes_before: 25, window: "1h", detail: "revision 7", evidence: ["E5"], before_symptoms: true }],
    timeline: [{ at: "2026-11-20T10:00:00Z", event: "rollout: Deployment/payments", evidence: ["E5"], source: "evidence" }],
    review: { rounds: 2, conflicts: [{ ids: ["F1", "H2"], description: "they disagree" }], missing_telemetry: ["no metrics"] },
    requests: [{ round: 1, tool: "scale_deployment", args: {}, status: "refused", why: "not a tool MOKS runs" }],
    unsupported: [{ statement: "An invented observation", task: "kubernetes", reason: "cites no recorded evidence" }],
    tasks: [{ task: "plan", round: 1, label: "Incident commander: Plan the investigation", status: "done", seconds: 3.1, model_calls: 3, tool_calls: 2, attempts: 1 },
            { task: "observability", round: 1, label: "Observability investigator: Metrics, logs and traces", status: "partial", ended_by: "time limit", seconds: 120, model_calls: 4, tool_calls: 3, attempts: 1 }],
  }, ...over });

describe("the incident investigation's result", () => {
  afterEach(cleanup);

  it("shows what MOKS graded: severity, root cause, hypotheses, findings, changes and what it set aside", () => {
    const onCite = vi.fn();
    render(<MemoryRouter><IncidentReport crew={crew()} onCite={onCite} /></MemoryRouter>);
    expect(screen.getByText("SEV2")).toBeTruthy();
    expect(screen.getByText(/The incident commander proposed SEV1/)).toBeTruthy();
    expect(screen.getByText(/Strongly supported hypothesis \(H1\):/)).toBeTruthy();
    expect(screen.getByText(/MOKS never confirms a root cause/)).toBeTruthy();
    expect(screen.getAllByText("Insufficient evidence").length).toBeGreaterThan(0);
    expect(screen.getByText("Confirmed observation")).toBeTruthy();
    expect(screen.getByText(/named in the evidence/)).toBeTruthy();
    expect(screen.getByText("Within 1h")).toBeTruthy();
    expect(screen.getByText("before the symptoms")).toBeTruthy();
    expect(screen.getByText(/An invented observation/)).toBeTruthy();
    expect(screen.getByText("E99")).toBeTruthy();
    expect(screen.getByText(/F1 vs H2/)).toBeTruthy();
    expect(screen.getByText(/1 retried/)).toBeTruthy();
    expect(screen.getByText(/task_timeout/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "E5" })[0]);
    expect(onCite).toHaveBeenCalledWith("E5");
  });

  it("says when no root cause is established", () => {
    const c = crew();
    c.investigation.root_cause = null;
    c.investigation.hypotheses[0].status = "possible_explanation";
    render(<MemoryRouter><IncidentReport crew={c} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.getByText("No root cause is established.")).toBeTruthy();
    expect(screen.getByText(/most likely explanation is H1/)).toBeTruthy();
  });

  it("explains a run that fell back to the standard investigation", () => {
    render(<MemoryRouter><CrewAIPanel crew={{ status: "fallback", fallback: "langgraph", stopped: "the flow is switched off" }} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.getByText(/could not start \(the flow is switched off\)/)).toBeTruthy();
  });
});

describe("the incident flow's live progress", () => {
  afterEach(cleanup);
  const ev = (seq: number, type: string, crewai: any, label?: string) => ({ seq, type, at: "2026-11-20T10:00:00Z", label, crewai, message: "m" });

  it("marks each step as it starts and finishes", () => {
    render(<CrewProgress events={[ev(1, "phase", { task: "plan", round: 1 }, "CrewAI: Incident commander: Plan the investigation"),
      ev(2, "note", { task: "plan", round: 1, status: "done", seconds: 3, tool_calls: 2 }),
      ev(3, "phase", { task: "kubernetes", round: 1 }, "CrewAI: Kubernetes investigator: Kubernetes evidence")]} />);
    expect(screen.getByText("Incident commander: Plan the investigation")).toBeTruthy();
    expect(screen.getByText("3s · 2 tool calls")).toBeTruthy();
    expect(screen.getByText("Kubernetes investigator: Kubernetes evidence").closest(".cw-status")?.className).toContain("live");
  });

  it("renders nothing for other investigations", () => {
    const { container } = render(<CrewProgress events={[{ seq: 1, type: "phase", at: "x", label: "Triage" }]} />);
    expect(container.innerHTML).toBe("");
  });
});
