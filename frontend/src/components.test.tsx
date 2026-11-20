import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ListHeader, PartialErrors, Stat } from "./components";

afterEach(cleanup);

describe("Stat", () => {
  it("keeps a number large, sets words smaller on the same line, and keeps the full text for hover", () => {
    const { container } = render(<><Stat value={5} label="nodes ready" /><Stat value="moks-chunks-a11fedd0" label="active generation" /><Stat value="$19.29" label="total" /></>);
    const [num, words, money] = [...container.querySelectorAll(".stat")];
    expect(num.className).toBe("stat");
    expect(words.className).toBe("stat words");
    expect(words.getAttribute("title")).toBe("moks-chunks-a11fedd0");
    expect(money.className).toBe("stat");
  });

  it("carries a tone for values that need attention", () => {
    const { container } = render(<Stat value={3} label="firing alerts" tone="bad" />);
    expect(container.querySelector(".stat-tile")!.className).toBe("stat-tile bad");
  });
});

describe("ListHeader", () => {
  it("says how many rows are shown, of how many, in words", () => {
    render(<><ListHeader shown={3} total={10} noun={["user", "users"]} /><ListHeader shown={1} noun={["source", "sources"]} /></>);
    expect(screen.getByText("3 of 10 users")).toBeTruthy();
    expect(screen.getByText("1 source")).toBeTruthy();
  });

  it("refreshes, and says so while it does", () => {
    const reload = vi.fn();
    const { rerender } = render(<ListHeader shown={2} noun={["user", "users"]} load={{ loading: false, reload }} />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh users" }));
    expect(reload).toHaveBeenCalledOnce();
    rerender(<ListHeader shown={2} noun={["user", "users"]} load={{ loading: true, reload }} />);
    expect((screen.getByRole("button", { name: "Refresh users" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Refreshing…")).toBeTruthy();
  });
});

describe("PartialErrors", () => {
  it("is the shared partial state, naming each source and why", () => {
    const { container } = render(<PartialErrors errors={[{ environment: "staging", error: "no Prometheus connector" }, { environment: "mk4", namespace: "shop", error: "timeout" }]} />);
    expect(container.querySelector("[data-state=partial]")).toBeTruthy();
    expect(screen.getByText("2 sources returned partial data")).toBeTruthy();
    expect(screen.getByText(/no Prometheus connector/)).toBeTruthy();
    expect(screen.getByText("mk4/shop")).toBeTruthy();
    expect(screen.getByText(/What you can do:/)).toBeTruthy();
  });

  it("shows nothing when every source answered", () => {
    const { container } = render(<PartialErrors errors={[]} />);
    expect(container.innerHTML).toBe("");
  });
});
