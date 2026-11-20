import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { MOVED, settingsFor, type Can } from "./nav";
import { Layout } from "./shell";
import { Skeleton } from "./ui";
import Login from "./pages/Login";

// Each area loads on first use, so first paint does not pay for pages never opened.
const admin = () => import("./pages/Admin");
const insights = () => import("./pages/Insights");
const investigate = () => import("./pages/Investigate");
const live = () => import("./pages/Live");
const ops = () => import("./pages/Ops");
const backups = () => import("./pages/Backups");
const secrets = () => import("./pages/Secrets");
const terminal = () => import("./pages/Terminal");
const page = <M,>(load: () => Promise<M>, name: keyof M) => lazy(() => load().then((m) => ({ default: m[name] as ComponentType })));

const Audit = page(admin, "Audit"), Connectors = page(admin, "Connectors"), Environments = page(admin, "Environments"), LLMProviders = page(admin, "LLMProviders"), Users = page(admin, "Users");
const Terminals = page(terminal, "Terminals"), TerminalView = page(terminal, "TerminalView");
const Backups = page(backups, "Backups"), BackupStorage = page(backups, "BackupStorage");
const Secrets = page(secrets, "Secrets"), SecretPlan = page(secrets, "SecretPlan");
const Workspace = lazy(() => import("./pages/Conversations").then((m) => ({ default: m.Workspace })));
const InvestigationView = page(investigate, "InvestigationView"), NewInvestigation = page(investigate, "NewInvestigation");
const Evals = page(insights, "Evals"), FinOps = page(insights, "FinOps"), Knowledge = page(insights, "Knowledge");
const knowledge = () => import("./pages/Knowledge");
const KnowledgeExplorer = page(knowledge, "KnowledgeExplorer"), RetrievalWorkspace = page(knowledge, "RetrievalWorkspace"), KnowledgeReview = page(knowledge, "KnowledgeReview");
const Skills = lazy(() => import("./pages/Skills").then((m) => ({ default: m.Skills })));
const Explorer = page(live, "Explorer");
const Logs = lazy(() => import("./pages/Logs").then((m) => ({ default: m.Logs })));
const Detections = lazy(() => import("./pages/Detections").then((m) => ({ default: m.Detections })));
const Metrics = lazy(() => import("./pages/Metrics").then((m) => ({ default: m.Metrics })));
const Actions = page(ops, "Actions"), DiagnosticView = page(ops, "DiagnosticView"), Diagnostics = page(ops, "Diagnostics"), IncidentView = page(ops, "IncidentView"), Incidents = page(ops, "Incidents"), PlanView = page(ops, "PlanView");
const Overview = lazy(() => import("./pages/Overview"));
const ml = () => import("./pages/ML");
const MLInsights = page(ml, "MLInsights"), MLAdmin = page(ml, "MLAdmin");
const CrewAIConsole = lazy(() => import("./pages/CrewAIConsole").then((m) => ({ default: m.CrewAIConsole })));
const settings = () => import("./pages/Settings");
const SettingsLayout = page(settings, "SettingsLayout"), SettingsOverview = page(settings, "SettingsOverview"), Features = page(settings, "Features"), RuntimeSettings = page(settings, "RuntimeSettings"),
  Deployment = page(settings, "Deployment"), CrewAISettingsPage = page(settings, "CrewAISettingsPage"), SettingsNotFound = page(settings, "SettingsNotFound");

/** Explains a page the role cannot use, instead of an empty page and a 403. The backend still decides. */
function Need({ any, children }: { any: string[] | ((can: Can) => boolean); children: ReactNode }) {
  const { can } = useAuth();
  if (typeof any === "function" ? any(can) : any.some(can)) return children;
  return <div className="alert warn" role="alert"><h1>You don't have access to this page</h1>Ask an administrator for a role that includes it, or go back to <Link to="/">Overview</Link>.</div>;
}

/** An old address: same query and fragment, new path, without a history entry for the old one. */
function Moved({ to }: { to: string }) {
  const loc = useLocation();
  return <Navigate to={{ pathname: to, search: loc.search, hash: loc.hash }} replace />;
}

export default function App() {
  const { me, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="boot"><Skeleton rows={4} /></div>;
  if (loc.pathname === "/login") return <Login />;
  if (!me) return <Navigate to={`/login?next=${encodeURIComponent(import.meta.env.BASE_URL.replace(/\/$/, "") + loc.pathname + loc.search)}`} replace />;
  return (
    <Layout>
      <Suspense fallback={<Skeleton rows={6} />}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/investigate" element={<NewInvestigation />} />
        <Route path="/investigations" element={<Workspace />} />
        <Route path="/investigations/:id" element={<InvestigationView />} />
        <Route path="/conversations/:id" element={<Workspace />} />
        <Route path="/secrets" element={<Need any={["secrets.discover"]}><Secrets /></Need>} />
        <Route path="/secrets/plans/:id" element={<Need any={["secrets.discover"]}><SecretPlan /></Need>} />
        <Route path="/backups" element={<Need any={["backup.read"]}><Backups /></Need>} />
        <Route path="/explorer" element={<Need any={["explorer.read"]}><Explorer /></Need>} />
        <Route path="/metrics" element={<Need any={["explorer.read"]}><Metrics /></Need>} />
        <Route path="/logs" element={<Need any={["explorer.read"]}><Logs /></Need>} />
        <Route path="/detections" element={<Detections />} />
        <Route path="/incidents" element={<Incidents />} />
        <Route path="/crewai/*" element={<Need any={["investigation.read"]}><CrewAIConsole /></Need>} />
        <Route path="/incidents/:id" element={<IncidentView />} />
        <Route path="/actions" element={<Need any={["action.request", "action.approve"]}><Actions /></Need>} />
        <Route path="/actions/:id" element={<Need any={["investigation.read", "action.approve"]}><PlanView /></Need>} />
        <Route path="/diagnostics" element={<Need any={["action.request"]}><Diagnostics /></Need>} />
        <Route path="/diagnostics/:id" element={<Need any={["action.request"]}><DiagnosticView /></Need>} />
        <Route path="/terminal" element={<Need any={["terminal.use", "terminal.revoke"]}><Terminals /></Need>} />
        <Route path="/terminal/:id" element={<Need any={["terminal.use", "terminal.revoke"]}><TerminalView /></Need>} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/knowledge/explorer" element={<Need any={["knowledge.read"]}><KnowledgeExplorer /></Need>} />
        <Route path="/knowledge/retrieval" element={<Need any={["knowledge.test"]}><RetrievalWorkspace /></Need>} />
        <Route path="/knowledge/review" element={<Need any={["knowledge.review", "skills.review", "investigation.read"]}><KnowledgeReview /></Need>} />
        <Route path="/finops" element={<Need any={["finops.read"]}><FinOps /></Need>} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/ml" element={<Need any={["ml.read"]}><MLInsights /></Need>} />
        <Route path="/settings" element={<Need any={(can) => settingsFor(can).length > 0}><SettingsLayout /></Need>}>
          <Route index element={<SettingsOverview />} />
          <Route path="features" element={<Need any={["ml.admin"]}><Features /></Need>} />
          <Route path="runtime" element={<Need any={["admin.settings"]}><RuntimeSettings /></Need>} />
          <Route path="deployment" element={<Need any={["admin.settings"]}><Deployment /></Need>} />
          <Route path="users" element={<Need any={["admin.users"]}><Users /></Need>} />
          <Route path="environments" element={<Need any={["admin.policies"]}><Environments /></Need>} />
          <Route path="connectors" element={<Need any={["admin.connectors"]}><Connectors /></Need>} />
          <Route path="backup-storage" element={<Need any={["admin.connectors"]}><BackupStorage /></Need>} />
          <Route path="llm" element={<Need any={["admin.llm"]}><LLMProviders /></Need>} />
          <Route path="ml" element={<Need any={["ml.admin", "learning.review", "learning.export"]}><MLAdmin /></Need>} />
          <Route path="crewai" element={<Need any={["crewai.admin"]}><CrewAISettingsPage /></Need>} />
          <Route path="evaluation" element={<Need any={["admin.policies"]}><Evals /></Need>} />
          <Route path="audit" element={<Need any={["audit.read"]}><Audit /></Need>} />
          <Route path="*" element={<SettingsNotFound />} />
        </Route>
        {Object.entries(MOVED).map(([from, to]) => <Route key={from} path={from} element={<Moved to={to} />} />)}
        <Route path="*" element={<div className="empty"><h1>Page not found</h1><Link to="/">Back to Overview</Link></div>} />
      </Routes>
      </Suspense>
    </Layout>
  );
}
