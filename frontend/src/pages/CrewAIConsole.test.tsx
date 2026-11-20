import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";

const state = vi.hoisted(() => ({ admin: true, calls: [] as { path: string; opts?: any }[], responses: {} as Record<string, any> }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true, me: { email: "s@x" } }) }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  state.calls.push({ path, opts });
  const key = Object.keys(state.responses).filter((k) => path.startsWith(k)).sort((a, b) => b.length - a.length)[0];
  const r = key ? state.responses[key] : {};
  return typeof r === "function" ? r(path, opts) : r;
}) }));

import { wordDiff } from "../crewai/CrewEditor";
import { CrewAIConsole } from "./CrewAIConsole";

const run = (over: Partial<any> = {}) => ({ id: "r1", number: 7, flow: "workload_investigation", flow_label: "Workload investigation", trigger: "user", status: "completed",
  crew_status: "completed", environment: "mk4", namespace: "shop", workload: "payments", kind: "Deployment", question: "q", actor: "a@x", created_at: new Date().toISOString(),
  started_at: new Date().toISOString(), finished_at: new Date().toISOString(), seconds: 42, cost_usd: "0.0123", tokens_in: 10, tokens_out: 5, tool_calls: 3, error: null,
  summary: "payments crash-loops", config_version: null, schedule_id: null, schedule: null, rerun_of: null, conversation_id: "r1", ...over });
const step = (s: string, label: string, status = "succeeded") => ({ step: s, label, status, started_at: null, finished_at: null, seconds: 3, error: null, model_calls: 1, tool_calls: 1,
  tokens_in: 1, tokens_out: 1, cost_usd: "0" });
const STEPS = [step("setup", "Set up run"), step("workload", "Workload investigator", "failed"), step("change", "Change investigator", "skipped"), step("review", "Incident reviewer", "skipped"),
  step("validate", "Validate the report", "skipped")];
const stats = { runs: 3, finished: 3, succeeded: 2, failed: 1, success_rate: 0.667, median_seconds: 40, p95_seconds: 90, cost_usd: "0.5" };

const defaults = {
  agents: { workload: { role: "Workload investigator", goal: "Establish {target}.", backstory: "You are an SRE.", tools: ["namespace_health", "pod_logs"] },
            change: { role: "Change investigator", goal: "Find changes.", backstory: "Gaps.", tools: ["rollout_history"] },
            review: { role: "Incident reviewer", goal: "Summarise.", backstory: "Separate facts." } },
  tasks: { workload: { description: "Target: {target}.\n{question}", expected_output: "Observations." }, change: { description: "Changes.", expected_output: "Changes." },
           review: { description: "Report.", expected_output: "JSON." } },
  limits: { timeout_seconds: 120, max_tool_calls: 16, max_iterations: 5, step_tokens: 900, report_tokens: 1500 }, defaults: { kind: "Deployment", minutes: null },
};
const crew = (admin: boolean) => ({ crew: "workload", label: "Workload investigation crew", defaults, effective: defaults, overlay: {}, diff: [], active: null,
  versions: [{ version: 2, note: "rename", active: false, digest: "d", created_by: "a@x", created_at: new Date().toISOString(), activated_at: null, fields: 1, upstream_changed: true }],
  tools: { workload: [{ name: "namespace_health", description: "Health", moks_tool: "k8s_triage" }, { name: "pod_logs", description: "Logs", moks_tool: "k8s_pod_logs" }],
           change: [{ name: "rollout_history", description: "Revisions", moks_tool: "k8s_rollout_history" }] },
  ceilings: { timeout_seconds: [10, 180], max_tool_calls: [1, 16], max_iterations: [1, 20], step_tokens: [100, 4000], report_tokens: [200, 6000] },
  text_limits: { role: 120, goal: 1500, backstory: 4000, description: 6000, expected_output: 2000 }, placeholders: ["{target}", "{question}"],
  code_owned: { rules: "untrusted_data rule", output_contract: "JSON contract", structure: "Three agents in sequence." }, runs_to_test: [run()], can_admin: admin });

const page = (path: string) => render(<FeedbackProvider><MemoryRouter initialEntries={[path]}><Routes><Route path="/crewai/*" element={<CrewAIConsole />} /></Routes></MemoryRouter></FeedbackProvider>);

describe("the CrewAI console", () => {
  afterEach(() => { cleanup(); state.calls = []; state.responses = {}; });

  it("shows the overview: stats, what is running, and each flow's last runs", async () => {
    state.responses["/crewai/overview"] = { day: stats, week: stats, running: [run({ id: "r2", number: 8, status: "running", crew_status: null })], per_day: [{ date: "2026-11-19", queued: 0, running: 1, completed: 2, failed: 1, cancelled: 0 }],
      flows: [{ name: "workload_investigation", label: "Workload investigation", enabled: false, reason: null, last: [run(), run({ id: "r3", number: 6, status: "failed" })], last_run: run(), week: stats }],
      failures: [run({ id: "r3", number: 6, status: "failed", error: "boom" })], can_admin: true };
    page("/crewai");
    expect(await screen.findByText("Last 24 hours")).toBeTruthy();
    expect(screen.getAllByText("67%").length).toBe(2);
    expect(screen.getByText("Workload investigation #8")).toBeTruthy();
    expect(screen.getByText("off")).toBeTruthy();
    expect(screen.getByLabelText("Last runs").querySelectorAll("a").length).toBe(2);
  });

  it("shows a run's steps and its log grouped by step, with search and the transcript of a model call", async () => {
    state.responses["/crewai/runs/r1/logs"] = { lines: [
      { id: 1, step: "setup", at: new Date().toISOString(), level: "info", message: "CrewAI 1.15.41 · model openai/m", data: null, call_id: null },
      { id: 2, step: "workload", at: new Date().toISOString(), level: "info", message: "Model call 1: 10 in, 5 out", data: { event: "llm_call" }, call_id: 9 },
      { id: 3, step: "workload", at: new Date().toISOString(), level: "error", message: "Crew stopped early: limit", data: null, call_id: null },
      { id: 4, step: "workload", at: new Date().toISOString(), level: "debug", message: "hidden debug line", data: null, call_id: null },
    ], last: 4, done: true, status: "failed", steps: STEPS };
    state.responses["/crewai/runs/r1"] = { run: run({ status: "failed", crew_status: "partial", error: "the crew stopped" }), steps: STEPS, inputs: { environment: "mk4", namespace: "shop", workload: "payments" },
      config: { label: "the code's defaults", version: null, draft: false }, report: { summary: "payments crash-loops", findings: 1, causes: 1, missing_evidence: [], next_steps: [], mode: "crewai", crewai: null },
      cost: { by_agent: [{ step: "workload", label: "Workload investigator", model_calls: 1, tool_calls: 1, tokens_in: 10, tokens_out: 5, cost_usd: "0.01" }], by_model: [] },
      errors: ["the crew stopped"], prev: { id: "r0", number: 6 }, next: null, reruns: [], rerun_of: null, can: { rerun: true, cancel: false, save_version: false, test: true } };
    state.responses["/crewai/calls/9"] = { id: 9, step: "workload", model: "openai/m", system: "You are the Workload investigator", prompt: [{ role: "user", content: "Begin." }],
      response: "Thought: I need namespace_health", tokens_in: 10, tokens_out: 5, cost_usd: "0.01", duration_ms: 1200, outcome: "ok", error: null };
    page("/crewai/runs/r1");
    expect(await screen.findByText("Workload investigation #7")).toBeTruthy();
    expect(screen.getAllByText("the crew stopped").length).toBeGreaterThan(0);
    const log = await screen.findByRole("log", { name: "Run log" });
    await within(log).findByText("Crew stopped early: limit");
    expect(within(log).queryByText("hidden debug line")).toBeNull(); // debug hidden at first
    expect((within(log).getByText("Workload investigator").closest("details") as HTMLDetailsElement).open).toBe(true); // failed group opens itself
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "debug" } });
    expect(within(log).getByText("hidden debug line")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search the log"), { target: { value: "Model call" } });
    expect(log.querySelector("mark")?.textContent).toBe("Model call");
    fireEvent.click(within(log).getByText("Transcript"));
    expect(await screen.findByText("Thought: I need namespace_health")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "System" }));
    expect(screen.getByText("You are the Workload investigator")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Re-run" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("edits the incident crew: agents come from the crew, and the commander has two tasks", async () => {
    const d = {
      agents: { commander: { role: "Incident commander", goal: "Scope {target}.", backstory: "You run incidents.", tools: ["namespace_health"] },
                reviewer: { role: "Evidence reviewer", goal: "Challenge.", backstory: "Sceptic." } },
      tasks: { plan: { description: "Incident: {target}.\n{question}", expected_output: "Plan." }, hypotheses: { description: "Hypotheses.", expected_output: "JSON." },
               review: { description: "Review.", expected_output: "JSON." } },
      limits: { timeout_seconds: 600, review_rounds: 2 }, defaults: { kind: "Deployment", minutes: null, change_window: "24h" },
    };
    state.responses["/crewai/crews/sre"] = { ...crew(true), crew: "sre", label: "Incident investigation crew", defaults: d, effective: d, versions: [],
      agent_keys: ["commander", "reviewer"], agent_labels: { commander: "Incident commander", reviewer: "Evidence reviewer" }, task_keys: ["plan", "hypotheses", "review"],
      task_agent: { plan: "commander", hypotheses: "commander", review: "reviewer" }, task_labels: { plan: "Plan the investigation", hypotheses: "Root-cause hypotheses", review: "Independent evidence review" },
      tools: { commander: [{ name: "namespace_health", description: "Health", moks_tool: "k8s_triage" }] },
      ceilings: { timeout_seconds: [30, 600], review_rounds: [1, 3] },
      code_owned: { rules: "rule", output_contract: "[plan] ...", contracts: { plan: "p", hypotheses: "h", review: "r" }, structure: "Seven agents, eight tasks." } };
    page("/crewai/crews/sre");
    expect(await screen.findByText("Task: Plan the investigation")).toBeTruthy();
    expect(screen.getByText("Task: Root-cause hypotheses")).toBeTruthy();
    expect(screen.getByText("Task: Independent evidence review")).toBeTruthy();
    expect(screen.getByText("Review rounds")).toBeTruthy();
    expect((screen.getByRole("combobox", { name: /Default change window/ }) as HTMLSelectElement).value).toBe("24h");
    expect(screen.getByText("Output contracts (one per task)")).toBeTruthy();
  });

  it("edits a crew: an edited field is marked, diffed and checked before it is saved", async () => {
    state.responses["/crewai/crews/workload/check"] = { errors: [{ field: "agents.workload.role", message: "must not be empty" }], warnings: [] };
    state.responses["/crewai/crews/workload"] = crew(true);
    page("/crewai/crews/workload");
    const role = await screen.findByLabelText(/^Role/, { selector: "#agents\\.workload\\.role" });
    fireEvent.change(role, { target: { value: "Pod detective" } });
    expect(screen.getAllByText("edited").length).toBe(1);
    expect(screen.getByText(/1 field\(s\) differ/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Diff" })[0]);
    expect(screen.getByLabelText("Changes against the code's default").querySelector("ins")?.textContent).toContain("Pod");
    fireEvent.click(screen.getByRole("button", { name: "Save as new version" }));
    expect(await screen.findByText("must not be empty")).toBeTruthy();
    expect(state.calls.some((c) => c.path === "/crewai/crews/workload/versions")).toBe(false); // a failed check saves nothing
    expect(screen.getByText("upstream changed")).toBeTruthy();
    expect(screen.getByText("untrusted_data rule")).toBeTruthy();
  });

  it("starts a test run with the draft on a finished run's inputs", async () => {
    state.responses["/crewai/crews/workload/check"] = { errors: [], warnings: [] };
    state.responses["/crewai/crews/workload/test"] = { id: "t1" };
    state.responses["/crewai/crews/workload"] = crew(true);
    page("/crewai/crews/workload");
    fireEvent.change(await screen.findByLabelText(/^Goal/, { selector: "#agents\\.review\\.goal" }), { target: { value: "Be brief." } });
    fireEvent.click(screen.getByRole("button", { name: "Test run" }));
    await waitFor(() => expect(state.calls.some((c) => c.path === "/crewai/crews/workload/test")).toBe(true));
    const body = state.calls.find((c) => c.path === "/crewai/crews/workload/test")!.opts.body;
    expect(body.run_id).toBe("r1");
    expect(body.config.agents.review.goal).toBe("Be brief.");
  });

  it("is read only without crewai.admin", async () => {
    state.responses["/crewai/crews/workload"] = crew(false);
    page("/crewai/crews/workload");
    expect(await screen.findByText(/Read only/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save and activate" })).toBeNull();
    expect((screen.getByLabelText(/^Role/, { selector: "#agents\\.workload\\.role" }) as HTMLInputElement).readOnly).toBe(true);
  });

  it("lists schedules; only an admin can add one, with a preview of the next times", async () => {
    state.responses["/crewai/schedules"] = { schedules: [{ id: "s1", flow: "workload_investigation", flow_label: "Workload investigation", name: "hourly payments", cron: "0 * * * *",
      inputs: { environment: "mk4", namespace: "shop", workload: "payments" }, enabled: true, owner: "a@x", version: 1, next_run_at: new Date(Date.now() + 3600e3).toISOString(),
      upcoming: [], last_run_at: null, last_error: "the schedule's owner may no longer run it", last_run: null, updated_at: null, updated_by: null }], can_admin: true, min_interval_minutes: 15 };
    state.responses["/crewai/flows"] = { flows: [{ name: "workload_investigation", label: "Workload investigation", inputs: [{ name: "environment", label: "Environment", type: "environment", required: true },
      { name: "namespace", label: "Namespace", type: "string", required: true }, { name: "workload", label: "Workload", type: "string", required: true }], defaults: {} }], environments: ["mk4"], can_admin: true };
    state.responses["/crewai/cron"] = { valid: true, next: [new Date().toISOString()] };
    page("/crewai/schedules");
    expect(await screen.findByText("hourly payments")).toBeTruthy();
    expect(screen.getByText("the schedule's owner may no longer run it")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New schedule" }));
    expect(await screen.findByText(/runs workload investigation as the person who saves it/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/^Next:/)).toBeTruthy());
  });
});

describe("wordDiff", () => {
  it("keeps common words and marks what was removed and added", () => {
    const parts = wordDiff("find what changed for the target", "find what broke for the target");
    expect(parts.filter((p) => p.kind === "del").map((p) => p.text.trim())).toEqual(["changed"]);
    expect(parts.filter((p) => p.kind === "add").map((p) => p.text.trim())).toEqual(["broke"]);
    expect(parts.map((p) => p.kind === "del" ? "" : p.text).join("")).toBe("find what broke for the target");
  });
});
