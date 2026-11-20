import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Report } from "../pages/Investigate";
import { LivePipeline, UnifiedPanel } from "./UnifiedPanel";

const stages = ["detect", "classify", "identify", "telemetry", "traces", "changes", "anomalies", "history", "semantic", "hypotheses", "more_evidence", "validate", "rca", "remediation", "report"]
  .map((key, i) => ({ key, label: `Stage ${key}`, status: i === 8 ? "partial" : i === 10 ? "skipped" : "completed", reason: i === 10 ? "nothing to probe" : null,
    notes: i === 8 ? ["semantic search ran as keyword"] : [], evidence: i === 3 ? ["E7", "E8"] : [], tool_calls: 0, duration_ms: 3 }));

const factor = (name: string, value: number, weight: number, explanation: string, evidence: string[] = []) => ({ name, value, weight, contribution: value * weight, explanation, evidence });

const unified = {
  engine: "unified", version: "1", status: "partial", pipeline: stages, budget: { tool_calls: { used: 17, limit: 24 }, seconds: { used: 2.1, limit: 120 } },
  agents: { mode: "rules" }, target: { kind: "Deployment", workload: "payments", how: "requested" },
  classification: { categories: ["latency", "data_store"], severity: "high", severity_basis: ["user-facing symptoms in production"], note: "an estimate" },
  executive_summary: "Deployment payments in shop: the leading hypothesis is database connection pool exhaustion (strong, 81/100).",
  hypotheses: [
    { id: "H1", rank: 1, pattern: "connection_pool_exhaustion", title: "Database connection pool exhaustion", statement: "payments cannot get connections", origin: "rules",
      supporting: ["E7", "E8"], contradicting: [], missing: [], verification: ["Compare the pool size with the previous revision."],
      groups: [{ label: "a user-facing symptom", satisfied: true, observed_absent: false, evidence: ["E7"], absent_evidence: [] },
               { label: "connection saturation", satisfied: true, observed_absent: false, evidence: ["E8"], absent_evidence: [] }],
      review: { verdict: "supported", by: "moks", citations: [], unsupported_assumptions: [], refuted: [] },
      score: { score: 81, band: "strong", not_a_probability: true, caps: [],
        factors: [factor("evidence_strength", 0.9, 0.35, "2 of 2 required groups", ["E7", "E8"]), factor("temporal_correlation", 1, 0.2, "revision 8 (E3) is within 5 minutes of the onset", ["E3"])],
        penalties: [{ name: "contradictions", value: 0, explanation: "nothing speaks against it", evidence: [] }] } },
    { id: "H3", rank: 2, pattern: "cpu_saturation", title: "CPU saturation or throttling", statement: "payments is CPU-bound", origin: "rules", supporting: ["E6"],
      contradicting: [{ ref: "E6", detail: "CPU throttling stayed below 5%" }], missing: [], verification: [], explained_by: { id: "H1", title: "Database connection pool exhaustion", refs: ["E7"] },
      groups: [{ label: "CPU throttling", satisfied: false, observed_absent: true, evidence: [], absent_evidence: ["E6"] }],
      review: { verdict: "contradicted", by: "moks", refuted: ["CPU throttling was checked and is absent (E6)"], unsupported_assumptions: [], citations: [] },
      score: { score: 32, band: "weak", factors: [], penalties: [{ name: "contradictions", value: 0.21, explanation: "E6: CPU throttling stayed below 5%", evidence: ["E6"] }], caps: [] } },
  ],
  timeline: [{ at: "2026-11-20T07:47:16Z", kind: "change", title: "revision 8 rolled out", evidence: ["E3"], onset: false },
             { at: "2026-11-20T07:50:16Z", kind: "symptom", title: "p95 latency rose", evidence: ["E7"], onset: true }],
  affected_services: [{ service: "payments", role: "primary", evidence: ["E7"] }],
  impact: { technical: [{ statement: "p95 latency rose from 0.18s to 1.56s", evidence: ["E7"] }], business: { statement: "payments serves shop.example.com/pay through an Ingress", evidence: ["E2"] } },
  observations: [{ statement: "p95 latency rose", evidence: ["E7"] }],
  anomaly_correlations: [{ service: "payments", feature: "latency_p95", lag_minutes: 9, relation: "coincides", shadow: true, evidence: ["E14"] }],
  historical_references: { similar_incidents: [{ title: "Connection pool exhaustion after a release", evidence: ["E15"] }], documents: [] },
  runbooks: [{ title: "Connection pool exhaustion", path: "runbooks/postgres-connection-pool.md", evidence: ["E17"] }],
  verification_steps: [{ step: "Compare the pool size with the previous revision.", hypothesis: "H1" }],
  remediation: [{ action: "Roll back to the previous revision", hypothesis: "H1", action_kind: "rollback_deployment", risk: "low", rollback: "roll forward", requires_approval: true, executable: false }],
  evidence_items: [
    { id: "E7", source_type: "metrics", status: "ok", stage: "telemetry", observation: "golden signals for payments", resource: { uri: "k8s://prod/shop/Deployment/payments" },
      source_ref: { tool: "prom_golden_signals", connector_type: "prometheus", query: ["histogram_quantile(0.95, …)"] }, service: "payments", timestamp: "2026-11-20T08:00:00Z",
      time_range: { start: "2026-11-20T07:00:00Z", end: "2026-11-20T08:00:00Z" }, confidence: { level: "high", score: 0.85, basis: ["source metrics: 0.85"] },
      access: { decision: "allow", scope: "namespace:shop", permission: "investigation.run", realm: "production", redacted: true, untrusted_content: false },
      signals: ["metric.latency_increase"], raw_ref: { api: "/investigations/i/evidence/E7" } },
    { id: "E8", source_type: "logs", status: "ok", stage: "telemetry", observation: "LogQL returned 4 lines", resource: { uri: "k8s://prod/shop/Deployment/payments" }, source_ref: { tool: "loki_search" },
      confidence: { level: "medium", score: 0.75, basis: [] }, access: { decision: "allow", scope: "namespace:shop", untrusted_content: true }, signals: [], raw_ref: { api: "/x" } },
  ],
  limitations: { scoring: "Scores rank hypotheses ... not probabilities.", ml: "Anomaly-model observations are leads.", gaps: [{ stage: "semantic", source: "embeddings", reason: "knowledge search ran as keyword" }],
    stages_not_completed: [{ stage: "semantic", status: "partial", reason: null }], hypotheses_not_shown: [{ title: "Postgres failover", score: 10, why: "nothing in the evidence supports it" }], refused: [] },
};

describe("the unified investigation view", () => {
  afterEach(cleanup);

  it("shows every stage, the RCA and the ranked hypotheses with their scores", () => {
    render(<UnifiedPanel unified={unified} onCite={vi.fn()} />);
    expect(screen.getAllByRole("button", { name: /^Stage \d+,/ })).toHaveLength(15);
    expect(screen.getByRole("button", { name: "Stage 9, Stage semantic: partial" })).toBeTruthy();
    expect(screen.getByText(/leading hypothesis is database connection pool exhaustion/)).toBeTruthy();
    expect(screen.getByRole("meter", { name: "Score 81 of 100, strong" })).toBeTruthy();
    expect(screen.getAllByText(/not a probability/).length).toBeGreaterThan(0);
    expect(screen.getByText("contradicted")).toBeTruthy();
    expect(screen.getByText("symptom of H1")).toBeTruthy();
    expect(screen.getByText(/checked and is absent/)).toBeTruthy();
  });

  it("opens the evidence behind a finding, a factor and the timeline", () => {
    const onCite = vi.fn();
    render(<UnifiedPanel unified={unified} onCite={onCite} />);
    const factors = screen.getAllByRole("table")[0];
    fireEvent.click(within(factors).getByRole("button", { name: "Open evidence E3" }));
    expect(onCite).toHaveBeenCalledWith("E3");
    fireEvent.click(screen.getAllByRole("button", { name: "Open evidence E14" })[0]);
    expect(onCite).toHaveBeenCalledWith("E14");
  });

  it("explains a stage when it is picked", () => {
    render(<UnifiedPanel unified={unified} onCite={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Stage 11, Stage more_evidence: skipped" }));
    expect(screen.getByText(/nothing to probe/)).toBeTruthy();
  });

  it("lists the evidence in the shared contract, with its access and exact query", () => {
    const onCite = vi.fn();
    render(<UnifiedPanel unified={unified} onCite={onCite} />);
    const ledger = screen.getByText("Evidence ledger").closest("details") as HTMLElement;
    fireEvent.click(within(ledger).getAllByRole("button", { name: "Details" })[0]);
    expect(within(ledger).getByText(/namespace:shop/)).toBeTruthy();
    expect(within(ledger).getByText(/histogram_quantile/)).toBeTruthy();
    fireEvent.click(within(ledger).getByRole("button", { name: "Open the stored evidence" }));
    expect(onCite).toHaveBeenCalledWith("E7");
    fireEvent.change(within(ledger).getByRole("combobox"), { target: { value: "logs" } });
    expect(within(ledger).queryByRole("button", { name: "Open evidence E7" })).toBeNull();
  });

  it("is honest about what it could not do", () => {
    render(<UnifiedPanel unified={unified} onCite={vi.fn()} />);
    expect(screen.getByText(/knowledge search ran as keyword/)).toBeTruthy();
    expect(screen.getByText(/Also considered: Postgres failover/)).toBeTruthy();
    expect(screen.getByText(/Needs an approved action plan/)).toBeTruthy();
    expect(screen.getByText("shadow")).toBeTruthy();
  });

  it("follows a run live from its pipeline events", () => {
    const live = stages.map((s, i) => ({ ...s, status: i < 3 ? "completed" : i === 3 ? "running" : "pending" }));
    render(<LivePipeline events={[{ type: "phase", seq: 1, at: "", phase: "detect" }, { type: "pipeline", seq: 2, at: "", stages: live, budget: { tool_calls: { used: 5, limit: 24 } } }]} />);
    expect(screen.getByText("3/15 stages · 5/24 tool calls")).toBeTruthy();
    expect(screen.getByText("Now:", { exact: false }).textContent).toBe("Now: Stage telemetry");
    expect(screen.getByRole("button", { name: "Stage 4, Stage telemetry: running" })).toBeTruthy();
  });

  it("is part of every report that carries it, and absent otherwise", () => {
    const base = { summary: "s", observed_facts: [], suspected_causes: [], missing_evidence: [], next_steps: [], proposed_remediation: [], generated_by: "rules" };
    const { unmount } = render(<MemoryRouter><Report report={{ ...base, unified }} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.getByRole("region", { name: "Unified investigation" })).toBeTruthy();
    unmount();
    render(<MemoryRouter><Report report={base} onCite={vi.fn()} /></MemoryRouter>);
    expect(screen.queryByRole("region", { name: "Unified investigation" })).toBeNull();
  });
});
