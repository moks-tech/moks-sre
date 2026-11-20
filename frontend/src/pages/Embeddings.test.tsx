import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { optionsBody, profileFor, withProfile } from "./Admin";
import { EmbeddingComparison, Evals, RebuildIndex } from "./Insights";

vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true }) }));

function server(routes: Record<string, unknown>) {
  const writes: { method: string; path: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^.*\/moks\/api/, "");
    if (init?.method && init.method !== "GET") {
      writes.push({ method: init.method, path, body: init.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ id: "run-1", queued: true, model: "huggingface/BAAI/bge-base-en-v1.5/768/abc" }), { status: 201 });
    }
    const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => path.startsWith(k));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: 200 });
  }));
  return writes;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const PROFILES = [{ model: "BAAI/bge-base-en-v1.5", match: ["bge-base-en-v1.5"], query_prefix: "Represent: ", document_prefix: "", measured: true },
  { model: "intfloat/e5-base-v2", match: ["e5-base-v2"], query_prefix: "query: ", document_prefix: "passage: ", measured: true }];
const MODELS = [{ id: "m1", name: "bge-m3", purpose: "embedding", provider: "ollama", model: "bge-m3:567m", is_active: true },
  { id: "m2", name: "bge-base", purpose: "embedding", provider: "huggingface", model: "BAAI/bge-base-en-v1.5", is_active: false },
  { id: "c1", name: "claude", purpose: "chat", provider: "anthropic", model: "claude-sonnet-5-5", is_active: true }];

describe("embedding options", () => {
  it("only sends the options that are set, so an empty form keeps the model key", () => {
    expect(optionsBody({ query_prefix: "", document_prefix: "", revision: "", max_input_chars: "" })).toEqual({});
    expect(optionsBody({ query_prefix: "q: ", max_input_chars: "2000", revision: "abc" })).toEqual({ query_prefix: "q: ", max_input_chars: 2000, revision: "abc" });
  });

  it("finds a profile by any spelling of the model name and fills a new configuration only", () => {
    expect(profileFor("BAAI/bge-base-en-v1.5", PROFILES)?.query_prefix).toBe("Represent: ");
    expect(profileFor("/opt/models/e5-base-v2", PROFILES)?.document_prefix).toBe("passage: ");
    expect(profileFor("unknown", PROFILES)).toBeUndefined();
    const fresh = withProfile({ purpose: "embedding", model: "intfloat/e5-base-v2", embedding_options: {} }, PROFILES, true, { revision: "f52b" });
    expect(fresh.embedding_options).toMatchObject({ query_prefix: "query: ", document_prefix: "passage: ", revision: "f52b" });
    const existing = { purpose: "embedding", model: "intfloat/e5-base-v2", embedding_options: { query_prefix: "" } };
    expect(withProfile(existing, PROFILES, false)).toBe(existing);
    const chat = { purpose: "chat", model: "x" };
    expect(withProfile(chat, PROFILES, true)).toBe(chat);
  });
});

describe("rebuilding with another model", () => {
  it("is blocked until the model passed its evaluation, and says why", async () => {
    server({ "/admin/llm": MODELS, "/knowledge/reindex/check": { model: "huggingface/BAAI/bge-base-en-v1.5/768/abc", serving: "ollama/bge-m3:567m/1024", replacement: true, allowed: false,
      reason: "huggingface/BAAI/bge-base-en-v1.5/768/abc cannot replace ollama/bge-m3:567m/1024 yet: it has not passed the embedding evaluation." } });
    render(<MemoryRouter><FeedbackProvider><RebuildIndex onClose={() => {}} onQueued={() => {}} /></FeedbackProvider></MemoryRouter>);
    const d = screen.getByRole("dialog", { name: "Rebuild the index" });
    expect(await within(d).findByText(/has not passed the embedding evaluation/)).toBeTruthy();
    expect((within(d).getByRole("button", { name: "Rebuild" }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(d).getAllByRole("option").map((o) => o.textContent)).not.toContain(expect.stringContaining("claude"));
  });

  it("sends the chosen model once the check allows it", async () => {
    const writes = server({ "/admin/llm": MODELS, "/knowledge/reindex/check": { model: "m", serving: "s", replacement: true, allowed: true, reason: null, evaluation: { id: "r", passed: true, at: "2026-11-20T10:00:00Z" } } });
    const queued = vi.fn();
    render(<MemoryRouter><FeedbackProvider><RebuildIndex onClose={() => {}} onQueued={queued} /></FeedbackProvider></MemoryRouter>);
    const d = screen.getByRole("dialog", { name: "Rebuild the index" });
    await waitFor(() => expect(within(d).getAllByRole("option").length).toBe(3));
    fireEvent.change(within(d).getByLabelText("Model"), { target: { value: "m2" } });
    expect(await within(d).findByText("passed")).toBeTruthy();
    fireEvent.click(within(d).getByRole("button", { name: "Rebuild" }));
    await waitFor(() => expect(queued).toHaveBeenCalled());
    expect(writes[0]).toMatchObject({ method: "POST", path: "/knowledge/reindex", body: { llm_config_id: "m2" } });
  });
});

describe("the embedding evaluation", () => {
  it("asks which embedding model to evaluate instead of a mode", async () => {
    const writes = server({ "/evals/datasets": [{ name: "rag", cases: 24, kind: "rag" }, { name: "embedding", cases: 85, kind: "embedding" }], "/evals/runs": [], "/admin/llm": MODELS });
    render(<MemoryRouter><FeedbackProvider><Evals /></FeedbackProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Run evaluation" }));
    const d = screen.getByRole("dialog", { name: "Run an evaluation" });
    fireEvent.click(within(d).getByRole("radio", { name: /embedding/ }));
    expect(within(d).queryByRole("radio", { name: "With the active LLM" })).toBeNull();
    await waitFor(() => expect(within(d).getAllByRole("option").length).toBe(3));
    fireEvent.change(within(d).getByLabelText("Model"), { target: { value: "m2" } });
    fireEvent.click(within(d).getByRole("button", { name: "Run" }));
    await waitFor(() => expect(writes.length).toBe(1));
    expect(writes[0]).toMatchObject({ path: "/evals/runs", body: { dataset: "embedding", llm_config_id: "m2" } });
  });

  it("compares the candidate with the serving model", () => {
    const side = (model: string, ndcg: number) => ({ model, dimensions: 768, latency: { query_p95_ms: 20, documents_per_second: 12.5 },
      modes: { hybrid: { overall: { "ndcg@10": ndcg, "recall@5": 0.88, "mrr@10": 0.97, "answer@1": 0.84 } }, vector: { overall: { "ndcg@10": 0.915 } }, keyword: { overall: { "ndcg@10": 0.885 } } } });
    render(<EmbeddingComparison m={{ candidate: side("huggingface/bge-base", 0.92), serving: side("ollama/bge-m3", 0.923) }} />);
    const t = screen.getByRole("region", { name: "Embedding comparison" });
    expect(within(t).getByText("huggingface/bge-base")).toBeTruthy();
    expect(within(t).getByText("0.920")).toBeTruthy();
    expect(within(t).getByText("0.923")).toBeTruthy();
    expect(within(t).getAllByText("0.885").length).toBe(2);
  });
});
