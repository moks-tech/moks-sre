import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ calls: [] as { path: string; method?: string; body: any }[] }));
vi.mock("../api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async (path: string, opts?: any) => {
  state.calls.push({ path, method: opts?.method, body: opts?.body });
  if (path === "/environments") return [{ name: "demo" }];
  if (path === "/admin/connectors" && !opts?.method) return [{ id: "c1", environment: "demo", type: "prometheus", name: "prometheus", enabled: true, implemented: true, config: { base_url: "http://p" }, credential_ref: { type: "none" } }];
  if (path === "/admin/connectors") return { id: "c2", name: opts.body.name };
  if (path.endsWith("/test")) return { ok: true, message: "reachable" };
  return {};
}) }));

import { FeedbackProvider } from "../ui";
import { connectorBody, Connectors, duplicateConnector, endpointOf, LLM_PRESETS, matchesUser, missingSources, presetOf } from "./Admin";

afterEach(() => { cleanup(); state.calls.length = 0; });

describe("LLM presets", () => {
  it("offers the major providers and points compatible servers at their endpoints", () => {
    expect(LLM_PRESETS.anthropic.provider).toBe("anthropic");
    expect(LLM_PRESETS.google.provider).toBe("gemini");
    expect(LLM_PRESETS.openai.provider).toBe("openai");
    expect(LLM_PRESETS.mistral).toMatchObject({ provider: "openai", endpoint: "https://api.mistral.ai/v1" });
  });
  it("recognises a saved provider again, including an unlisted compatible server", () => {
    expect(presetOf({ provider: "anthropic", endpoint: "" })).toBe("anthropic");
    expect(presetOf({ provider: "openai", endpoint: "https://api.groq.com/openai/v1" })).toBe("groq");
    expect(presetOf({ provider: "openai", endpoint: "http://vllm:8000/v1" })).toBe("compatible");
  });
  it("only prices models it lists, and never a key value", () => {
    expect(LLM_PRESETS.anthropic.prices?.["claude-opus-5-5"]).toEqual(["4", "20"]);
    for (const p of Object.values(LLM_PRESETS)) if (p.key) expect(p.key).toMatch(/^MOKS_CRED_[A-Z0-9_]+$/);
  });
});

const source = (extra: any = {}) => ({ source: "prometheus", name: "prom", enabled: true, config: { base_url: "http://p", auth: "none" }, advanced: false, json: "", cred: { type: "env", name: "MOKS_CRED_X" }, ...extra });

describe("connector form rules", () => {
  it("sends data-store clusters with their password as a credential reference, never in config", () => {
    const pg = connectorBody(source({ source: "postgres", name: "shop-db", config: { host: "shop-db-rw.shop.svc", port: 5432, database: "app", username: "moks_monitor", sslmode: "require", namespace: "shop" }, cred: { type: "env", name: "MOKS_CRED_PG_SHOP" } }), "demo");
    expect(pg).toMatchObject({ type: "postgres", name: "shop-db", credential_ref: { type: "env", name: "MOKS_CRED_PG_SHOP" } });
    expect(JSON.stringify(pg.config)).not.toMatch(/password/i);
    expect(connectorBody(source({ source: "redis", name: "cache", config: { host: "redis", mode: "sentinel" } }), "demo").type).toBe("redis");
    expect(endpointOf({ config: { host: "redis.cache.svc", port: 6379 } })).toBe("redis.cache.svc:6379");
    expect(endpointOf({ config: { base_url: "http://p" } })).toBe("http://p");
  });
  it("does not store a credential the connector would ignore", () => {
    expect(connectorBody(source(), "demo").credential_ref).toEqual({ type: "none" });
    expect(connectorBody(source({ config: { base_url: "http://p", auth: "basic", username: "eks" } }), "demo").credential_ref).toEqual({ type: "env", name: "MOKS_CRED_X" });
  });
  it("sends a pasted credential as a stored reference, and AWS keys inside config.credentials", () => {
    const id = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b";
    expect(connectorBody(source({ source: "grafana", config: { base_url: "http://g" }, cred: { type: "stored", id } }), "demo").credential_ref).toEqual({ type: "stored", id });
    const aws = connectorBody(source({ source: "aws_secrets", config: { account_id: "123456789012", region: "us-east-1", allowed_prefixes: ["moks/"] }, cred: { type: "none" },
      aws: { access_key_id: { type: "stored", id }, secret_access_key: { type: "stored", id } } }), "demo");
    expect(aws.config.credentials).toEqual({ access_key_id: { type: "stored", id }, secret_access_key: { type: "stored", id } });
    const pod = connectorBody(source({ source: "aws_secrets", config: { account_id: "123456789012", credentials: { access_key_id: {}, secret_access_key: {} } }, cred: { type: "none" },
      aws: { access_key_id: { type: "none" }, secret_access_key: { type: "none" } } }), "demo");
    expect(pod.config.credentials).toBeUndefined();
  });
  it("finds a name already used in that environment, but not the connector being edited", () => {
    const rows = [{ id: "c1", environment: "demo", name: "prometheus" }];
    expect(duplicateConnector(rows, "demo", "prometheus", null)?.id).toBe("c1");
    expect(duplicateConnector(rows, "demo", "prometheus", "c1")).toBeUndefined();
    expect(duplicateConnector(rows, "other", "prometheus", null)).toBeUndefined();
  });
  it("lists what an environment lacks, counting CloudWatch as logs and ignoring disabled connectors", () => {
    const rows = [{ environment: "demo", type: "kubernetes", enabled: true }, { environment: "demo", type: "cloudwatch", enabled: true }, { environment: "demo", type: "prometheus", enabled: false }];
    expect(missingSources(rows, "demo").map((m) => m.source)).toEqual(["prometheus", "cost_model"]);
  });
  it("finds users by name or email", () => {
    expect(matchesUser({ email: "a@x.io", display_name: "Sam Lee" }, "lee")).toBe(true);
    expect(matchesUser({ email: "a@x.io" }, "b@")).toBe(false);
  });
});

describe("Connectors page", () => {
  const open = async () => {
    render(<FeedbackProvider><Connectors /></FeedbackProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Add connector" }));
  };
  it("blocks a duplicate name and offers the existing connector", async () => {
    await open();
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("already has a connector named prometheus");
    expect((screen.getByRole("button", { name: "Add and test" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Edit prometheus" }));
    expect(await screen.findByRole("button", { name: "Save and test" })).toBeTruthy();
  });
  it("tests a new connector as soon as it is saved", async () => {
    await open();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "prom-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Add and test" }));
    await waitFor(() => expect(state.calls.some((c) => c.path === "/admin/connectors/c2/test" && c.method === "POST")).toBe(true));
    expect(await screen.findByText("prom-2: reachable")).toBeTruthy();
  });
  it("tests every untested connector from one button and sums up", async () => {
    render(<FeedbackProvider><Connectors /></FeedbackProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Test all (1 untested)" }));
    await waitFor(() => expect(state.calls.some((c) => c.path === "/admin/connectors/c1/test" && c.method === "POST")).toBe(true));
    expect(await screen.findByText("The connector passed.")).toBeTruthy();
  });
  it("asks for the missing data sources", async () => {
    render(<FeedbackProvider><Connectors /></FeedbackProvider>);
    expect(await screen.findByRole("button", { name: "Add Cluster cost model to demo" })).toBeTruthy();
  });
});
