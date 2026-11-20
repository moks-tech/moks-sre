import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "./auth";
import { navFor, settingsFor } from "./nav";
import { scopeSearch } from "./scope";
import { Icon, useModal } from "./ui";

/** Something the palette can do: open a page, or run a command such as switching theme. */
export interface Command { id: string; label: string; group: string; icon: string; keywords?: string; run: () => void }

export const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

/** Higher is better; 0 is no match. The label wins over the group and keywords, and a word start over a substring. */
export function rank(query: string, c: Pick<Command, "label" | "group" | "keywords">): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const label = c.label.toLowerCase();
  if (label.startsWith(q)) return 100;
  if (label.split(/[\s&/()-]+/).some((w) => w.startsWith(q))) return 80;
  if (label.includes(q)) return 60;
  const extra = `${c.group} ${c.keywords ?? ""}`.toLowerCase();
  if (extra.split(/\s+/).some((w) => w.startsWith(q))) return 40;
  const hay = `${label} ${extra}`;
  return q.split(/\s+/).every((w) => hay.includes(w)) ? 20 : 0;
}

/** ⌘K / Ctrl+K: type a few letters of a page (or a word people use for it, such as "pods" or "cost") and press Enter.
 *  Lists only what this person may open, from the same NAV as the sidebar. */
export function CommandPalette({ onClose, commands = [] }: { onClose: () => void; commands?: Command[] }) {
  const { can } = useAuth();
  const navigate = useNavigate();
  const loc = useLocation();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  useModal(box, true, onClose);
  const carry = scopeSearch(loc.search);
  const all = useMemo<Command[]>(() => [
    ...navFor(can).flatMap((g) => g.items.map((i) => ({ id: `page:${i.to}`, label: i.label, group: g.label, icon: i.icon, keywords: i.keywords,
      run: () => navigate({ pathname: i.to, search: i.scoped ? carry : "" }) }))),
    // every Settings page by name, so "users" or "anomaly" finds it without opening Settings first
    ...settingsFor(can).flatMap((g) => g.items.filter((i) => i.to !== "/settings").map((i) => ({ id: `page:${i.to}`, label: i.label, group: "Settings", icon: i.icon,
      keywords: `${g.label} ${i.keywords ?? ""}`, run: () => navigate(i.to) }))),
    ...commands,
  ], [can, carry, commands, navigate]);
  const results = useMemo(() => all.map((c, n) => ({ c, score: rank(q, c), n })).filter((r) => r.score > 0)
    .sort((a, b) => b.score - a.score || a.n - b.n).map((r) => r.c), [all, q]);
  useEffect(() => { setActive(0); }, [q]);
  useEffect(() => { document.getElementById(`palette-${active}`)?.scrollIntoView?.({ block: "nearest" }); }, [active]);

  function run(c?: Command) {
    if (!c) return;
    onClose();
    c.run();
  }
  function onKey(e: ReactKeyboardEvent<HTMLInputElement>) {
    const last = results.length - 1;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (a >= last ? 0 : a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (a <= 0 ? last : a - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); run(results[active]); }
  }
  return (
    <div className="backdrop palette-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={box} className="palette" role="dialog" aria-modal="true" aria-label="Go to a page or command" tabIndex={-1}>
        <div className="palette-input">
          <Icon name="search" />
          <input data-autofocus type="text" role="combobox" aria-label="Search pages and commands" aria-expanded="true" aria-controls="palette-list" aria-autocomplete="list"
            aria-activedescendant={results.length ? `palette-${active}` : undefined} placeholder="Go to a page or run a command…" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey} />
          <kbd>Esc</kbd>
        </div>
        <ul id="palette-list" role="listbox" aria-label="Results" className="palette-list">
          {results.map((c, n) => (
            <li key={c.id} id={`palette-${n}`} role="option" aria-selected={n === active} className={n === active ? "active" : undefined}
              onMouseMove={() => setActive(n)} onMouseDown={(e) => e.preventDefault()} onClick={() => run(c)}>
              <Icon name={c.icon} /><span className="palette-label">{c.label}</span><span className="palette-group">{c.group}</span>
            </li>
          ))}
        </ul>
        {!results.length && <div className="palette-empty muted" role="status">Nothing matches “{q.trim()}”.</div>}
        <div className="palette-foot" aria-hidden="true"><span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span></div>
      </div>
    </div>
  );
}
