/**
 * The screen sandbox — renders one migrated screen on its own, so it can be
 * looked at.
 *
 * The spec's rule is that a screen is migrated only once it has been RENDERED
 * and LOOKED AT, and compiling proves nothing about appearance. Until now that
 * was impossible without a signed-in session and a live API: every screen sits
 * behind `AuthGate` and `ScopeProvider`, so `ConnectorsCatalog` and
 * `RegressionRuns` were rewired onto the library months ago and nobody has
 * ever seen either of them.
 *
 * This route renders a screen directly, with no auth and no shell. Its data
 * comes from wherever the caller decides: in a browser the API calls simply
 * fail and the screen shows its own error surface — which is itself worth
 * seeing — and under Playwright the requests are intercepted and answered with
 * a fixture, which is how the migration checks each screen.
 *
 *     /debug/screen?name=ConnectorsCatalog
 *
 * Development builds only; the branch is dropped from the bundle. It renders
 * no chrome of its own beyond the page background, because the point is to
 * judge the screen and not the frame around it.
 */
import { lazy, Suspense } from "react";
import type * as React from "react";
import { TooltipProvider, Toaster } from "../ui";
import DataScreenSandbox, { DATA_SANDBOX_SCREENS } from "./DataScreenSandbox";
import "../styles/theme.css";
import "../styles/console.css";

/**
 * Every screen that has been rewired onto the component library. A screen
 * joins this list when its migration starts, and it is how the batch gets
 * looked at before it is called done.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- every screen
// takes a different prop shape; the sandbox passes one stub and does not care.
type AnyScreen = React.ComponentType<any>;

const SCREENS: Record<string, React.LazyExoticComponent<AnyScreen>> = {
  ConnectorsCatalog: lazy(() => import("./pages/ConnectorsCatalog")),
  RegressionRuns: lazy(() => import("./pages/RegressionRuns")),
  DatastreamPreconfiguration: lazy(() => import("../datastreams/preconfiguration/DatastreamPreconfigurationPreview")),
  PlatformClocks: lazy(() => import("./pages/PlatformClocks")),
  DatastreamWorkbench: lazy(() => import("./DatastreamWorkbenchSandbox")),
  // Governance screens
  GovernanceMasterData: lazy(() => import("./GovernanceSandbox").then((m) => ({ default: m.GovernanceMasterData }))),
  GovernanceSemanticModel: lazy(() => import("./GovernanceSandbox").then((m) => ({ default: m.GovernanceSemanticModel }))),
  GovernanceCommonKeys: lazy(() => import("./GovernanceSandbox").then((m) => ({ default: m.GovernanceCommonKeys }))),
  GovernanceRelationship: lazy(() => import("./GovernanceSandbox").then((m) => ({ default: m.GovernanceRelationship }))),
  GovernanceControlsQuality: lazy(() => import("./GovernanceSandbox").then((m) => ({ default: m.GovernanceControlsQuality }))),
  GovernanceEvidence: lazy(() => import("./GovernanceSandbox").then((m) => ({ default: m.GovernanceEvidence }))),
  ProjectOverview: lazy(() => import("./pages/ProjectOverview")),
  // Project settings had no address here, so its three tabs had never been
  // looked at in a browser — which is how a section header indented past its own
  // cards, an action button that moved with the length of the prose next to it,
  // and a tab band flush against its own edge all survived a design pass.
  // `?section=` picks the tab, the way `DatastreamWorkbench` takes `?tab=`.
  ProjectSettings: lazy(() => import("./pages/ProjectSettings")),
  // Analyze and Test. Seven screens that no design pass could see: the review of
  // `analyze/explore` could check nothing visual because none of this family was
  // registered here. `Explore` throws outside a RouterProvider, which is why the
  // whole family was skipped rather than wired -- `AnalyzeSandbox` supplies it,
  // exactly as `GovernanceSandbox` does for its four.
  AnalyzeExplore: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.AnalyzeExplore }))),
  AnalyzeReports: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.AnalyzeReports }))),
  AnalyzeReportWorkbench: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.AnalyzeReportWorkbench }))),
  AnalyzeNotebooks: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.AnalyzeNotebooks }))),
  AnalyzeRenders: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.AnalyzeRenders }))),
  TestGoldenQuestions: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.TestGoldenQuestions }))),
  TestRegressionRuns: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.TestRegressionRuns }))),
  TestWidgetFeedback: lazy(() => import("./AnalyzeSandbox").then((m) => ({ default: m.TestWidgetFeedback }))),
  // The shell itself. Every screen here could be looked at; the rail that frames
  // all of them could not, which is how it stayed wrong for a whole migration.
  ShellSidebar: lazy(() => import("./ShellSandbox").then((m) => ({ default: m.ShellSidebar }))),
  ShellScopeSurface: lazy(() => import("./ShellSandbox").then((m) => ({ default: m.ShellScopeSurface }))),
  // The Context Hub. Five surfaces, and NONE of them was registered here --
  // exactly the hole the Analyze family had, one workspace over. It is why the
  // real-browser pass the control note asks for (§B.3: the ELK graph, the
  // drawers, the timeline) could not be run at all: there was no address to
  // open. `scripts/context_hub_browser_pass.py` drives these five.
  ContextKnowledgeGraph: lazy(() => import("../connaissances/ContextHubSandbox").then((m) => ({ default: m.ContextKnowledgeGraph }))),
  ContextKnowledgeLibrary: lazy(() => import("../connaissances/ContextHubSandbox").then((m) => ({ default: m.ContextKnowledgeLibrary }))),
  ContextSkillsRegistry: lazy(() => import("../connaissances/ContextHubSandbox").then((m) => ({ default: m.ContextSkillsRegistry }))),
  ContextObjectWorkbench: lazy(() => import("../connaissances/ContextHubSandbox").then((m) => ({ default: m.ContextObjectWorkbench }))),
  ContextAiPath: lazy(() => import("../connaissances/ContextHubSandbox").then((m) => ({ default: m.ContextAiPath }))),
};

export const SANDBOX_SCREENS = [...Object.keys(SCREENS), ...DATA_SANDBOX_SCREENS];

export default function ScreenSandbox() {
  const params = new URLSearchParams(window.location.search);
  const name = params.get("name") ?? "";
  const dark = params.get("theme") === "dark";
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.setAttribute("data-color-scheme", dark ? "dark" : "light");
  document.documentElement.setAttribute("data-mui-color-scheme", dark ? "dark" : "light");
  const Screen = SCREENS[name];
  const isDataScreen = DATA_SANDBOX_SCREENS.includes(name);

  if (!Screen && !isDataScreen) {
    return (
      <div className="min-h-screen bg-background-light p-10 font-primary text-body text-text dark:bg-background-dark">
        <h1 className="m-0 font-display text-h1 font-h1">Screen sandbox</h1>
        <p className="mt-2 text-body text-text-secondary">
          Add <code className="font-mono">?name=</code> and one of these:
        </p>
        <ul className="mt-3 flex list-none flex-col gap-1 p-0">
          {SANDBOX_SCREENS.map((key) => (
            <li key={key}>
              <a className="font-label text-primary underline-offset-2 hover:underline" href={`?name=${key}`}>
                {key}
              </a>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <TooltipProvider>
      {/* The page background and the base typography, which the shell sets on
          `body` and this route never loads. Without them a screen is judged in
          the wrong typeface — the same defect the component sheet had. */}
      {/* The shell's gutter, to the pixel — `px-page-gutter pt-8 pb-page-gutter`.
          A screen judged here and shipped there must sit at the same distance
          from the edge in both, or the sandbox certifies an alignment the
          console does not have. */}
      <div className="min-h-screen bg-background-light px-page-gutter pt-8 pb-page-gutter font-primary text-body text-text dark:bg-background-dark">
        <Suspense fallback={<p className="text-body text-text-secondary">Loading the screen…</p>}>
          {isDataScreen ? <DataScreenSandbox name={name} /> : Screen ? (
            <Screen
              projectId="proj_EXAMPLE"
              datastreamId="ds_EXAMPLE"
              section={params.get("section") ?? undefined}
            />
          ) : null}
        </Suspense>
      </div>
      <Toaster />
    </TooltipProvider>
  );
}
