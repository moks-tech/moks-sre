import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Cred, CredentialInput, credFromRef, refOf, saveCred } from "./credentials";

const ID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b";

function server(answer: (url: string, init: any) => any) {
  const calls: { url: string; method: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: any) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body) : null });
    return new Response(JSON.stringify(answer(url, init)), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("credential references", () => {
  it("round-trips every kind and keeps an unsaved paste out of the reference", () => {
    expect(credFromRef({ type: "stored", id: ID })).toEqual({ type: "stored", id: ID });
    expect(refOf({ type: "stored", id: ID })).toEqual({ type: "stored", id: ID });
    expect(refOf({ type: "stored", id: ID, value: "new" })).toBeNull();
    expect(refOf({ type: "env", name: "MOKS_CRED_X" })).toEqual({ type: "env", name: "MOKS_CRED_X" });
    expect(refOf(credFromRef(null))).toEqual({ type: "none" });
  });

  it("stores a new paste, replaces the value of one already stored, and asks for one that is missing", async () => {
    const calls = server((url) => (url.endsWith("/admin/credentials") ? { ref: { type: "stored", id: ID } } : { ref: { type: "stored", id: ID } }));
    expect(await saveCred({ type: "stored", value: "sk-new" }, "API key")).toEqual({ type: "stored", id: ID });
    expect(calls[0]).toMatchObject({ method: "POST", body: { label: "API key", value: "sk-new" } });
    expect(await saveCred({ type: "stored", id: ID, value: "sk-rotated" }, "API key")).toEqual({ type: "stored", id: ID });
    expect(calls[1]).toMatchObject({ method: "PUT", body: { value: "sk-rotated" } });
    expect(calls[1].url).toContain(`/admin/credentials/${ID}`);
    expect(await saveCred({ type: "stored", id: ID }, "API key")).toEqual({ type: "stored", id: ID });
    expect(calls).toHaveLength(2);
    await expect(saveCred({ type: "stored" }, "API key")).rejects.toThrow(/paste the value/);
  });
});

function Harness({ initial, storeAs }: { initial: Cred; storeAs?: string }) {
  const [c, setC] = useState<Cred>(initial);
  return <form><CredentialInput label="API key" value={c} onChange={setC} none="None" storeAs={storeAs} /><output data-testid="ref">{JSON.stringify(c)}</output></form>;
}

describe("CredentialInput", () => {
  it("defaults to pasting, hides what is typed, and never shows a stored value", () => {
    render(<Harness initial={{ type: "stored", id: ID }} />);
    const input = screen.getByLabelText("API key", { selector: "input" }) as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
    expect(input.placeholder).toMatch(/Saved/);
    expect(input.closest("label")!.textContent).toMatch(/never shown again/);
  });

  it("can still point at an environment variable", () => {
    render(<Harness initial={{ type: "stored" }} />);
    fireEvent.change(screen.getByLabelText("API key from"), { target: { value: "env" } });
    fireEvent.change(screen.getByLabelText("Variable"), { target: { value: "MOKS_CRED_ANTHROPIC_API_KEY" } });
    expect(JSON.parse(screen.getByTestId("ref").textContent!)).toEqual({ type: "env", name: "MOKS_CRED_ANTHROPIC_API_KEY" });
  });

  it("with storeAs, saves the paste when the field is left, so the form can use it at once", async () => {
    const calls = server(() => ({ ref: { type: "stored", id: ID } }));
    render(<Harness initial={{ type: "stored" }} storeAs="API key for Anthropic" />);
    const input = screen.getByLabelText("API key", { selector: "input" });
    fireEvent.change(input, { target: { value: "sk-ant-pasted" } });
    fireEvent.blur(input);
    await waitFor(() => expect(JSON.parse(screen.getByTestId("ref").textContent!)).toEqual({ type: "stored", id: ID }));
    expect(calls[0]).toMatchObject({ method: "POST", body: { label: "API key for Anthropic", value: "sk-ant-pasted" } });
    expect(screen.getByTestId("ref").textContent).not.toContain("sk-ant-pasted");
  });
});
