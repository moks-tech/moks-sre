import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { navFor, titleFor } from "./nav";

const auth = vi.hoisted(() => ({ perms: [] as string[] }));
vi.mock("./auth", () => ({ useAuth: () => ({ me: { display_name: "Sam", email: "s@x", roles: [] }, logout: vi.fn(), can: (p: string) => auth.perms.includes(p) }) }));
vi.mock("./api", async (orig) => ({ ...(await orig<any>()), api: vi.fn(async () => ({ status: "ok" })) }));

import { rank } from "./palette";
import { Layout } from "./shell";

function app(path = "/") {
  return render(<MemoryRouter initialEntries={[path]}><Layout><Routes><Route path="*" element={<p>page</p>} /></Routes></Layout></MemoryRouter>);
}
const open = () => fireEvent.click(screen.getByRole("button", { name: "Menu" }));

beforeEach(() => { auth.perms = ["investigation.read", "investigation.run", "explorer.read", "admin.users"]; localStorage.clear(); });
afterEach(() => { cleanup(); document.body.className = ""; });

describe("navigation drawer", () => {
  it("opens with every authorized destination and moves focus inside", () => {
    app();
    open();
    const d = screen.getByRole("dialog", { name: "Navigation" });
    const links = () => within(d).getAllByRole("link").map((a) => a.textContent);
    for (const l of ["Overview", "Metrics", "Investigations", "Incidents"]) expect(links()).toContain(l);
    const admin = within(d).getByRole("button", { name: "Admin" });  // closed until opened, so it never pushes the rest down
    expect(admin.getAttribute("aria-expanded")).toBe("false");
    expect(links()).not.toContain("Users & roles");
    fireEvent.click(admin);
    expect(links()).toContain("Users & roles");
    expect(d.contains(document.activeElement)).toBe(true);
    expect(document.body.classList.contains("scroll-locked")).toBe(true);
  });

  it("closes on Escape, unlocks the page and returns focus to the menu button", () => {
    app();
    const btn = screen.getByRole("button", { name: "Menu" });
    btn.focus();
    fireEvent.click(btn);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.body.classList.contains("scroll-locked")).toBe(false);
    expect(document.activeElement).toBe(btn);
  });

  it("closes when the backdrop is tapped", () => {
    app();
    open();
    fireEvent.mouseDown(screen.getByRole("dialog").parentElement!);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes after navigating", async () => {
    app();
    open();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("link", { name: "Metrics" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getAllByText("Metrics").length).toBeGreaterThan(0);
  });

  it("keeps Tab inside the drawer", () => {
    app();
    open();
    const d = screen.getByRole("dialog");
    const items = [...d.querySelectorAll<HTMLElement>("a[href], button")];
    items[items.length - 1].focus();
    act(() => { fireEvent.keyDown(document, { key: "Tab" }); });
    expect(document.activeElement).toBe(items[0]);
  });
});

describe("RBAC visibility", () => {
  it("lists only what the role may use, and drops empty groups", () => {
    const groups = navFor((p) => p === "investigation.read");
    expect(groups.map((g) => g.label)).toEqual(["Investigate", "Monitor", "Knowledge"]);
    const items = groups.flatMap((g) => g.items.map((i) => i.label));
    expect(items).not.toContain("New investigation");
    expect(items).not.toContain("Audit log");
    expect(items).not.toContain("Metrics");
  });

  it("the drawer and the sidebar show the same destinations", () => {
    auth.perms = ["investigation.read"];
    app();
    const side = within(document.querySelector(".side") as HTMLElement).getAllByRole("link").map((a) => a.textContent).filter((t) => t !== "MOKS");
    open();
    const drawer = within(screen.getByRole("dialog")).getAllByRole("link").map((a) => a.textContent).filter((t) => t !== "MOKS");
    expect(drawer).toEqual(side);
  });

  it("names deep links for the header", () => {
    expect(titleFor("/conversations/abc").label).toBe("Investigations");
    expect(titleFor("/admin/users").group).toBe("Admin");
  });
});

describe("/moks deep links", () => {
  it("opens a nested route under the base path with its menu available", () => {
    render(<MemoryRouter basename="/moks" initialEntries={["/moks/explorer?tab=pods&env=mk4"]}><Layout><Routes><Route path="*" element={<p>explorer page</p>} /></Routes></Layout></MemoryRouter>);
    expect(screen.getByText("explorer page")).toBeTruthy();
    open();
    expect(within(screen.getByRole("dialog", { name: "Navigation" })).getByRole("link", { name: "Overview" }).getAttribute("href")).toBe("/moks");
  });
  it("names every destination the header needs, with or without a conversation open", () => {
    expect(titleFor("/conversations/abc").label).toBe("Investigations");
    expect(titleFor("/logs").label).toBe("Logs");
  });
});

describe("sidebar groups", () => {
  const side = () => within(document.querySelector(".side") as HTMLElement);

  it("close and open, and stay as they were left", () => {
    app("/incidents");
    const monitor = side().getByRole("button", { name: "Monitor" });
    expect(monitor.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(monitor);
    expect(monitor.getAttribute("aria-expanded")).toBe("false");
    expect(side().queryByRole("link", { name: "Metrics" })).toBeNull();
    cleanup();
    app("/incidents");
    expect(side().getByRole("button", { name: "Monitor" }).getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(side().getByRole("button", { name: "Monitor" }));
    expect(side().getByRole("link", { name: "Metrics" })).toBeTruthy();
  });

  it("keep Admin at the foot of the sidebar, and open it on an Admin page", () => {
    app("/admin/users");
    const bottom = document.querySelector(".side .nav-bottom") as HTMLElement;
    expect(within(bottom).getByRole("button", { name: "Admin" }).getAttribute("aria-expanded")).toBe("true");
    expect(within(bottom).getByRole("link", { name: "Users & roles" })).toBeTruthy();
    expect(document.querySelector(".side .nav-main")!.textContent).not.toContain("Users & roles");
  });
});

describe("command palette", () => {
  const palette = () => screen.queryByRole("dialog", { name: "Go to a page or command" });
  const options = () => within(palette()!).queryAllByRole("option").map((o) => o.querySelector(".palette-label")!.textContent);

  it("opens with Ctrl+K or the header button, finds a page by what people call it, and goes there", async () => {
    app();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(palette()).toBeTruthy();
    const input = screen.getByRole("combobox", { name: "Search pages and commands" });
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "pods" } });
    expect(options()[0]).toBe("Kubernetes explorer");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(palette()).toBeNull());
    expect(document.querySelector(".crumbs")!.textContent).toContain("Kubernetes explorer");
    fireEvent.click(screen.getByRole("button", { name: /Go to/ }));
    expect(palette()).toBeTruthy();
  });

  it("lists only pages this person may open, moves with the arrows and closes on Escape", () => {
    auth.perms = ["investigation.read"];
    app();
    const trigger = screen.getByRole("button", { name: /Go to/ });
    trigger.focus();
    fireEvent.click(trigger);
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "audit" } });
    expect(options()).not.toContain("Audit log");
    fireEvent.change(input, { target: { value: "metrics" } });
    expect(options()).not.toContain("Metrics");
    fireEvent.change(input, { target: { value: "in" } });
    const first = input.getAttribute("aria-activedescendant");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.getAttribute("aria-activedescendant")).not.toBe(first);
    expect(screen.getByRole("option", { selected: true }).id).toBe(input.getAttribute("aria-activedescendant"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(palette()).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("runs commands, such as switching theme", () => {
    document.documentElement.dataset.theme = "dark";
    app();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "light" } });
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("moks.theme")).toBe("light");
  });

  it("leaves Ctrl+K to a terminal", () => {
    app();
    const term = document.createElement("div");
    term.className = "xterm";
    const box = document.createElement("textarea");
    term.appendChild(box);
    document.body.appendChild(term);
    fireEvent.keyDown(box, { key: "k", ctrlKey: true });
    expect(palette()).toBeNull();
    term.remove();
  });

  it("ranks the page name before its group and keywords", () => {
    const explorer = { label: "Kubernetes explorer", group: "Monitor", keywords: "pods workloads" };
    const logs = { label: "Logs", group: "Monitor", keywords: "loki" };
    expect(rank("kub", explorer)).toBeGreaterThan(rank("pods", explorer));
    expect(rank("explorer", explorer)).toBeGreaterThan(rank("monitor", explorer));
    expect(rank("monitor", logs)).toBeGreaterThan(0);
    expect(rank("zzz", logs)).toBe(0);
  });
});
