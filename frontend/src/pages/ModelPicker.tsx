import { useEffect, useRef, useState } from "react";
import { api } from "../api";

export interface ModelInfo { id: string; label: string; purpose: "chat" | "embedding" | "other"; context?: number; input_price_per_mtok?: string | null; output_price_per_mtok?: string | null }
interface Listing { models: ModelInfo[]; error: string | null; note: string | null }

const TYPE = "__type__";

/** Whether the connection is filled in far enough to ask the provider. */
export function canList(provider: string, endpoint: string, credential: { type: string; name?: string; id?: string }): boolean {
  if (endpoint && !/^https?:\/\/[^/\s]+/.test(endpoint)) return false;
  if (provider === "ollama" && !endpoint) return false;
  if (credential.type === "stored") return !!credential.id;
  return credential.type !== "env" || /^MOKS_CRED_[A-Z0-9_]+$/.test(credential.name ?? "");
}

function optionText(m: ModelInfo): string {
  const extra = [m.label !== m.id ? m.id : "", m.context ? `${Math.round(m.context / 1000)}k context` : ""].filter(Boolean).join(" · ");
  return extra ? `${m.label} (${extra})` : m.label;
}

/** The model field of the provider form. Choosing a provider lists models at once: its usual ones first, then,
 *  as soon as the connection works, what the provider itself says this credential can use. The purpose being
 *  configured comes first, a long list has a filter, and a name the list lacks can still be typed. */
export function ModelPicker({ provider, endpoint, region, credential, purpose, value, suggested = [], onChange }: {
  provider: string; endpoint: string; region: string; credential: { type: string; name?: string; id?: string }; purpose: string; value: string;
  suggested?: ModelInfo[]; onChange: (model: string, info?: ModelInfo) => void;
}) {
  const [listing, setListing] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(false);
  const [typing, setTyping] = useState(false);
  const [filter, setFilter] = useState("");
  const seq = useRef(0);
  const ready = canList(provider, endpoint, credential);
  const key = JSON.stringify([provider, endpoint, provider === "bedrock" ? region : "", credential.type, credential.name ?? "", credential.id ?? ""]);

  async function load(fresh = false) {
    const n = ++seq.current;
    setLoading(true);
    try {
      const r = await api<Listing>("/admin/llm/models", { method: "POST", body: { provider, endpoint: endpoint || null, region: provider === "bedrock" ? region : null, credential_ref: credential, fresh } });
      if (n === seq.current) setListing(r);
    } catch (e: any) {
      if (n === seq.current) setListing({ models: [], error: e?.message ?? "the request failed", note: null });
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }
  useEffect(() => {
    seq.current += 1;  // an answer for the old connection must not land
    setListing(null);
    setFilter("");
    setTyping(false);
    if (!ready) { setLoading(false); return; }
    const t = setTimeout(() => load(), 400);  // typing an endpoint or key name asks once, not per keystroke
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ready]);

  const live = listing?.models ?? [];
  const fromProvider = live.length > 0;
  const models = fromProvider ? live : suggested;
  const byId = new Map(models.map((m) => [m.id, m]));
  const needle = filter.trim().toLowerCase();
  const shown = needle ? models.filter((m) => `${m.id} ${m.label}`.toLowerCase().includes(needle)) : models;
  const primary = shown.filter((m) => m.purpose === purpose);
  const rest = shown.filter((m) => m.purpose !== purpose);
  const list = models.length > 0 && !typing;
  const noun = `${fromProvider ? "" : "Suggested "}${purpose === "embedding" ? "embedding" : "chat"} models`;
  const why = !ready ? "Fill in the endpoint and API key reference to list your account's models."
    : listing?.error ? `Could not list your account's models: ${listing.error}.`
    : listing?.note ?? null;

  return <div className="model-picker stack tight">
    {list ? <div className={models.length > 12 ? "model-pick" : undefined}>
      {models.length > 12 && <label>Filter<input type="search" aria-label="Filter models" placeholder={`Filter ${models.length} models`} value={filter} onChange={(e) => setFilter(e.target.value)} /></label>}
      <label>Model
        <select required value={value} onChange={(e) => { if (e.target.value === TYPE) { setTyping(true); return; } onChange(e.target.value, byId.get(e.target.value)); }}>
          <option value="" disabled>Choose a model…</option>
          {value && !shown.some((m) => m.id === value) && <option value={value}>{byId.get(value) ? optionText(byId.get(value)!) : `${value} (current)`}</option>}
          {primary.length > 0 && <optgroup label={`${noun[0].toUpperCase()}${noun.slice(1)} (${primary.length})`}>{primary.map((m) => <option key={m.id} value={m.id}>{optionText(m)}</option>)}</optgroup>}
          {rest.length > 0 && <optgroup label={`Other models (${rest.length})`}>{rest.map((m) => <option key={m.id} value={m.id}>{optionText(m)}{m.purpose !== "other" ? ` · ${m.purpose}` : ""}</option>)}</optgroup>}
          <option value={TYPE}>Type a model name…</option>
        </select>
      </label>
    </div> : <label>Model<input required value={value} onChange={(e) => onChange(e.target.value, byId.get(e.target.value))} placeholder={provider === "bedrock" ? "inference profile or model ID" : provider === "azure_openai" ? "deployment name" : "model name"} /></label>}
    <div className="small muted row tight" role="status">
      {loading ? <span>Asking the provider for your account's models…</span>
        : fromProvider ? <><span>{live.length} model{live.length === 1 ? "" : "s"} from the provider{needle ? `, ${shown.length} match` : ""}.</span><button type="button" className="ghost small" onClick={() => load(true)}>Refresh</button></>
        : <>
            <span>{why}{why && " "}{suggested.length ? "Showing the usual models; check that your account has the one you choose." : !listing?.note ? "Type the model name." : ""}</span>
            {ready && listing?.error && <button type="button" className="ghost small" onClick={() => load(true)}>Try again</button>}
          </>}
      {typing && models.length > 0 && <button type="button" className="ghost small" onClick={() => setTyping(false)}>Choose from the list</button>}
    </div>
  </div>;
}
