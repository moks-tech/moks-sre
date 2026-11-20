import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type Cred, CredentialInput, credFromRef, forgetChoices, refOf, saveCred } from "./credentials";

const ID = "6f1c2a3b-4d5e-4f60-8a9b-0c1d2e3f4a5b";

function server(answer: (url: string, init: any) => any) {
  const calls: { url: string; method: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: any) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body ? JSON.parse(init.body) : null });
    return new Response(JSON.stringify(answer(url, init)), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); forgetChoices(); });

describe("credential references", () => {
  it("round-trips every kind and keeps an unsaved paste out of the reference", () => {
    expect(credFromRef({ type: "stored", id: ID })).toEqual({ type: "stored", id: ID });
    expect(refOf({ type: "stored", id: ID })).toEqual({ type: "stored", id: ID });
    expect(refOf({ type: "stored", id: ID, value: "new" })).toBeNull();
    expect(refOf({ type: "env", name: "MOKS_CRED_X" })).toEqual({ type: "env", name: "MOKS_CRED_X" });
    expect(refOf(credFromRef(null))).toEqual({ type: "none" });
    expect(credFromRef({ type: "env", name: "MOKS_CRED_PG" }, "env:MOKS_CRED_PG (missing)")).toEqual({ type: "env", name: "MOKS_CRED_PG", unset: true });
    expect(refOf({ type: "env", name: "MOKS_CRED_X", value: "pasted" })).toBeNull();
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
    // A paste over an older environment reference becomes a stored credential of its own.
    expect(await saveCred({ type: "env", name: "MOKS_CRED_PG", value: "pg-pass" }, "Password")).toEqual({ type: "stored", id: ID });
    expect(calls[2]).toMatchObject({ method: "POST", body: { label: "Password", value: "pg-pass" } });
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

  it("offers credentials saved in Settings and never an environment variable", async () => {
    const SAVED = "0b7e7c55-0d3c-4a43-9c32-4d1f3e2a9b10";
    server((url) => (url.includes("/admin/credentials/choices") ? { credentials: [{ id: SAVED, label: "OpenAI key (prod)" }] } : {}));
    render(<Harness initial={{ type: "stored" }} />);
    const from = screen.getByLabelText("API key from") as HTMLSelectElement;
    await waitFor(() => expect([...from.options].map((o) => o.textContent)).toEqual(["None", "Paste a new value", "OpenAI key (prod)"]));
    fireEvent.change(from, { target: { value: `id:${SAVED}` } });
    expect(JSON.parse(screen.getByTestId("ref").textContent!)).toEqual({ type: "stored", id: SAVED });
    expect(screen.getByLabelText("API key", { selector: "input" }).closest("label")!.textContent).toContain('as "OpenAI key (prod)"');
  });

  it("shows an older reference to a variable the deployment lacks as a paste box", () => {
    server(() => ({ credentials: [] }));
    render(<Harness initial={{ type: "env", name: "MOKS_CRED_PG_SHOP", unset: true }} />);
    const from = screen.getByLabelText("API key from") as HTMLSelectElement;
    expect(from.selectedOptions[0].textContent).toBe("MOKS_CRED_PG_SHOP (not set)");
    const input = screen.getByLabelText("API key", { selector: "input" }) as HTMLInputElement;
    expect(input.closest("label")!.textContent).toMatch(/does not have\. Paste the value/);
    expect(screen.queryByLabelText("Variable")).toBeNull();
    fireEvent.change(input, { target: { value: "pg-pass" } });
    expect(JSON.parse(screen.getByTestId("ref").textContent!)).toMatchObject({ type: "env", value: "pg-pass" });  // saved as a stored credential
  });

  it("with storeAs, saves the paste when the field is left, so the form can use it at once", async () => {
    const calls = server(() => ({ ref: { type: "stored", id: ID } }));
    render(<Harness initial={{ type: "stored" }} storeAs="API key for Anthropic" />);
    const input = screen.getByLabelText("API key", { selector: "input" });
    fireEvent.change(input, { target: { value: "sk-ant-pasted" } });
    fireEvent.blur(input);
    await waitFor(() => expect(JSON.parse(screen.getByTestId("ref").textContent!)).toEqual({ type: "stored", id: ID }));
    expect(calls.filter((c) => c.method === "POST")).toMatchObject([{ body: { label: "API key for Anthropic", value: "sk-ant-pasted" } }]);
    expect(screen.getByTestId("ref").textContent).not.toContain("sk-ant-pasted");
  });
});
