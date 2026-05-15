import { Suspense, lazy, useEffect, useState } from "react";
import {
  HashRouter,
  Link,
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Activity, ArrowRight, Binoculars, FolderKanban, Radar } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  addRecentProject,
  getRecentProjects,
  mergeProjectSuggestions,
} from "@/lib/recent-projects";
import { cn } from "@/lib/utils";

const POLLING_STALL_SECONDS = 30;

function PollingPill() {
  const queryClient = useQueryClient();
  const [, force] = useState(0);

  useEffect(() => {
    const unsub = queryClient.getQueryCache().subscribe(() => force((n) => n + 1));
    const interval = window.setInterval(() => force((n) => n + 1), 1000);
    return () => {
      unsub();
      window.clearInterval(interval);
    };
  }, [queryClient]);

  const queries = queryClient.getQueryCache().getAll();
  const lastUpdatedAt = queries.reduce(
    (max, q) => Math.max(max, q.state.dataUpdatedAt ?? 0),
    0,
  );

  if (!lastUpdatedAt) {
    return (
      <span className="chip-pill chip-mono text-muted-foreground">
        <span className="dot bg-amber-500" />
        polling · idle
      </span>
    );
  }

  const seconds = Math.max(0, Math.round((Date.now() - lastUpdatedAt) / 1000));
  const stalled = seconds > POLLING_STALL_SECONDS;
  const label = stalled
    ? `stalled · ${seconds}s ago`
    : seconds < 2
      ? "polling · just now"
      : `polling · last ${seconds}s`;

  return (
    <span
      className={cn(
        "chip-pill chip-mono",
        stalled
          ? "border-destructive/30 bg-destructive/10 text-destructive"
          : "border-border/70 bg-white/70 text-muted-foreground",
      )}
    >
      <span className={cn("dot", stalled ? "bg-destructive" : "bg-emerald-500")} />
      {label}
    </span>
  );
}


const ObservationView = lazy(() =>
  import("@/views/observation-view").then((module) => ({
    default: module.ObservationView,
  })),
);
const AnalysisView = lazy(() =>
  import("@/views/analysis-view").then((module) => ({
    default: module.AnalysisView,
  })),
);
const DirectivesView = lazy(() =>
  import("@/views/directives-view").then((module) => ({
    default: module.DirectivesView,
  })),
);

function Landing() {
  const navigate = useNavigate();
  const [recentProjects, setRecentProjects] = useState<string[]>(() => getRecentProjects());
  const projectSuggestions = mergeProjectSuggestions(recentProjects);
  const [projectId, setProjectId] = useState(() => recentProjects[0] ?? "");

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-10">
      <Card className="w-full max-w-[1200px] overflow-hidden rounded-tile p-0 shadow-tile">
        <div className="grid gap-0 md:grid-cols-[1.15fr_0.85fr]">
          <section className="space-y-6 bg-[radial-gradient(circle_at_top_left,rgba(14,165,233,0.18),transparent_40%),linear-gradient(180deg,rgba(248,250,252,0.95),rgba(255,255,255,0.82))] p-8 md:p-10">
            <div className="chip-pill chip-mono w-fit text-muted-foreground">
              <Radar className="h-4 w-4" />
              SecondSight
            </div>
            <div className="space-y-3">
              <h1 className="max-w-xl text-balance text-[2rem] font-semibold leading-[1.1] tracking-[-0.03em] md:text-[2.75rem]">
                Observation, analysis, and directives on one spatial surface.
              </h1>
              <p className="max-w-lg text-pretty text-base leading-7 text-muted-foreground">
                Pick a project and step through sessions, behavior flags, and active
                conventions without leaving the same dashboard plane.
              </p>
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              {[
                { icon: Binoculars, label: "Observation", body: "Drill down from session to segment to event." },
                { icon: Activity, label: "Analysis", body: "Trend lines, reports, and flag clusters." },
                { icon: FolderKanban, label: "Directives", body: "Lifecycle, source traces, deletion." },
              ].map((item) => (
                <div
                  className="rounded-chunk border border-white/80 bg-white/60 p-4 shadow-sm"
                  key={item.label}
                >
                  <item.icon className="mb-3 h-5 w-5 text-primary" />
                  <div className="mb-1 text-sm font-medium">{item.label}</div>
                  <p className="hyphens-none text-[13px] leading-5 text-muted-foreground">{item.body}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2 pt-1 text-xs text-muted-foreground">
              <span className="chip-mono">Compatible with</span>
              <span className="chip-pill">Claude Code</span>
              <span className="chip-pill">OpenCode</span>
              <span className="chip-pill">Codex CLI</span>
            </div>
          </section>
          <section className="space-y-5 p-8 md:p-10">
            <div className="space-y-2">
              <h2 className="text-xl font-semibold">Project scope</h2>
              <p className="text-sm leading-6 text-muted-foreground">
                Every API route is project-scoped. Enter the project id you want to inspect.
              </p>
            </div>
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                const nextProjectId = projectId.trim();
                if (!nextProjectId) {
                  return;
                }
                setRecentProjects(addRecentProject(nextProjectId));
                navigate(`/projects/${encodeURIComponent(nextProjectId)}/observation`);
              }}
            >
              <Input
                autoFocus
                list="landing-recent-projects"
                onChange={(event) => setProjectId(event.target.value)}
                placeholder="example-project"
                value={projectId}
              />
              <datalist id="landing-recent-projects">
                {projectSuggestions.map((id) => (
                  <option key={id} value={id} />
                ))}
              </datalist>
              <Button className="w-full justify-between" size="lg" type="submit">
                Enter dashboard
                <ArrowRight className="h-4 w-4" />
              </Button>
            </form>
            <div className="space-y-2">
              <p className="eyebrow">Recent</p>
              <div className="flex flex-wrap gap-2">
                {projectSuggestions.map((id) => (
                  <button
                    className="chip-pill chip-mono transition-colors duration-150 hover:bg-primary/5"
                    key={id}
                    onClick={() => setProjectId(id)}
                    type="button"
                  >
                    {id}
                  </button>
                ))}
              </div>
            </div>
            <p className="text-[13px] leading-6 text-muted-foreground">
              Recent project ids are kept locally in this browser — click a chip or start typing to autocomplete.
            </p>
          </section>
        </div>
      </Card>
    </main>
  );
}

function DashboardLayout() {
  const { projectId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [draftProjectId, setDraftProjectId] = useState(projectId);
  const [recentProjects, setRecentProjects] = useState<string[]>(() => getRecentProjects());
  const projectSuggestions = mergeProjectSuggestions(recentProjects);

  useEffect(() => {
    setDraftProjectId(projectId);
    if (projectId) {
      setRecentProjects(addRecentProject(projectId));
    }
  }, [projectId]);

  const navItems = [
    { to: "observation", label: "Observation", icon: Binoculars },
    { to: "analysis", label: "Analysis", icon: Activity },
    { to: "directives", label: "Directives", icon: FolderKanban },
  ];

  return (
    <main className="min-h-screen px-4 py-4 md:px-6 md:py-6">
      <div className="mx-auto flex min-h-[calc(100vh-2rem)] max-w-[1920px] flex-col gap-4">
        <header className="sticky top-4 z-30 grid gap-4 rounded-[32px] border border-white/70 bg-white/65 p-4 shadow-ambient backdrop-blur-xl md:grid-cols-[1fr_auto] md:p-5">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <Link className="font-mono uppercase tracking-[0.22em] text-primary" to="/">
                SecondSight
              </Link>
              <span>/</span>
              <span className="font-mono numeric text-foreground">{projectId}</span>
              <PollingPill />
            </div>
            <div className="flex flex-wrap gap-2">
              {navItems.map((item) => (
                <NavLink
                  className={({ isActive }) =>
                    cn(
                      "inline-flex items-center gap-2 rounded-input border px-4 py-2 text-sm transition-all duration-150",
                      isActive
                        ? "border-primary/30 bg-primary text-primary-foreground shadow-sm"
                        : "border-white/70 bg-white/60 text-foreground hover:bg-white",
                    )
                  }
                  key={item.to}
                  to={item.to}
                >
                  <item.icon className="h-4 w-4" />
                  {item.label}
                </NavLink>
              ))}
            </div>
          </div>
          <form
            className="flex flex-col gap-2 sm:flex-row"
            onSubmit={(event) => {
              event.preventDefault();
              const nextProjectId = draftProjectId.trim();
              if (!nextProjectId || nextProjectId === projectId) {
                return;
              }
              const nextLeaf = location.pathname.split("/").pop() || "observation";
              navigate(`/projects/${encodeURIComponent(nextProjectId)}/${nextLeaf}`);
            }}
          >
            <Input
              className="min-w-[220px]"
              list="switcher-recent-projects"
              onChange={(event) => setDraftProjectId(event.target.value)}
              value={draftProjectId}
            />
            <datalist id="switcher-recent-projects">
              {projectSuggestions.map((id) => (
                <option key={id} value={id} />
              ))}
            </datalist>
            <Button className="whitespace-nowrap" type="submit">
              Switch project
            </Button>
          </form>
        </header>
        <Outlet />
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border/70 px-1 py-4 text-xs text-muted-foreground">
          <span className="chip-mono">SecondSight dashboard · v0.alpha</span>
          <span>Visual tokens follow `frontend/mockups/DESIGN.md` and `preview.html`.</span>
        </footer>
      </div>
    </main>
  );
}

export function App() {
  return (
    <HashRouter>
      <Routes>
        <Route element={<Landing />} path="/" />
        <Route element={<DashboardLayout />} path="/projects/:projectId">
          <Route element={<Navigate replace to="observation" />} index />
          <Route
            element={
              <Suspense fallback={<RouteFallback />}>
                <ObservationView />
              </Suspense>
            }
            path="observation"
          />
          <Route
            element={
              <Suspense fallback={<RouteFallback />}>
                <AnalysisView />
              </Suspense>
            }
            path="analysis"
          />
          <Route
            element={
              <Suspense fallback={<RouteFallback />}>
                <DirectivesView />
              </Suspense>
            }
            path="directives"
          />
        </Route>
        <Route element={<Navigate replace to="/" />} path="*" />
      </Routes>
    </HashRouter>
  );
}

function RouteFallback() {
  return (
    <Card className="flex min-h-[420px] items-center justify-center">
      <div className="space-y-2 text-center">
        <div className="font-mono text-xs uppercase tracking-[0.22em] text-muted-foreground">
          Loading route
        </div>
        <div className="text-xl font-semibold">Hydrating the next surface</div>
      </div>
    </Card>
  );
}
