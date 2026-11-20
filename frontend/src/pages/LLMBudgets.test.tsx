import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const LLM = { id: "l1", name: "claude-opus", purpose: "chat", provider: "anthropic", model: "claude-opus-5-5", endpoint: null, region: null,
  credential: "env MOKS_CRED_ANTHROPIC_API_KEY", credential_ref: { type: "env", name: "MOKS_CRED_ANTHROPIC_API_KEY" }, timeout_seconds: 60, max_output_tokens: 2000,
  daily_budget_usd: "5", monthly_budget_usd: null, spent_today_usd: "1.0000", spent_month_usd: "3.0000", input_price_per_mtok: "4", output_price_per_mtok: "20",
  embedding_dimensions: null, embedding_options: {}, is_active: true, model_key: null, spent_last_24h_usd: "1" };
const TOTALS = { month_resets_at: "2026-12-01T00:00:00+00:00", month_start: "2026-11-01", providers: [
  { provider: "anthropic", llms: 1, daily_budget_usd: null, monthly_budget_usd: "100.0000", spent_today_usd: "1.5000", spent_month_usd: "85.0000", forecast_month_usd: "120.0000" }] };

const state = vi.hoisted(() => ({ calls: [] as { path: string; method?: string; body: any }[] }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  state.calls.push({ path, method: opts?.method, body: opts?.body });
  if (path === "/admin/llm" && !opts?.method) return [LLM];
  if (path === "/admin/llm/totals") return TOTALS;
  if (path === "/admin/llm/embedding-profiles") return [];
  return {};
}) }));

import { limitOrNull, nearLimit, spentOf } from "../budget";
import { FeedbackProvider } from "../ui";
import { LLMProviders } from "./Admin";
import { LLMBudgets } from "./Insights";

afterEach(() => { cleanup(); state.calls.length = 0; });

const writes = () => state.calls.filter((c) => c.method);

describe("budget helpers", () => {
  it("treats an empty limit as no limit and never invents one", () => {
    expect(limitOrNull("")).toBeNull();
    expect(limitOrNull(null)).toBeNull();
    expect(limitOrNull(" 25 ")).toBe("25");
    expect(spentOf("1.5", null)).toBe("$1.50 · no limit");
    expect(spentOf("85", "100.0000")).toBe("$85.00 of $100.00");
    expect(nearLimit("85", "100")).toBe(true);
    expect(nearLimit("10", "100")).toBe(false);
    expect(nearLimit("1000", null)).toBe(false);
  });
});

describe("Settings → LLM providers budgets", () => {
  function open() { render(<MemoryRouter><FeedbackProvider><LLMProviders /></FeedbackProvider></MemoryRouter>); }

  it("shows each LLM's spend today and this month, with no monthly limit unless one is set", async () => {
    open();
    expect(await screen.findByText("$1.00 of $5.00")).toBeTruthy();
    expect(screen.getByText("$3.00 · no limit")).toBeTruthy();
  });

  it("saves an empty monthly budget as no limit, and a typed one as typed", async () => {
    open();
    fireEvent.click(await screen.findByText("claude-opus"));
    const monthly = await screen.findByLabelText("Monthly budget (USD)") as HTMLInputElement;
    expect(monthly.value).toBe("");
    expect(monthly.placeholder).toBe("no monthly limit");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toMatchObject({ method: "PUT", path: "/admin/llm/l1" });
    expect(writes()[0].body.monthly_budget_usd).toBeNull();
    expect(writes()[0].body).not.toHaveProperty("spent_month_usd");
    state.calls.length = 0;
    fireEvent.click(await screen.findByText("claude-opus"));
    fireEvent.change(await screen.findByLabelText("Monthly budget (USD)"), { target: { value: "40" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].body.monthly_budget_usd).toBe("40");
  });

  it("lists provider totals and saves cleared fields as no limit", async () => {
    open();
    const table = await screen.findByRole("table", { name: "Provider totals" }).catch(() => screen.findByLabelText("Provider totals"));
    expect(within(table).getByText("$85.00 of $100.00").className).toContain("warn-text");  // at or above 80 %
    expect(within(table).getByText("$1.50 · no limit")).toBeTruthy();
    expect(within(table).getByText("$120.00")).toBeTruthy();
    fireEvent.click(within(table).getByText("anthropic"));
    const monthly = await screen.findByLabelText("Monthly total (USD)") as HTMLInputElement;
    expect(monthly.value).toBe("100.0000");
    expect((screen.getByLabelText("Daily total (USD)") as HTMLInputElement).value).toBe("");
    fireEvent.change(monthly, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toEqual({ method: "PUT", path: "/admin/llm/totals/anthropic", body: { daily_budget_usd: null, monthly_budget_usd: null } });
  });
});

describe("FinOps LLM budgets", () => {
  it("shows provider totals with a forecast and every LLM's budgets", () => {
    render(<MemoryRouter><LLMBudgets b={{ ...TOTALS, llms: [{ ...LLM, monthly_budget_usd: "25" }] }} /></MemoryRouter>);
    expect(screen.getByText("$85.00 of $100.00")).toBeTruthy();
    expect(screen.getByText("$3.00 of $25.00")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Settings → LLM providers" }).getAttribute("href")).toBe("/settings/llm");
  });
});
