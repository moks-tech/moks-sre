import { useEffect, useState } from "react";
import { api } from "./api";

/** A credential as a form holds it. `value` is text pasted in this form and not yet saved; `id` is a credential
 * MOKS already stores (docs/CREDENTIALS.md). Stored values are never sent back to the browser. `env` and `file` are
 * older references to the Kubernetes Secret: forms no longer offer them, and pasting a value replaces one. `unset`
 * marks such a reference whose variable or file the deployment does not have. */
export interface Cred { type: "none" | "stored" | "env" | "file"; id?: string; value?: string; name?: string; path?: string; unset?: boolean }

export const NO_CRED: Cred = { type: "none" };
export const PASTE: Cred = { type: "stored" };

/** `described` is the API's value-free description ("env:MOKS_CRED_X (missing)"), when the caller has it. */
export function credFromRef(ref: any, described?: string): Cred {
  const unset = typeof described === "string" && /\(missing\)$/.test(described) ? { unset: true } : {};
  if (ref?.type === "stored") return { type: "stored", id: ref.id };
  if (ref?.type === "env") return { type: "env", name: ref.name ?? "", ...unset };
  if (ref?.type === "file") return { type: "file", path: ref.path ?? "", ...unset };
  return { type: "none" };
}

/** The reference for a credential that needs no saving first, or null while a pasted value is still unsaved. */
export function refOf(c: Cred): any | null {
  if (c.type !== "none" && c.value) return null;
  if (c.type === "stored") return c.id ? { type: "stored", id: c.id } : null;
  if (c.type === "env") return { type: "env", name: c.name ?? "" };
  if (c.type === "file") return { type: "file", path: c.path ?? "" };
  return { type: "none" };
}

/** Saves a pasted value (a new credential, or a new value for the stored one this form already uses) and returns the
 * reference to save with the connector, provider or source. A value pasted over an older environment or file
 * reference becomes a new stored credential. */
export async function saveCred(c: Cred, label: string): Promise<any> {
  if (c.type !== "none" && c.value) {
    if (c.type === "stored" && c.id) {
      await api(`/admin/credentials/${c.id}`, { method: "PUT", body: { value: c.value, label } });
      return { type: "stored", id: c.id };
    }
    const ref = (await api<any>("/admin/credentials", { method: "POST", body: { label, value: c.value } })).ref;
    forgetChoices();
    return ref;
  }
  if (c.type === "stored" && !c.id) throw new Error(`${label}: paste the value, or pick a saved credential.`);
  return refOf(c);
}

// ---- saved credentials to pick from ---------------------------------------------------------------------------------

export interface Choice { id: string; label: string }
let choices: { at: number; list: Promise<Choice[]> } | null = null;

/** Drops the cached list, so the next field shows a credential saved a moment ago. */
export function forgetChoices() { choices = null; }

function loadChoices(): Promise<Choice[]> {
  if (!choices || Date.now() - choices.at > 15_000) {
    // Names only; anyone who may store a credential may list them. Without that, the field simply offers none.
    choices = { at: Date.now(), list: api<{ credentials: Choice[] }>("/admin/credentials/choices").then((r) => r.credentials ?? []).catch(() => []) };
  }
  return choices.list;
}

function useChoices(): Choice[] {
  const [list, setList] = useState<Choice[]>([]);
  useEffect(() => {
    let live = true;
    loadChoices().then((l) => { if (live) setList(l); });
    return () => { live = false; };
  }, []);
  return list;
}

const OLD = "old";
const NEW = "new";

/** One credential field: pick a credential stored in MOKS (Settings > Credentials) or paste a new value.
 * `none` names the option for no credential (for example "Pod identity"); omit it when one is always needed.
 * `storeAs` saves a pasted value as soon as the field is left, for forms that use it before they are saved. */
export function CredentialInput({ label, value, onChange, none, multiline, storeAs }: {
  label: string; value: Cred; onChange: (c: Cred) => void; none?: string; multiline?: boolean; storeAs?: string;
}) {
  const list = useChoices();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const old = value.type === "env" || value.type === "file";
  const stored = value.type === "stored" && !!value.id;
  async function blur() {
    if (!storeAs || value.type === "none" || !value.value) return;
    setSaving(true);
    setError("");
    try {
      const ref = await saveCred(value, storeAs);
      onChange({ type: "stored", id: ref.id });
    } catch (e: any) {
      setError(e?.message ?? "Could not save the value");
    } finally {
      setSaving(false);
    }
  }
  const choice = value.type === "none" ? "none" : old ? OLD : stored ? `id:${value.id}` : NEW;
  const pick = (v: string) => {
    setError("");
    if (v === "none") onChange(NO_CRED);
    else if (v === NEW) onChange(PASTE);
    else if (v.startsWith("id:")) onChange({ type: "stored", id: v.slice(3) });
  };
  const savedLabel = stored ? list.find((c) => c.id === value.id)?.label : undefined;
  const from = value.type === "env" ? `environment variable ${value.name}` : `mounted file ${value.path}`;
  const placeholder = old ? "Paste the value to store it in MOKS" : stored ? "Saved. Paste a new value to replace it" : multiline ? "Paste it here" : "Paste the key";
  const note = saving ? "Saving…" : error || (old
    ? value.unset ? `Was set to read ${from}, which this deployment does not have. Paste the value: MOKS stores it encrypted.`
      : `Still read from ${from}. Paste the value to store it in MOKS instead.`
    : stored ? `Stored encrypted in MOKS${savedLabel ? ` as "${savedLabel}"` : ""} and never shown again. A value pasted here replaces it everywhere it is used.`
      : "Stored encrypted in MOKS when you save. It is never shown again.");
  const field = multiline
    ? <textarea aria-label={label} rows={5} className="mono" autoComplete="off" spellCheck={false} value={value.value ?? ""} placeholder={placeholder} required={choice === NEW}
        onChange={(e) => onChange({ ...value, value: e.target.value })} onBlur={blur} />
    : <input aria-label={label} type="password" autoComplete="new-password" spellCheck={false} value={value.value ?? ""} placeholder={placeholder} required={choice === NEW}
        onChange={(e) => onChange({ ...value, value: e.target.value })} onBlur={blur} />;
  return <>
    <label>{label} from
      <select value={choice} onChange={(e) => pick(e.target.value)}>
        {none && <option value="none">{none}</option>}
        <option value={NEW}>Paste a new value</option>
        {old && <option value={OLD}>{value.type === "env" ? value.name : value.path}{value.unset ? " (not set)" : ""}</option>}
        {list.length > 0 && <optgroup label="Saved in Settings > Credentials">
          {list.map((c) => <option key={c.id} value={`id:${c.id}`}>{c.label}</option>)}
        </optgroup>}
        {stored && !savedLabel && <option value={choice}>Saved credential</option>}
      </select>
    </label>
    {value.type !== "none" && <label className={multiline ? "full" : undefined}>{label}{field}
      <span className={`small ${old && value.unset && !value.value ? "warn-text" : "muted"}`} aria-live="polite">{note}</span>
    </label>}
  </>;
}
