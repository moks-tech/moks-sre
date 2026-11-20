import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackProvider } from "../ui";
import { Detections } from "./Detections";
import { Evals } from "./Insights";
import { Incidents } from "./Ops";

vi.mock("../auth", () => ({ useAuth: () => ({ can: () => true }) }));

/** Answers GETs from `routes` by path prefix and records every write. */
function server(routes: Record<string, unknown>) {
  const writes: { method: string; path: string; body: any }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const path = String(url).replace(/^.*\/moks\/api/, "").split("?")[0];
    if (init?.method && init.method !== "GET") {
      writes.push({ method: init.method, path, body: init.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ id: "inc-1" }), { status: 201 });
    }
    const key = Object.keys(routes).sort((a, b) => b.length - a.length).find((k) => path.startsWith(k));
    return new Response(JSON.stringify(key ? routes[key] : {}), { status: 200 });
  }));
  return writes;
}

const ENVS = [{ name: "mk4", display_name: "Minikube mk4", kind: "minikube", is_production: false }];

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("forms open from the page header in a drawer", () => {
  it("Incidents: Open incident asks what and where, then opens the new workspace", async () => {
    const writes = server({ "/incidents": [], "/alerts/events": [], "/environments": ENVS, "/explore/mk4/namespaces": { namespaces: ["shop", "batch"], cluster_wide: true } });
    render(<MemoryRouter initialEntries={["/incidents"]}><FeedbackProvider><Routes>
      <Route path="/incidents" element={<Incidents />} /><Route path="/incidents/:id" element={<p>incident workspace</p>} />
    </Routes></FeedbackProvider></MemoryRouter>);
    expect(screen.queryByLabelText("Title")).toBeNull();  // no form on the page itself
    fireEvent.click(screen.getByRole("button", { name: "Open incident" }));
    const d = screen.getByRole("dialog", { name: "Open an incident" });
    fireEvent.change(within(d).getByLabelText("Title"), { target: { value: "Checkout returning 502s" } });
    await waitFor(() => expect((within(d).getByLabelText("Environment") as HTMLSelectElement).value).toBe("mk4"));
    await waitFor(() => expect(within(d).getAllByRole("option").some((o) => o.textContent === "shop")).toBe(true));
    fireEvent.change(within(d).getByLabelText("Namespace"), { target: { value: "shop" } });
    fireEvent.click(within(d).getByRole("button", { name: "Open incident" }));
    await waitFor(() => expect(screen.getByText("incident workspace")).toBeTruthy());
    expect(writes[0]).toMatchObject({ method: "POST", path: "/incidents", body: { title: "Checkout returning 502s", severity: "sev3", environment: "mk4", namespace: "shop" } });
  });

  it("Evaluation: one Run evaluation button instead of a button per dataset", async () => {
    const writes = server({ "/evals/datasets": [{ name: "lab", cases: 6 }, { name: "rag", cases: 24 }], "/evals/runs": [] });
    render(<MemoryRouter><FeedbackProvider><Evals /></FeedbackProvider></MemoryRouter>);
    expect(await screen.findByText("No evaluation runs yet.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run lab/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Run evaluation" }));
    const d = screen.getByRole("dialog", { name: "Run an evaluation" });
    fireEvent.click(within(d).getByRole("radio", { name: /rag/ }));
    fireEvent.click(within(d).getByRole("radio", { name: "With the active LLM" }));
    fireEvent.click(within(d).getByRole("button", { name: "Run" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(writes[0]).toMatchObject({ path: "/evals/runs", body: { dataset: "rag", mode: "llm" } });
    expect(screen.getByText(/Evaluation of rag queued \(with the active LLM\)/)).toBeTruthy();
  });

  it("Detection rules: a maintenance window that ends before it starts cannot be added", async () => {
    const writes = server({ "/detections/rules": { rules: [], environments: [{ name: "mk4", detection_enabled: true }] }, "/detections/states": { states: [] }, "/detections/maintenance": { windows: [] } });
    render(<MemoryRouter><FeedbackProvider><Detections /></FeedbackProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Add window" }));
    const d = screen.getByRole("dialog", { name: "Add a maintenance window" });
    fireEvent.change(within(d).getByLabelText("From"), { target: { value: "2026-11-21T10:00" } });
    fireEvent.change(within(d).getByLabelText("To"), { target: { value: "2026-11-21T09:00" } });
    expect(within(d).getByText("The window must end after it starts.")).toBeTruthy();
    expect((within(d).getByRole("button", { name: "Add window" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(d).getByLabelText("To"), { target: { value: "2026-11-21T12:00" } });
    fireEvent.change(within(d).getByLabelText("Reason"), { target: { value: "Node pool upgrade" } });
    fireEvent.click(within(d).getByRole("button", { name: "Add window" }));
    await waitFor(() => expect(writes.length).toBe(1));
    expect(writes[0]).toMatchObject({ path: "/detections/maintenance", body: { environment: "mk4", namespace: "*", reason: "Node pool upgrade" } });
    expect(new Date(writes[0].body.ends_at).getTime()).toBeGreaterThan(new Date(writes[0].body.starts_at).getTime());
  });
});
