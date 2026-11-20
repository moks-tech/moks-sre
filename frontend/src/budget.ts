/** LLM budgets as Settings → LLM providers and FinOps show them. Every limit except an LLM's daily budget is optional. */

/** An optional limit as the API takes it: empty is no limit (null), anything else is sent as typed. */
export function limitOrNull(v: unknown): string | null {
  return v === "" || v === null || v === undefined ? null : String(v).trim() || null;
}

const noLimit = (limit: unknown) => limit === null || limit === undefined || limit === "";

/** "$1.20 of $50.00" against a limit, or "$1.20 · no limit" without one. */
export function spentOf(spent: unknown, limit: unknown): string {
  const s = `$${Number(spent ?? 0).toFixed(2)}`;
  return noLimit(limit) ? `${s} · no limit` : `${s} of $${Number(limit).toFixed(2)}`;
}

/** Spend at or above this share of a limit is shown as a warning. */
export const NEAR = 0.8;

export function nearLimit(spent: unknown, limit: unknown): boolean {
  return !noLimit(limit) && Number(spent ?? 0) >= NEAR * Number(limit);
}

/** The class for a spend figure: highlighted near or over its limit. */
export const spendClass = (spent: unknown, limit: unknown) => `small${nearLimit(spent, limit) ? " warn-text" : ""}`;
