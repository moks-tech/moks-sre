import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { Credentials, Deployment, Features, RuntimeSettings, SettingsLayout, SettingsOverview, showValue } from "./Settings";

const auth = vi.hoisted(() => ({ perms: [] as string[] }));
vi.mock("../auth", () => ({ useAuth: () => ({ can: (p: string) => auth.perms.includes(p) }) }));

/** Answers GETs from `routes` by path prefix and records every write. */
function server(routes: Record<string, unknown>) {
  const writes: { method: string; path: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^.*\/moks\/api/, "").split("?")[0];
    if (init?.method && init.method !== "GET") {
      writes.push({ method: init.method, path, body: init.body ? JSON.parse(String(init.body)) : null });
      return new Response("{}", { status: 200 });
    }
    const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => path.startsWith(k));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: 200 });
  }));
  return writes;
}

const AUTO = { key: "auto_investigations_per_hour", group: "investigations", group_label: "Investigations", label: "Automatic investigations per hour", help: "Per environment per hour.",
  kind: "int", unit: "per hour", optional: false, limit: true, min: 1, max: 6, value: 6, helm_value: 6, overridden: false, ignored: null, env: "MOKS_AUTO_INVESTIGATIONS_PER_HOUR",
  helm: "config.autoInvestigationsPerHour", reason: null, updated_at: null, updated_by: null };
const CLASSIFIER = { ...AUTO, key: "intent_llm_classifier", label: "Ask the chat model when a question is unclear", kind: "bool", unit: "", limit: false, min: null, max: null, value: true, helm_value: true, helm: "" };
const BUDGET = { ...AUTO, key: "llm_user_daily_budget_usd", group: "llm", group_label: "LLM spend", label: "Daily LLM budget per person", kind: "float", unit: "USD", optional: true,
  min: 0.01, max: 10000, value: 2.5, helm_value: null, overridden: true, reason: "cap spend", updated_by: "a@example.com", updated_at: "2026-11-20T08:00:00Z" };
const SETTINGS = {
  runtime: [AUTO, CLASSIFIER, BUDGET], apply_seconds: 30,
  deployment: [
    { section: "Authentication and sessions", note: null, items: [{ key: "session_secret", env: "MOKS_SESSION_SECRET", editable: false, secret: true, set: true },
      { key: "auth_mode", env: "MOKS_AUTH_MODE", editable: false, secret: false, value: "oidc" }] },
    { section: "CrewAI", note: null, items: [{ key: "crewai_enabled", env: "MOKS_CREWAI_ENABLED", editable: false, secret: false, value: false }] },
  ],
};
const STATUS = { capabilities: {
  anomaly_detection: { mode: "disabled", allowed: ["disabled", "shadow"], describe: "Unusual metric behavior per service cohort.", reason: "default", forced_off: false },
  similar_incidents: { mode: "enabled", allowed: ["disabled", "shadow", "enabled"], describe: "Similar historical incidents.", reason: "default", forced_off: false },
}, models: [] };

function at(path: string, element: React.ReactNode) {
  return render(<MemoryRouter initialEntries={[path]}><FeedbackProvider><Routes>
    <Route path="/settings" element={<SettingsLayout />}><Route index element={<SettingsOverview />} /><Route path="*" element={element} /></Route>
  </Routes></FeedbackProvider></MemoryRouter>);
}

beforeEach(() => { auth.perms = ["admin.settings", "admin.users", "ml.admin", "ml.read", "audit.read"]; });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Settings", () => {
  it("lists in its submenu only the pages this role may open, grouped", async () => {
    auth.perms = ["admin.users", "audit.read"];
    server({});
    at("/settings", null);
    const nav = screen.getByRole("navigation", { name: "Settings" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["Overview", "Users & roles", "Audit log"]);
    expect(within(nav).getByRole("group", { name: "Access" })).toBeTruthy();
    expect(within(nav).getByRole("link", { name: "Overview" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("heading", { name: "Compliance" })).toBeTruthy();  // the overview's cards, by group
  });

  it("overview says at a glance whether anomaly detection is on and how many limits were changed here", async () => {
    server({ "/ml/status": STATUS, "/admin/settings": SETTINGS });
    at("/settings", null);
    const glance = await screen.findByLabelText("At a glance");
    await waitFor(() => expect(glance.textContent).toContain("Anomaly detectiondisabled"));
    await waitFor(() => expect(glance.textContent).toContain("1 changed here"));
    expect(within(glance).getAllByRole("link")[0].getAttribute("href")).toBe("/settings/features");
  });

  it("Features: switching anomaly detection asks for a mode and a reason, and says it needs a model first", async () => {
    const writes = server({ "/ml/status": STATUS });
    at("/settings/features", <Features />);
    const card = await screen.findByRole("region", { name: "anomaly detection mode" });
    expect(card.textContent).toContain("scores only workloads with an active model");
    expect(within(card).getByRole("button", { name: "Train models" })).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: "Change mode" }));
    const d = screen.getByRole("dialog", { name: "Change anomaly detection" });
    expect((within(d).getByLabelText("shadow") as HTMLInputElement).checked).toBe(true);
    fireEvent.change(within(d).getByLabelText("Reason (audited)"), { target: { value: "trial on mk4" } });
    fireEvent.click(within(d).getByRole("button", { name: "Save mode" }));
    await waitFor(() => expect(writes[0]).toEqual({ method: "PUT", path: "/ml/capabilities/anomaly_detection", body: { mode: "shadow", reason: "trial on mk4" } }));
  });

  it("Limits & schedules: shows where each value comes from and saves a change with its reason", async () => {
    const writes = server({ "/admin/settings": SETTINGS });
    at("/settings/runtime", <RuntimeSettings />);
    const row = (await screen.findByText("Automatic investigations per hour")).closest("li") as HTMLElement;
    expect(row.textContent).toContain("Helm");
    expect(row.textContent).toContain("6 per hour");
    const budget = screen.getByText("Daily LLM budget per person").closest("li") as HTMLElement;
    expect(budget.textContent).toContain("changed here");
    expect(budget.textContent).toContain("Helm value No limit");
    expect(budget.textContent).toContain("a@example.com");

    fireEvent.click(within(row).getByRole("button", { name: "Change Automatic investigations per hour" }));
    const d = screen.getByRole("dialog", { name: "Change automatic investigations per hour" });
    expect(d.textContent).toContain("Helm sets the ceiling");
    const save = within(d).getByRole("button", { name: "Save" });
    fireEvent.change(within(d).getByLabelText("Automatic investigations per hour (per hour)"), { target: { value: "3" } });
    expect((save as HTMLButtonElement).disabled).toBe(true);  // no reason yet
    fireEvent.change(within(d).getByLabelText("Reason (audited)"), { target: { value: "noisy week" } });
    fireEvent.click(save);
    await waitFor(() => expect(writes[0]).toEqual({ method: "PUT", path: "/admin/settings/auto_investigations_per_hour", body: { value: 3, reason: "noisy week" } }));
  });

  it("Limits & schedules: a value changed here can go back to Helm's", async () => {
    const writes = server({ "/admin/settings": SETTINGS });
    at("/settings/runtime", <RuntimeSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Change Daily LLM budget per person" }));
    const d = screen.getByRole("dialog");
    fireEvent.change(within(d).getByLabelText("Reason (audited)"), { target: { value: "no cap again" } });
    fireEvent.click(within(d).getByRole("button", { name: "Use the Helm value" }));
    await waitFor(() => expect(writes[0]).toEqual({ method: "POST", path: "/admin/settings/llm_user_daily_budget_usd/reset", body: { reason: "no cap again" } }));
  });

  it("Deployment: read-only, secrets only say whether they are set, and a filter narrows it", async () => {
    server({ "/admin/settings": SETTINGS });
    at("/settings/deployment", <Deployment />);
    const auth_ = await screen.findByRole("region", { name: "Authentication and sessions" });
    expect(auth_.textContent).toContain("MOKS_SESSION_SECRETset");
    expect(screen.queryByRole("textbox", { name: /MOKS_/ })).toBeNull();
    fireEvent.change(screen.getByLabelText("Filter"), { target: { value: "crewai" } });
    expect(screen.queryByRole("region", { name: "Authentication and sessions" })).toBeNull();
    expect(screen.getByRole("region", { name: "CrewAI" }).textContent).toContain("false");
  });

  it("formats values the way people read them", () => {
    expect(showValue({ kind: "bool", unit: "" }, false)).toBe("Off");
    expect(showValue({ kind: "float", unit: "USD" }, null)).toBe("No limit");
    expect(showValue({ kind: "float", unit: "USD" }, 2.5)).toBe("$2.5");
    expect(showValue({ kind: "int", unit: "minutes" }, 15)).toBe("15 minutes");
  });
});

describe("Credentials page", () => {
  it("lists stored credentials without values, offers the import, and sets a deployment credential", async () => {
    const posted: any[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const path = String(url).replace(/^.*\/moks\/api/, "").split("?")[0];
      if (path === "/admin/credentials" && init?.method === "POST") { posted.push(JSON.parse(String(init.body))); return new Response(JSON.stringify({ ref: { type: "stored", id: "x" } }), { status: 201 }); }
      if (path === "/admin/credentials") return new Response(JSON.stringify({
        key: { source: "MOKS_SESSION_SECRET", id: "abcd" },
        credentials: [{ id: "c1", label: "API key for Anthropic", used_by: ["LLM provider chat-llm"], state: "current", updated_at: "2026-11-20T06:00:00Z", updated_by: "a@x" }],
        environment_references: [{ name: "MOKS_CRED_GRAFANA_TOKEN", set_here: true }],
        deployment: [{ name: "MOKS_CRED_PINECONE_API_KEY", help: "Pinecone API key", source: "unset", id: null }], count: 1 }), { status: 200 });
      return new Response("{}", { status: 200 });
    }));
    render(<MemoryRouter><FeedbackProvider><Credentials /></FeedbackProvider></MemoryRouter>);
    expect(await screen.findByText("API key for Anthropic")).toBeTruthy();
    expect(screen.getByText(/used by LLM provider chat-llm/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Import from Kubernetes Secret" })).toBeTruthy();
    expect(screen.getByText("MOKS_CRED_GRAFANA_TOKEN")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Set value" }));
    const d = screen.getByRole("dialog", { name: "Set MOKS_CRED_PINECONE_API_KEY" });
    fireEvent.change(within(d).getByLabelText("Value"), { target: { value: "pc-key" } });
    fireEvent.click(within(d).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted).toEqual([{ label: "MOKS_CRED_PINECONE_API_KEY", value: "pc-key" }]));
  });
});
