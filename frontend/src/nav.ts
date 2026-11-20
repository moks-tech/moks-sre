/** Every destination in one place, so the sidebar and the mobile drawer can never disagree.
 *  `show` only decides what is listed: the backend enforces every permission itself. */
export type Can = (perm: string) => boolean;

// scoped: links keep env/ns. keywords: other words people search for in the command palette.
export interface NavItem { to: string; label: string; icon: string; end?: boolean; show: (can: Can) => boolean; scoped?: boolean; keywords?: string }
// bottom: listed under the main groups, at the foot of the sidebar.
export interface NavGroup { label: string; items: NavItem[]; bottom?: boolean }

const read = (can: Can) => can("investigation.read");
const live = (can: Can) => can("explorer.read");

export const NAV: NavGroup[] = [
  { label: "Investigate", items: [
    { to: "/investigate", label: "New investigation", icon: "plus", keywords: "ask question troubleshoot", show: (can) => can("investigation.run") },
    { to: "/investigations", label: "Investigations", icon: "chat", keywords: "conversations inbox reports history", show: read, scoped: true },
    { to: "/incidents", label: "Incidents", icon: "alert", keywords: "outage alerts sev", show: read },
    { to: "/crewai", label: "CrewAI console", icon: "spark2", keywords: "agents crews flows runs schedules", show: read },
  ] },
  { label: "Monitor", items: [
    { to: "/", label: "Overview", icon: "grid", keywords: "home dashboard health", end: true, show: () => true },
    { to: "/explorer", label: "Kubernetes explorer", icon: "cube", keywords: "pods workloads deployments services events nodes kubectl", show: live, scoped: true },
    { to: "/metrics", label: "Metrics", icon: "chart", keywords: "prometheus promql charts", show: live },
    { to: "/logs", label: "Logs", icon: "lines", keywords: "loki logql search", show: live },
    { to: "/detections", label: "Detection rules", icon: "radar", keywords: "alerts rules", show: read },
    { to: "/ml", label: "ML insights", icon: "spark2", keywords: "anomaly similar incidents", end: true, show: (can) => can("ml.read") },
  ] },
  { label: "Operate", items: [
    { to: "/actions", label: "Approvals & actions", icon: "check", keywords: "plans approve remediation changes", show: (can) => can("action.request") || can("action.approve") },
    { to: "/diagnostics", label: "Diagnostics", icon: "tool", keywords: "debug pod netshoot dns", show: (can) => can("action.request") },
    { to: "/terminal", label: "Terminal", icon: "terminal", keywords: "shell kubectl console", show: (can) => can("terminal.use") || can("terminal.revoke") },
    { to: "/finops", label: "FinOps & rightsizing", icon: "coin", keywords: "cost spend rightsizing", show: (can) => can("finops.read"), scoped: true },
    { to: "/backups", label: "Backups", icon: "archive", keywords: "velero restore snapshots", show: (can) => can("backup.read") },
    { to: "/secrets", label: "Secrets sync", icon: "shield", keywords: "vault aws secrets manager", show: (can) => can("secrets.discover") },
  ] },
  { label: "Knowledge", items: [
    { to: "/knowledge", label: "Runbooks & knowledge", icon: "book", keywords: "runbooks docs sources sync index", end: true, show: read },
    { to: "/knowledge/explorer", label: "Knowledge Explorer", icon: "layers", keywords: "documents browse", show: (can) => can("knowledge.read") },
    { to: "/knowledge/retrieval", label: "Retrieval test", icon: "flask", keywords: "search test rag", show: (can) => can("knowledge.test") },
    { to: "/knowledge/review", label: "Knowledge review", icon: "check", keywords: "proposals feedback quarantine", show: (can) => can("knowledge.review") || can("skills.review") },
    { to: "/skills", label: "SRE skills", icon: "spark", keywords: "playbooks", show: read },
  ] },
  { label: "Admin", bottom: true, items: [
    { to: "/audit", label: "Audit log", icon: "shield", keywords: "history who changed", show: (can) => can("audit.read") },
    { to: "/admin/environments", label: "Environments", icon: "layers", keywords: "clusters namespaces", show: (can) => can("admin.policies") },
    { to: "/admin/connectors", label: "Connectors", icon: "plug", keywords: "integrations data sources prometheus loki git", show: (can) => can("admin.connectors") },
    { to: "/admin/backup-storage", label: "Backup storage", icon: "cloud", keywords: "r2 bucket", show: (can) => can("admin.connectors") },
    { to: "/admin/llm", label: "LLM providers", icon: "cpu", keywords: "model ai openai anthropic ollama", show: (can) => can("admin.llm") },
    { to: "/admin/users", label: "Users & roles", icon: "users", keywords: "rbac roles permissions access", show: (can) => can("admin.users") },
    { to: "/evals", label: "Evaluation", icon: "chart", keywords: "evaluation benchmark", show: (can) => can("admin.policies") },
    { to: "/admin/ml", label: "Machine learning", icon: "cpu", keywords: "models training", show: (can) => can("ml.admin") || can("learning.review") || can("learning.export") },
  ] },
];

/** Groups closed until someone opens them: Admin is rarely needed and would push the rest below the fold. */
export const CLOSED_BY_DEFAULT = ["Admin"];

export function navFor(can: Can): NavGroup[] {
  return NAV.map((g) => ({ ...g, items: g.items.filter((i) => i.show(can)) })).filter((g) => g.items.length);
}

export function titleFor(path: string): { group?: string; label: string } {
  if (path.startsWith("/conversations/")) return { group: "Investigate", label: "Investigations" };
  if (path.startsWith("/investigations/")) return { group: "Investigate", label: "Investigation" };
  for (const g of NAV) for (const i of g.items) {
    if (i.end ? path === i.to : path === i.to || path.startsWith(`${i.to}/`)) return { group: g.label, label: i.label };
  }
  return { label: "MOKS" };
}
