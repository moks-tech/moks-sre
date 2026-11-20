import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { chooseModel, LLMProviders } from "./Admin";
import { canList } from "./ModelPicker";
import { forgetChoices } from "../credentials";

vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true, me: { email: "a@x" } }) }));

const KEY_ID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b";
const SAVED_ID = "0b7e7c55-0d3c-4a43-9c32-4d1f3e2a9b10";

/** /admin/llm/models answers with `listing(body)`; every request is recorded. */
function server(listing: (body: any) => any) {
  const asked: any[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^.*\/moks\/api/, "").split("?")[0];
    if (path === "/admin/llm/models") {
      const body = JSON.parse(String(init?.body));
      asked.push(body);
      return new Response(JSON.stringify(listing(body)), { status: 200 });
    }
    if (path === "/admin/credentials") return new Response(JSON.stringify({ ref: { type: "stored", id: KEY_ID } }), { status: 201 });
    if (path === "/admin/credentials/choices") return new Response(JSON.stringify({ credentials: [{ id: SAVED_ID, label: "OpenAI key" }] }), { status: 200 });
    return new Response(JSON.stringify(path === "/admin/llm" ? [] : {}), { status: 200 });
  }));
  return asked;
}

async function openForm(preset: string) {
  render(<FeedbackProvider><LLMProviders /></FeedbackProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "Add provider" }));
  const d = screen.getByRole("dialog", { name: "Add provider" });
  fireEvent.change(within(d).getByLabelText("Provider"), { target: { value: preset } });
  return d;
}

const select = (d: HTMLElement) => within(d).getByLabelText("Model") as HTMLSelectElement;
const groups = (d: HTMLElement) => [...select(d).querySelectorAll("optgroup")].map((g) => g.label);

afterEach(() => { cleanup(); vi.unstubAllGlobals(); forgetChoices(); });

describe("LLM provider form: models from the provider", () => {
  it("stores the pasted Anthropic key, lists its models with it, and fills in prices and a name", async () => {
    const asked = server(() => ({ models: [{ id: "claude-opus-5-5", label: "Claude Opus 5.5", purpose: "chat" }, { id: "claude-haiku-5-5", label: "Claude Haiku 5.5", purpose: "chat" }], error: null, note: null }));
    const d = await openForm("anthropic");
    expect(asked).toHaveLength(0);  // nothing to ask with until a key is pasted
    const key = within(d).getByLabelText("API key");
    fireEvent.change(key, { target: { value: "sk-ant-pasted" } });
    fireEvent.blur(key);
    await waitFor(() => expect(groups(d)).toEqual(["Chat models (2)"]));
    expect(asked[0]).toEqual({ provider: "anthropic", endpoint: null, region: null, credential_ref: { type: "stored", id: KEY_ID }, fresh: false });
    expect(JSON.stringify(asked)).not.toContain("sk-ant-pasted");
    expect(within(d).getByRole("status").textContent).toContain("2 models from the provider");
    fireEvent.change(select(d), { target: { value: "claude-opus-5-5" } });
    expect(select(d).value).toBe("claude-opus-5-5");
    expect((within(d).getByLabelText("Input $ / 1M tokens") as HTMLInputElement).value).toBe("4");
    expect((within(d).getByLabelText("Name") as HTMLInputElement).value).toBe("anthropic-claude-opus-5-5");
    fireEvent.change(within(d).getByLabelText("Provider"), { target: { value: "openai" } });  // another provider: the filled-in name and prices go
    expect((within(d).getByLabelText("Name") as HTMLInputElement).value).toBe("");
    expect((within(d).getByLabelText("Input $ / 1M tokens") as HTMLInputElement).value).toBe("0");
  });

  it("puts the purpose being configured first, filters a long list, and takes prices from the provider's list", async () => {
    const many = Array.from({ length: 20 }, (_, n) => ({ id: `vendor/chat-${n}`, label: `Chat ${n}`, purpose: "chat", input_price_per_mtok: "3", output_price_per_mtok: "15" }));
    server(() => ({ models: [...many, { id: "vendor/embed-1", label: "Embed 1", purpose: "embedding" }], error: null, note: null }));
    const d = await openForm("openrouter");
    await waitFor(() => expect(groups(d)).toEqual(["Chat models (20)", "Other models (1)"]));
    fireEvent.change(within(d).getByLabelText("Filter models"), { target: { value: "chat-1" } });
    expect(groups(d)).toEqual(["Chat models (11)"]);  // chat-1 and chat-10 to chat-19
    fireEvent.change(select(d), { target: { value: "vendor/chat-12" } });
    expect((within(d).getByLabelText("Output $ / 1M tokens") as HTMLInputElement).value).toBe("15");
    fireEvent.change(within(d).getByLabelText("Purpose"), { target: { value: "embedding" } });
    fireEvent.change(within(d).getByLabelText("Filter models"), { target: { value: "" } });
    expect(groups(d)[0]).toBe("Embedding models (1)");
  });

  it("keeps the usual models when the account cannot be asked, says why, and still lets a name be typed", async () => {
    const asked = server(() => ({ models: [], error: "the provider answered 401: invalid API key", note: null }));
    const d = await openForm("openai");
    expect(groups(d)[0]).toMatch(/^Suggested chat models \(\d+\)$/);  // at once, before the provider is asked
    const from = within(d).getByLabelText("API key from") as HTMLSelectElement;
    await waitFor(() => expect([...from.options].map((o) => o.value)).toContain(`id:${SAVED_ID}`));
    fireEvent.change(from, { target: { value: `id:${SAVED_ID}` } });  // a key saved in Settings > Credentials
    expect([...select(d).options].map((o) => o.value)).toContain("gpt-5");
    await waitFor(() => expect(within(d).getByRole("status").textContent).toContain("401: invalid API key"));
    expect(asked.at(-1).credential_ref).toEqual({ type: "stored", id: SAVED_ID });
    expect(within(d).getByRole("status").textContent).toContain("Showing the usual models");
    fireEvent.change(within(d).getByLabelText("Purpose"), { target: { value: "embedding" } });
    expect(groups(d)[0]).toBe("Suggested embedding models (2)");
    fireEvent.change(select(d), { target: { value: "__type__" } });
    const input = within(d).getByLabelText("Model") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "my-model" } });
    expect(input.value).toBe("my-model");
    fireEvent.click(within(d).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(asked.at(-1).fresh).toBe(true));
  });

  it("offers typing a name the list does not have", async () => {
    server(() => ({ models: [{ id: "llama3.2:3b", label: "llama3.2:3b", purpose: "chat" }], error: null, note: null }));
    const d = await openForm("ollama");
    await waitFor(() => expect(groups(d)).toEqual(["Chat models (1)"]));
    fireEvent.change(select(d), { target: { value: "__type__" } });
    fireEvent.change(within(d).getByLabelText("Model"), { target: { value: "qwen3:8b" } });
    expect((within(d).getByLabelText("Model") as HTMLInputElement).value).toBe("qwen3:8b");
    fireEvent.click(within(d).getByRole("button", { name: "Choose from the list" }));
    expect(select(d).value).toBe("qwen3:8b");  // still the typed one, shown as the current value
  });
});

describe("model helpers", () => {
  it("asks only when the connection is filled in", () => {
    expect(canList("openai", "https://", { type: "env", name: "MOKS_CRED_X" })).toBe(false);
    expect(canList("ollama", "", { type: "none" })).toBe(false);
    expect(canList("openai", "", { type: "env", name: "MOKS_CRED_" })).toBe(false);
    expect(canList("openai", "", { type: "env", name: "MOKS_CRED_OPENAI_API_KEY" })).toBe(true);
    expect(canList("bedrock", "", { type: "none" })).toBe(true);
  });
  it("keeps a name someone already typed", () => {
    expect(chooseModel({ provider: "openai", endpoint: "", name: "chat-main" }, "gpt-x").name).toBe("chat-main");
  });
});
