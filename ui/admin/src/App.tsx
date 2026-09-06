/**
 * Admin console App — Epic 42 (IA migration, single UI, big-bang) + F-011 entry routing.
 *
 * One shell, no legacy transition. The FIRST decision the app makes is which entry
 * surface a signed-in person belongs on. Before F-011 the two onboarding screens were
 * reachable only by typing their URL, so a user with no organization fell straight into
 * the shell; now the decision is explicit and driven by the scope state.
 *
 * Entry routing (in order):
 * `RouterProvider` wraps ALL of them since 2026-08-31: every entry surface now renders
 * the rail (`page-structure.md §A.7.1` — "the rail stays on every surface"), and the rail
 * is a router consumer. The branch itself is a router consumer for the same reason.
 *
 *   1. /invite#invite=<bearer>      -> JoinOrg. Highest priority, decided from the URL
 *                                     alone: an invitation must never wait on (or need)
 *                                     the org/project fetch, and the invited user has no
 *                                     organization yet by definition.
 *   2. /onboarding | /create-org    -> CreateOrg. Deliberate routes (an operator adding
 *                                     another organization), so they keep Cancel and get
 *                                     no welcome banner.
 *   3. otherwise                    -> inside ScopeProvider, <EntryGate>
 *                                     dispatches on useScope().state:
 *                                       loading -> a sober loading surface (never a blank
 *                                                  page)
 *                                       error   -> an explicit failure + Try again. We do
 *                                                  NOT fall through to the shell as if
 *                                                  everything were fine (F-010).
 *                                       empty   -> the "Welcome to toorow" surface with
 *                                                  organization creation (Jean, F-011).
 *                                       ready   -> the project shell, as before.
 *
 * Scope contract (owned by shell/scope.tsx — read only, never assumed away here):
 *   state "loading" | "error" | "empty" | "ready"; `org` / `activeProject` are only
 *   meaningful when state === "ready" and are read nowhere else in this file.
 */
import { useEffect, useState } from "react";
import { RouterProvider, useRoute } from "./shell/router";
import { ScopeProvider, useScope } from "./shell/scope";
import { OrgThemeProvider } from "./shell/orgTheme";
import ApplicationShell from "./shell/ApplicationShell";
import AuthGate from "./shell/AuthGate";
import JoinOrg from "./shell/pages/JoinOrg";
import CreateOrg from "./shell/pages/CreateOrg";
import ClaimInstance from "./shell/pages/ClaimInstance";
import CreateProject from "./shell/pages/CreateProject";
import RouteState from "./shell/RouteState";
import { Toaster } from "./ui";
import { apiFetch } from "./lib/apiFetch";
// AD-42 : les decisions d'URL et les surfaces d'entree ont leur fichier.
// Ce qui reste ici est la COMPOSITION, et elle seule.
import {
  goHome,
  goToCreatedProject,
  goToCreatedScope,
  goToInvitationScope,
  isCreateOrgPath,
  isInvitePath,
  releaseInviteBearer,
  takeInviteBearer,
} from "./shell/entryPaths";
import { EntryBlocked, EntryFrame, EntryShell, ScopeError, ScopeLoading, type EntryState } from "./shell/EntryStates";
// Tailwind + the generated @theme. Must come first: the layered
// utilities have to be able to lose against the legacy sheets that
// have not been migrated yet.
import "./styles/theme.css";
// The target sheet, loaded after the theme and before the legacy one so a
// migrated screen can win without `!important`. It is empty on purpose: a rule
// only enters it when a migrated screen needs it and no component covers it.
import "./styles/console.css";
function InviteEntry() {
  // The initializer runs before AuthGate mounts, so GIS never sees the bearer in the URL.
  const [bearer] = useState(takeInviteBearer);
  useEffect(() => {
    releaseInviteBearer(bearer);
  }, [bearer]);
  return (
    <AuthGate>
      <EntryShell>
        <JoinOrg token={bearer} onAccepted={goToInvitationScope} />
      </EntryShell>
    </AuthGate>
  );
}
function useEntryState(): EntryState {
  const [entry, setEntry] = useState<EntryState>("loading");
  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/entry-state", { method: "GET", cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("entry state unavailable");
        const body = (await response.json()) as { state?: EntryState };
        const state = body.state;
        if (!cancelled) {
          setEntry(
            state === "hosted_entry_ready" ||
              state === "invitation_required" ||
              state === "setup_required" ||
              state === "local_entry_ready" ||
              state === "scoped"
              ? state
              : "error",
          );
        }
      })
      .catch(() => {
        if (!cancelled) setEntry("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return entry;
}

function EmptyEntryGate() {
  const entry = useEntryState();
  if (entry === "loading") return <ScopeLoading />;
  if (entry === "error") return <ScopeError />;
  if (entry === "hosted_entry_ready") return <CreateOrg welcome onCreated={goToCreatedScope} />;
  if (entry === "local_entry_ready") {
    return <CreateOrg welcome creationMode="organization-only" onCreated={goToCreatedScope} />;
  }
  return <EntryBlocked kind={entry} />;
}

function DirectCreateOrgEntry() {
  const entry = useEntryState();
  if (entry === "loading") return <ScopeLoading />;
  if (entry === "error") return <ScopeError />;
  if (entry === "hosted_entry_ready") {
    return <CreateOrg onCreated={goToCreatedScope} onCancel={goHome} />;
  }
  if (entry === "local_entry_ready") {
    return (
      <CreateOrg creationMode="organization-only" onCreated={goToCreatedScope} onCancel={goHome} />
    );
  }
  return (
    <EntryBlocked kind={entry === "scoped" ? "organization_limit_reached" : entry} />
  );
}
/** state === "ready": the six-workspace project shell, branded by the active org. */
function ScopedShell() {
  const scope = useScope();
  return (
    <OrgThemeProvider branding={scope.org?.branding ?? null}>
      <ApplicationShell />
      {/* One toaster for the whole console: a screen dispatches with notify(),
          it never mounts its own. */}
      <Toaster />
    </OrgThemeProvider>
  );
}

/**
 * The state-dependent half of the entry routing. It lives INSIDE ScopeProvider — it is
 * the only place that may read `state`, and the only gate between a signed-in user and
 * the shell.
 */
function EntryGate() {
  const scope = useScope();
  const { result } = useRoute();

  if (result.kind === "unknown") {
    return (
      <EntryFrame>
        <RouteState kind="unknown" reason={result.reason} onBackToOverview={() => window.location.assign("/")} />
      </EntryFrame>
    );
  }
  if (scope.state === "denied") {
    return (
      <EntryFrame>
        <RouteState kind="denied" onBackToOverview={() => window.location.assign("/")} />
      </EntryFrame>
    );
  }


  if (scope.state === "loading") {
    return (
      <EntryFrame>
        <ScopeLoading />
      </EntryFrame>
    );
  }
  if (scope.state === "error") {
    return (
      <EntryFrame>
        <ScopeError />
      </EntryFrame>
    );
  }
  if (scope.state === "empty") {
    return (
      <EntryFrame>
        <EmptyEntryGate />
      </EntryFrame>
    );
  }
  if (scope.state === "project_required" && scope.org) {
    return (
      <EntryFrame>
        <CreateProject org={scope.org} onCreated={goToCreatedProject} />
      </EntryFrame>
    );
  }

  return <ScopedShell />;
}

/**
 * Which entry surface the address names — and why it is read through the router.
 *
 * The three entry routes now render INSIDE the rail (`EntryFrame`, applying
 * `page-structure.md §A.7.1`), and a rail navigates with `pushState`. A branch
 * decided from a single `window.location` read taken at mount would leave the
 * address changing under a screen that never re-read it: the rail's six rows
 * would move the URL and nothing else — a dead end worse than the missing menu
 * A.7.1 was written against.
 *
 * `useRoute()` is consumed here for its SUBSCRIPTION, not its value. The router
 * re-renders every consumer on each address change, and the pathname is read on
 * that render. It is also why `RouterProvider` now wraps the whole app rather
 * than only the scoped branch: the rail needs it, and a provider that exists on
 * four addresses out of five is a provider a component cannot rely on.
 */
function AppSurface() {
  useRoute();
  const path = window.location.pathname;

  if (path === "/setup" || path === "/setup/") {
    return (
      <EntryShell>
        <ClaimInstance />
      </EntryShell>
    );
  }

  if (isInvitePath(path)) return <InviteEntry />;

  if (isCreateOrgPath(path)) {
    return (
      <AuthGate>
        <EntryShell>
          <DirectCreateOrgEntry />
        </EntryShell>
      </AuthGate>
    );
  }

  return (
    <AuthGate>
      <ScopeProvider>
        <EntryGate />
      </ScopeProvider>
    </AuthGate>
  );
}

export default function App() {
  return (
    <RouterProvider>
      <AppSurface />
    </RouterProvider>
  );
}
