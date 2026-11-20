import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { AddSource, suggestSourceName } from "./Insights";

const ENVS = [{ name: "prod", display_name: "Production", kind: "kubernetes", is_production: true }, { name: "dev", display_name: "Dev", kind: "kubernetes", is_production: false }];

function server(created: { status: number; body: unknown } = { status: 201, body: { id: "s1" } }) {
  const calls: { url: string; body: any }[] = [];
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).endsWith("/environments")) return new Response(JSON.stringify(ENVS), { status: 200 });
    if (String(url).includes("/admin/credentials/choices")) return new Response(JSON.stringify({ credentials: [] }), { status: 200 });
    calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null });
    return new Response(JSON.stringify(created.body), { status: created.status });
  });
  vi.stubGlobal("fetch", fetch);
  return calls;
}

const open = (taken: string[] = [], onAdded = vi.fn()) => {
  render(<FeedbackProvider><AddSource taken={taken} onClose={vi.fn()} onAdded={onAdded} /></FeedbackProvider>);
  return onAdded;
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Add knowledge source", () => {
  it("suggests a name from the repository or the first URL", () => {
    expect(suggestSourceName("github", "mrjithendar/moks-sre", "")).toBe("moks-sre");
    expect(suggestSourceName("github", "acme/Ops.Docs", "")).toBe("ops-docs");
    expect(suggestSourceName("web", "", "https://docs.example.com/runbooks/\nhttps://x.org")).toBe("docs-example-com");
    expect(suggestSourceName("web", "", "not a url")).toBe("");
    expect(suggestSourceName("upload", "acme/ops", "")).toBe("");
  });

  it("fills the name from the repository, scopes it to the first environment and says what will happen", async () => {
    const calls = server();
    const onAdded = open();
    await waitFor(() => expect((screen.getByLabelText("Environment") as HTMLSelectElement).value).toBe("prod"));
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "mrjithendar/moks-sre" } });
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("moks-sre");
    expect(screen.getByText(/moks-sre will copy documentation and infrastructure files from mrjithendar\/moks-sre now, then daily; people who can read prod can find it\./)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add and sync" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalledWith("moks-sre", true));
    expect(calls[0].body).toMatchObject({ name: "moks-sre", type: "github", repository: "mrjithendar/moks-sre", shared_globally: false, scope_environment: "prod", scope_namespace: "*",
      sync_interval_minutes: 1440, profiles: ["documentation", "infrastructure"], credential_ref: { type: "none" } });
  });

  it("keeps a typed name, and refuses one the table already has before sending anything", async () => {
    const calls = server();
    open(["moks-sre"]);
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "mrjithendar/moks-sre" } });
    expect(screen.getByText(/A source named moks-sre already exists/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Add and sync" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "SRE-Docs" } });
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "mrjithendar/other" } });
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("sre-docs");
    expect((screen.getByRole("button", { name: "Add and sync" }) as HTMLButtonElement).disabled).toBe(false);
    expect(calls).toEqual([]);
  });

  it("shares with everyone only when chosen, and never confidential content", async () => {
    const calls = server();
    open();
    fireEvent.click(screen.getByRole("radio", { name: /Uploads/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "handbook" } });
    fireEvent.click(screen.getByRole("radio", { name: /Everyone who can read knowledge/ }));
    expect(screen.getByText(/Anyone with any read access/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Classification"), { target: { value: "confidential" } });
    expect((screen.getByRole("radio", { name: /Everyone who can read knowledge/ }) as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByText(/Anyone with any read access/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Classification"), { target: { value: "public" } });
    fireEvent.click(screen.getByRole("radio", { name: /Everyone who can read knowledge/ }));
    expect(screen.getByText("handbook will hold the files you upload; everyone who can read knowledge can find it.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0].body).toMatchObject({ name: "handbook", type: "upload", shared_globally: true, scope_environment: "*", scope_namespace: "*", classification: "public" });
    expect(calls[0].body.repository).toBeUndefined();
  });

  it("shows the server's reason and stays open when adding fails", async () => {
    server({ status: 409, body: { detail: "A knowledge source named 'moks-sre' already exists; edit it or choose another name", error: { code: "conflict", message: "A knowledge source named 'moks-sre' already exists; edit it or choose another name", status: 409, request_id: "r1" } } });
    const onAdded = open();
    fireEvent.change(screen.getByLabelText("Repository"), { target: { value: "mrjithendar/moks-sre" } });
    await waitFor(() => expect((screen.getByLabelText("Environment") as HTMLSelectElement).value).toBe("prod"));
    fireEvent.click(screen.getByRole("button", { name: "Add and sync" }));
    await waitFor(() => expect(screen.getByText(/already exists; edit it or choose another name/)).toBeTruthy());
    expect(onAdded).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Add and sync" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
