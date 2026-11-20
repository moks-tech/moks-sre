import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ crewai: null as any, posts: [] as any[] }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true, me: { email: "s@x" } }) }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  if (path === "/investigate/config") return { llm: { provider: "openai", model: "m" }, skills: [], environments: ["mk4"], crewai: state.crewai };
  if (path === "/explore/mk4/namespaces") return { namespaces: ["crewai-demo", "shop"], cluster_wide: false };
  if (path.startsWith("/explore/mk4/workloads?namespace=crewai-demo")) return { data: [{ kind: "Deployment", name: "web" }, { kind: "Deployment", name: "api" }, { kind: "CronJob", name: "nightly" }] };
  if (path.startsWith("/explore/mk4/pods?namespace=crewai-demo")) return { data: [{ name: "web-7d9-abc", owner: "ReplicaSet/web-7d9" }] };
  if (opts?.method === "POST") { state.posts.push({ path, body: opts.body }); return { id: "i1", conversation_id: "c1" }; }
  return {};
}) }));

import { NewInvestigation, Report } from "./Investigate";

const off = { enabled: false, available: false, reason: "disabled: MOKS_CREWAI_ENABLED is not true", version: null, limits: { timeout_seconds: 120, max_tool_calls: 20, max_iterations_per_agent: 5 } };
const on = { ...off, enabled: true, available: true, reason: null, version: "1.15.36", llm: "openai/m" };
const page = () => render(<MemoryRouter><NewInvestigation /></MemoryRouter>);

describe("the CrewAI investigation option", () => {
  afterEach(() => { cleanup(); state.posts = []; });

  it("is not offered when the backend reports it unavailable", async () => {
    state.crewai = off;
    page();
    await screen.findByText(/SRE skills/);
    expect(screen.queryByText("CrewAI investigation")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Question" })).toBeTruthy();
  });

  it("is not offered by a backend that predates it", async () => {
    state.crewai = undefined;
    page();
    await screen.findByText(/SRE skills/);
    expect(screen.queryByText("CrewAI investigation")).toBeNull();
  });

  it("starts a scoped workload investigation when available", async () => {
    state.crewai = on;
    page();
    fireEvent.click(await screen.findByLabelText("CrewAI investigation"));
    const ns = await screen.findByRole("combobox", { name: "Namespace" });
    await waitFor(() => expect([...(ns as HTMLSelectElement).options].map((o) => o.value)).toContain("crewai-demo"));
    fireEvent.change(ns, { target: { value: "crewai-demo" } });
    const wl = screen.getByRole("combobox", { name: "Workload" }) as HTMLSelectElement;
    await waitFor(() => expect([...wl.options].map((o) => o.value)).toEqual(["", "api", "web", "__type__"]));  // Deployments only, sorted
    expect(screen.getByRole("option", { name: "CronJob (1)" })).toBeTruthy();
    fireEvent.change(wl, { target: { value: "web" } });
    fireEvent.click(screen.getByText("Run CrewAI investigation"));
    await waitFor(() => expect(state.posts).toHaveLength(1));
    expect(state.posts[0]).toEqual({ path: "/investigations/crewai", body: { environment: "mk4", namespace: "crewai-demo", workload: "web", kind: "Deployment", minutes: undefined } });
  });

  it("offers more kinds, lists that kind's workloads, and still lets a name be typed", async () => {
    state.crewai = on;
    page();
    fireEvent.click(await screen.findByLabelText("CrewAI investigation"));
    const ns = await screen.findByRole("combobox", { name: "Namespace" });
    await waitFor(() => expect((ns as HTMLSelectElement).options.length).toBeGreaterThan(2));
    fireEvent.change(ns, { target: { value: "crewai-demo" } });
    const kind = screen.getByRole("combobox", { name: /Kind/ }) as HTMLSelectElement;
    expect([...kind.options].map((o) => o.value)).toEqual(["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod"]);
    fireEvent.change(kind, { target: { value: "CronJob" } });
    const wl = screen.getByRole("combobox", { name: "Workload" }) as HTMLSelectElement;
    await waitFor(() => expect([...wl.options].map((o) => o.value)).toEqual(["", "nightly", "__type__"]));
    fireEvent.change(kind, { target: { value: "Pod" } });
    await waitFor(() => expect([...(screen.getByRole("combobox", { name: "Workload" }) as HTMLSelectElement).options].map((o) => o.value)).toContain("web-7d9-abc"));
    fireEvent.change(screen.getByRole("combobox", { name: "Workload" }), { target: { value: "__type__" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Workload" }), { target: { value: "not-created-yet" } });
    fireEvent.click(screen.getByText("Run CrewAI investigation"));
    await waitFor(() => expect(state.posts).toHaveLength(1));
    expect(state.posts[0].body).toMatchObject({ namespace: "crewai-demo", kind: "Pod", workload: "not-created-yet" });
  });
});

describe("the CrewAI incident investigation option", () => {
  afterEach(() => { cleanup(); state.posts = []; });
  const flow = (available: boolean) => ({ label: "Incident investigation", description: "d", enabled: true, available, reason: null, agents: 7,
    limits: { timeout_seconds: 600, max_tool_calls: 24, review_rounds: 2, token_budget: 150000 },
    inputs: [{ name: "change_window", default: "24h", choices: ["15m", "1h", "6h", "24h"] }] });

  it("offers both crews and posts the incident flow with its change window and description", async () => {
    state.crewai = { ...on, flows: { workload_investigation: { ...flow(true), label: "Workload investigation", agents: 3 }, incident_investigation: flow(true) } };
    page();
    fireEvent.click(await screen.findByLabelText("CrewAI investigation"));
    fireEvent.click(await screen.findByLabelText("Incident investigation (7 agents)"));
    expect(screen.getByText(/independent reviewer/)).toBeTruthy();
    const ns = await screen.findByRole("combobox", { name: "Namespace" });
    await waitFor(() => expect([...(ns as HTMLSelectElement).options].map((o) => o.value)).toContain("crewai-demo"));
    fireEvent.change(ns, { target: { value: "crewai-demo" } });
    const wl = screen.getByRole("combobox", { name: "Workload" }) as HTMLSelectElement;
    await waitFor(() => expect([...wl.options].map((o) => o.value)).toContain("web"));
    fireEvent.change(wl, { target: { value: "web" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Change window" }), { target: { value: "1h" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Incident" }), { target: { value: "5xx since the 10:00 release" } });
    fireEvent.click(screen.getByText("Run incident investigation"));
    await waitFor(() => expect(state.posts).toHaveLength(1));
    expect(state.posts[0].body).toEqual({ flow: "incident_investigation", environment: "mk4", namespace: "crewai-demo", workload: "web", kind: "Deployment", minutes: undefined,
                                          change_window: "1h", question: "5xx since the 10:00 release" });
  });

  it("is the only choice when the workload flow is switched off", async () => {
    state.crewai = { ...on, available: false, available_any: true, flows: { workload_investigation: { ...flow(false) }, incident_investigation: flow(true) } };
    page();
    fireEvent.click(await screen.findByLabelText("CrewAI investigation"));
    expect(await screen.findByText("Run incident investigation")).toBeTruthy();
    expect(screen.queryByLabelText("Workload investigation (3 agents)")).toBeNull();
  });
});

describe("the CrewAI part of a report", () => {
  afterEach(cleanup);
  const base = { summary: "s", observed_facts: [], suspected_causes: [], missing_evidence: [], next_steps: [], proposed_remediation: [], generated_by: "llm" };

  it("shows agents, gaps, rejected citations and registry excerpts", () => {
    render(<MemoryRouter><Report onCite={vi.fn()} report={{ ...base, crewai: {
      version: "1.15.36", status: "partial", stopped: "the crew's time limit of 120s was reached", tool_calls: 3,
      agents: [{ agent: "workload", label: "Workload investigator", status: "done" }, { agent: "change", label: "Change investigator", status: "running" }],
      gaps: [{ source: "git", reason: "no repository configured" }], validation: { citations_rejected: ["E99"], claims_removed: [{ statement: "x" }], confidence_lowered: [] },
      evidence: [{ id: "E1", source: "k8s_triage", excerpt: "ImagePullBackOff", truncated: false }] } }} /></MemoryRouter>);
    expect(screen.getByText(/Workload investigator: done/)).toBeTruthy();
    expect(screen.getByText(/Stopped early/)).toBeTruthy();
    expect(screen.getByText(/no repository configured/)).toBeTruthy();
    expect(screen.getByText(/1 invented citation\(s\) rejected \(E99\)/)).toBeTruthy();
    expect(screen.getByText("ImagePullBackOff")).toBeTruthy();
  });

  it("is absent from ordinary reports", () => {
    render(<MemoryRouter><Report onCite={vi.fn()} report={base} /></MemoryRouter>);
    expect(screen.queryByText(/CrewAI run/)).toBeNull();
  });
});
