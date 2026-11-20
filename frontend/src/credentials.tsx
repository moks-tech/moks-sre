import { useState } from "react";
import { api } from "./api";

/** A credential as a form holds it. `value` is text pasted in this form and not yet saved; `id` is a credential
 * MOKS already stores (docs/CREDENTIALS.md). Stored values are never sent back to the browser. */
export interface Cred { type: "none" | "stored" | "env" | "file"; id?: string; value?: string; name?: string; path?: string }

export const NO_CRED: Cred = { type: "none" };
export const PASTE: Cred = { type: "stored" };

export function credFromRef(ref: any): Cred {
  if (ref?.type === "stored") return { type: "stored", id: ref.id };
  if (ref?.type === "env") return { type: "env", name: ref.name ?? "" };
  if (ref?.type === "file") return { type: "file", path: ref.path ?? "" };
  return { type: "none" };
}

/** The reference for a credential that needs no saving first, or null while a pasted value is still unsaved. */
export function refOf(c: Cred): any | null {
  if (c.type === "stored") return c.id && !c.value ? { type: "stored", id: c.id } : null;
  if (c.type === "env") return { type: "env", name: c.name ?? "" };
  if (c.type === "file") return { type: "file", path: c.path ?? "" };
  return { type: "none" };
}

/** Saves a pasted value (a new credential, or a new value for the one this form already uses) and returns the
 * reference to save with the connector, provider or source. */
export async function saveCred(c: Cred, label: string): Promise<any> {
  if (c.type === "stored" && c.value) {
    if (c.id) {
      await api(`/admin/credentials/${c.id}`, { method: "PUT", body: { value: c.value, label } });
      return { type: "stored", id: c.id };
    }
    return (await api<any>("/admin/credentials", { method: "POST", body: { label, value: c.value } })).ref;
  }
  if (c.type === "stored" && !c.id) throw new Error(`${label}: paste the value.`);
  return refOf(c);
}

/** One credential field: paste the value (the default), or point at an environment variable or a mounted file.
 * `none` names the option for no credential (for example "Pod identity"); omit it when one is always needed.
 * `storeAs` saves a pasted value as soon as the field is left, for forms that use it before they are saved. */
export function CredentialInput({ label, value, onChange, none, multiline, envPlaceholder, storeAs, files = true }: {
  label: string; value: Cred; onChange: (c: Cred) => void; none?: string; multiline?: boolean; envPlaceholder?: string; storeAs?: string; files?: boolean;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const stored = !!value.id && !value.value;
  async function blur() {
    if (!storeAs || value.type !== "stored" || !value.value) return;
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
  const placeholder = stored ? "Saved. Paste a new value to replace it" : multiline ? "Paste it here" : "Paste the key";
  const field = multiline
    ? <textarea aria-label={label} rows={5} className="mono" autoComplete="off" spellCheck={false} value={value.value ?? ""} placeholder={placeholder} required={!value.id}
        onChange={(e) => onChange({ ...value, value: e.target.value })} onBlur={blur} />
    : <input aria-label={label} type="password" autoComplete="new-password" spellCheck={false} value={value.value ?? ""} placeholder={placeholder} required={!value.id}
        onChange={(e) => onChange({ ...value, value: e.target.value })} onBlur={blur} />;
  return <>
    <label>{label} from
      <select value={value.type} onChange={(e) => onChange({ ...credFromRef(null), type: e.target.value as Cred["type"], id: e.target.value === "stored" ? value.id : undefined })}>
        {none && <option value="none">{none}</option>}
        <option value="stored">Paste the value</option>
        <option value="env">Environment variable</option>
        {files && <option value="file">Mounted file</option>}
      </select>
    </label>
    {value.type === "stored" && <label className={multiline ? "full" : undefined}>{label}{field}
      <span className="small muted" aria-live="polite">{saving ? "Saving…" : error || (stored ? "Stored encrypted in MOKS. It is never shown again." : "Stored encrypted in MOKS when you save. It is never shown again.")}</span>
    </label>}
    {value.type === "env" && <label>Variable<input required pattern="MOKS_CRED_[A-Z0-9_]+" title="MOKS_CRED_ followed by capitals, digits and underscores" value={value.name ?? ""}
      onChange={(e) => onChange({ ...value, name: e.target.value })} placeholder={envPlaceholder ?? "MOKS_CRED_…"} /></label>}
    {value.type === "file" && <label>Path under credentials dir<input required value={value.path ?? ""} onChange={(e) => onChange({ ...value, path: e.target.value })} placeholder="eks/kubeconfig" /></label>}
  </>;
}
