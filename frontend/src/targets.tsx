import { useEffect, useState } from "react";
import { api } from "./api";

/** The kinds a workload investigation can target (backend crew.config.KINDS). */
export const WORKLOAD_KINDS = ["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod"];

const NS = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const NAME = /^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$/;
const TYPE = "__type__";

type Listing<T> = { items: T[] | null; error: string };
const cache = new Map<string, Promise<any>>();

/** One request per URL while the page is open; a failure is not cached, so reopening asks again. */
function load(path: string): Promise<any> {
  if (!cache.has(path)) cache.set(path, api<any>(path).catch((e) => { cache.delete(path); throw e; }));
  return cache.get(path)!;
}

function useListing<T>(path: string | null, pick: (r: any) => T[]): Listing<T> {
  const [state, setState] = useState<Listing<T>>({ items: null, error: "" });
  useEffect(() => {
    if (!path) { setState({ items: [], error: "" }); return; }
    let live = true;
    setState({ items: null, error: "" });
    load(path).then((r) => live && setState({ items: pick(r), error: r?.error ?? "" }))
      .catch((e) => live && setState({ items: [], error: e?.message ?? "could not be listed" }));
    return () => { live = false; };
  }, [path]);  // eslint-disable-line react-hooks/exhaustive-deps
  return state;
}

/** Namespaces this person may read in the environment, as the cluster lists them. */
export function useNamespaces(environment: string): Listing<string> {
  return useListing(environment ? `/explore/${encodeURIComponent(environment)}/namespaces` : null, (r) => r?.namespaces ?? []);
}

/** Workloads in a namespace by kind; pods only when the kind is Pod. */
export function useWorkloads(environment: string, namespace: string, kind: string): Listing<{ kind: string; name: string }> {
  const ok = !!environment && NS.test(namespace);
  const base = `/explore/${encodeURIComponent(environment)}`;
  const path = !ok ? null : kind === "Pod" ? `${base}/pods?namespace=${encodeURIComponent(namespace)}` : `${base}/workloads?namespace=${encodeURIComponent(namespace)}`;
  return useListing(path, (r) => (Array.isArray(r?.data) ? r.data : []).map((w: any) => ({ kind: w.kind ?? "Pod", name: w.name })).filter((w: any) => w.name));
}

/** A select of the cluster's names, with a fallback to typing one: for a name not listed (not created yet, or
 * listing not allowed) and whenever the list cannot be read. */
function Picker({ id, label, value, onChange, names, error, pattern, placeholder, disabled, empty }: {
  id?: string; label: string; value: string; onChange: (v: string) => void; names: string[] | null; error: string; pattern: RegExp;
  placeholder: string; disabled?: boolean; empty: string;
}) {
  const listed = names ?? [];
  const [typing, setTyping] = useState(false);
  const typed = typing || (names !== null && !listed.length) || (!!value && names !== null && !listed.includes(value));
  if (typed) {
    return <span className="stack tight">
      <input id={id} aria-label={label} required pattern={pattern.source.replace(/^\^|\$$/g, "")} value={value} disabled={disabled} placeholder={placeholder}
        autoComplete="off" spellCheck={false} onChange={(e) => onChange(e.target.value.trim())} />
      <span className="small muted">{error ? `Not listed: ${error}` : listed.length ? <button type="button" className="link small" onClick={() => { setTyping(false); onChange(""); }}>Choose from the list</button> : empty}</span>
    </span>;
  }
  return <select id={id} aria-label={label} required value={value} disabled={disabled || names === null}
    onChange={(e) => (e.target.value === TYPE ? (setTyping(true), onChange("")) : onChange(e.target.value))}>
    <option value="">{names === null ? "Loading…" : `Choose (${listed.length})`}</option>
    {listed.map((n) => <option key={n} value={n}>{n}</option>)}
    <option value={TYPE}>Type a name…</option>
  </select>;
}

export function NamespaceSelect({ id, environment, value, onChange }: { id?: string; environment: string; value: string; onChange: (v: string) => void }) {
  const { items, error } = useNamespaces(environment);
  return <Picker id={id} label="Namespace" value={value} onChange={onChange} names={items} error={error} pattern={NS} placeholder="e.g. shop"
    empty="No namespaces are listed for you here; type one." />;
}

export function WorkloadSelect({ id, environment, namespace, kind, value, onChange }: {
  id?: string; environment: string; namespace: string; kind: string; value: string; onChange: (v: string) => void;
}) {
  const { items, error } = useWorkloads(environment, namespace, kind);
  const names = items === null ? null : [...new Set(items.filter((w) => w.kind === kind).map((w) => w.name))].sort();
  return <Picker id={id} label="Workload" value={value} onChange={onChange} names={namespace ? names : []} error={error} pattern={NAME}
    placeholder={namespace ? `${kind} name` : "Choose a namespace first"} disabled={!namespace}
    empty={namespace ? `No ${kind} in ${namespace}; choose another kind, or type a name.` : ""} />;
}

/** Kind options, each with how many the namespace has when that is known. */
export function KindOptions({ environment, namespace, kinds = WORKLOAD_KINDS }: { environment: string; namespace: string; kinds?: string[] }) {
  const { items } = useWorkloads(environment, namespace, "Deployment");
  const counts = new Map<string, number>();
  for (const w of items ?? []) counts.set(w.kind, (counts.get(w.kind) ?? 0) + 1);
  return <>{kinds.map((k) => <option key={k} value={k}>{k}{items && k !== "Pod" ? ` (${counts.get(k) ?? 0})` : ""}</option>)}</>;
}
