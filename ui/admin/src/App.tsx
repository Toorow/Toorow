/**
 * Admin console App — Epic 42 (IA migration, single UI, big-bang) + F-011 entry routing.
 *
 * One shell, no legacy transition. The FIRST decision the app makes is which entry
 * surface a signed-in person belongs on. Before F-011 the two onboarding screens were
 * reachable only by typing their URL, so a user with no organization fell straight into
 * the shell; now the decision is explicit and driven by the scope state.
 *
 * Entry routing (in order):
 *   1. /invite#invite=<bearer>      -> JoinOrg. Highest priority, decided from the URL
 *                                     alone: an invitation must never wait on (or need)
 *                                     the org/project fetch, and the invited user has no
 *                                     organization yet by definition.
 *   2. /onboarding | /create-org    -> CreateOrg. Deliberate routes (an operator adding
 *                                     another organization), so they keep Cancel and get
 *                                     no welcome banner.
 *   3. otherwise                    -> inside RouterProvider > ScopeProvider, <EntryGate>
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
import { CssBaseline } from "@mui/material";
import type { ReactNode } from "react";
import { RouterProvider } from "./shell/router";
import { ScopeProvider, useScope } from "./shell/scope";
import { OrgThemeProvider } from "./shell/orgTheme";
import ApplicationShell from "./shell/ApplicationShell";
import AuthGate from "./shell/AuthGate";
import JoinOrg from "./shell/pages/JoinOrg";
import CreateOrg from "./shell/pages/CreateOrg";
import "./shell/application.css";
import "./shell/pages/create-org.css";

/** An invitation link — decided from the URL alone, ahead of any scope fetch. */
function isInvitePath(p: string): boolean {
  return p.startsWith("/invite");
}

/** The explicit organization-creation routes (typed or linked deliberately). */
function isCreateOrgPath(p: string): boolean {
  return p.startsWith("/onboarding") || p.startsWith("/create-org");
}

function goHome(): void {
  // A full navigation, so the scope is refetched with the new membership.
  window.location.assign("/");
}

/**
 * Pre-shell frame: the neutral (unbranded) theme plus the CSS baseline, used by every
 * surface that renders before a project scope exists.
 */
function EntryFrame({ children }: { children: ReactNode }) {
  return (
    <OrgThemeProvider branding={null}>
      <CssBaseline />
      {children}
    </OrgThemeProvider>
  );
}

/** state === "loading": sober, labelled, and never a blank page. */
function ScopeLoading() {
  return (
    <div className="createorg-stage">
      <div className="createorg-scrim">
        <section
          className="createorg-dialog"
          role="status"
          aria-live="polite"
          aria-busy="true"
          aria-labelledby="entry-loading-title"
        >
          <header className="createorg-header">
            <div>
              <h1 id="entry-loading-title">Loading your workspace</h1>
              <p className="createorg-subtitle">
                Checking which organizations and projects you have access to.
              </p>
            </div>
          </header>
          <div className="createorg-body">
            <span className="signal-label info">
              <span className="signal-mark" />
              Loading…
            </span>
            <div className="entry-skeleton" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

/** state === "error": name the failure and offer a retry. Never a fabricated shell. */
function ScopeError() {
  return (
    <div className="createorg-stage">
      <div className="createorg-scrim">
        <section
          className="createorg-dialog"
          role="alert"
          aria-labelledby="entry-error-title"
        >
          <header className="createorg-header">
            <div>
              <h1 id="entry-error-title">We could not load your workspace</h1>
              <p className="createorg-subtitle">
                Your organizations and projects did not load, so there is nothing we can show
                you yet.
              </p>
            </div>
          </header>
          <div className="createorg-body">
            <div className="createorg-error">
              <span className="signal-label error">
                <span className="signal-mark" />
                Organizations and projects unavailable
              </span>
              <p>
                We will not show you a workspace we could not verify. Nothing was changed. Check
                your connection and try again; if this keeps happening, sign out and sign back in,
                or ask an organization owner whether your access is still active.
              </p>
            </div>
          </div>
          <footer className="createorg-footer">
            <span>Retrying reloads the console and asks for your access again.</span>
            <div className="createorg-actions">
              <button
                className="primary-button"
                type="button"
                onClick={() => window.location.reload()}
              >
                Try again
              </button>
            </div>
          </footer>
        </section>
      </div>
    </div>
  );
}

/** state === "ready": the six-workspace project shell, branded by the active org. */
function ScopedShell() {
  const scope = useScope();
  return (
    <OrgThemeProvider branding={scope.org?.branding ?? null}>
      <CssBaseline />
      <ApplicationShell />
    </OrgThemeProvider>
  );
}

/**
 * The state-dependent half of the entry routing. It lives INSIDE ScopeProvider — it is
 * the only place that may read `state`, and the only gate between a signed-in user and
 * the shell.
 */
function EntryGate() {
  const { state } = useScope();

  if (state === "loading") {
    return (
      <EntryFrame>
        <ScopeLoading />
      </EntryFrame>
    );
  }
  if (state === "error") {
    return (
      <EntryFrame>
        <ScopeError />
      </EntryFrame>
    );
  }
  if (state === "empty") {
    // No organization: the welcome surface. No Cancel — there is nowhere to go back to.
    return (
      <EntryFrame>
        <CreateOrg welcome onCreated={goHome} />
      </EntryFrame>
    );
  }
  return <ScopedShell />;
}

export default function App() {
  const path = window.location.pathname;

  if (isInvitePath(path)) {
    return (
      <AuthGate>
        <EntryFrame>
          <JoinOrg onAccepted={goHome} />
        </EntryFrame>
      </AuthGate>
    );
  }

  if (isCreateOrgPath(path)) {
    return (
      <AuthGate>
        <EntryFrame>
          <CreateOrg onCreated={goHome} onCancel={goHome} />
        </EntryFrame>
      </AuthGate>
    );
  }

  return (
    <AuthGate>
      <RouterProvider defaultProject="default">
        <ScopeProvider>
          <EntryGate />
        </ScopeProvider>
      </RouterProvider>
    </AuthGate>
  );
}
