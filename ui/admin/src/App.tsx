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
import { useEffect, useState, type ReactNode } from "react";
import { RouterProvider } from "./shell/router";
import { ScopeProvider, useScope } from "./shell/scope";
import { OrgThemeProvider } from "./shell/orgTheme";
import ApplicationShell from "./shell/ApplicationShell";
import AuthGate from "./shell/AuthGate";
import JoinOrg from "./shell/pages/JoinOrg";
import CreateOrg from "./shell/pages/CreateOrg";
import ClaimInstance from "./shell/pages/ClaimInstance";
import CreateProject from "./shell/pages/CreateProject";
import { apiFetch } from "./lib/apiFetch";
import "./shell/application.css";
import "./shell/pages/create-org.css";

/** An invitation link — decided from the URL alone, ahead of any scope fetch. */
function isInvitePath(p: string): boolean {
  return p === "/invite" || p === "/invite/";
}

let inviteFragmentCapture: { locationKey: string; bearer: string } | null = null;

function takeInviteBearer(): string {
  const hash = window.location.hash || "";
  const locationKey = window.location.pathname + window.location.search;
  const bearer = hash.startsWith("#invite=") ? hash.slice("#invite=".length) : "";
  if (hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  if (bearer) {
    inviteFragmentCapture = { locationKey, bearer };
    return bearer;
  }
  if (inviteFragmentCapture?.locationKey === locationKey) {
    return inviteFragmentCapture.bearer;
  }
  return bearer;
}

function InviteEntry() {
  // The initializer runs before AuthGate mounts, so GIS never sees the bearer in the URL.
  const [bearer] = useState(takeInviteBearer);
  useEffect(() => {
    if (inviteFragmentCapture?.bearer === bearer) {
      inviteFragmentCapture = null;
    }
  }, [bearer]);
  return (
    <AuthGate>
      <EntryFrame>
        <JoinOrg token={bearer} onAccepted={goToInvitationScope} />
      </EntryFrame>
    </AuthGate>
  );
}
/** The explicit organization-creation routes (typed or linked deliberately). */
function isCreateOrgPath(p: string): boolean {
  return p === "/onboarding" || p === "/onboarding/" || p === "/create-org" || p === "/create-org/";
}

function safeServerPath(value?: string): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/";
  }
  const parsed = new URL(value, window.location.origin);
  return parsed.origin === window.location.origin ? parsed.pathname + parsed.search : "/";
}

function goToCreatedProject(projectId: string): void {
  window.location.assign(`/p/${encodeURIComponent(projectId)}/overview/getting-started`);
}

function goToCreatedScope(_orgId: string, nextUrl?: string): void {
  window.location.assign(safeServerPath(nextUrl));
}
function goToInvitationScope(nextUrl: string): void {
  window.location.assign(safeServerPath(nextUrl));
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

type EntryState = "loading" | "hosted_entry_ready" | "local_entry_ready" | "invitation_required" | "setup_required" | "identity_activation_required" | "scoped" | "error";

function EntryBlocked({
  kind,
}: {
  kind: "invitation_required" | "setup_required" | "identity_activation_required" | "scoped" | "organization_limit_reached";
}) {
  const copy =
    kind === "invitation_required"
      ? {
          title: "An invitation is required",
          detail: "Your identity has no accepted platform or organization invitation. Ask an administrator for a new invitation link.",
        }
      : kind === "setup_required"
        ? {
            title: "This instance is not claimed",
            detail: "An operator must mint and open the one-time /setup link from a trusted server shell.",
          }
        : kind === "identity_activation_required"
          ? {
              title: "Identity activation is required",
              detail: "An operator must reconcile existing access and enable canonical identity before first setup can continue.",
            }
          : kind === "organization_limit_reached"
          ? {
              title: "Organization creation is not available",
              detail:
                "You already have an active scope. Access to another organization comes from an invitation or an authorized administrator.",
            }
        : {
            title: "Your workspace is incomplete",
            detail: "Your account has scope authority but no usable project. Ask an owner to repair the organization scope.",
          };
  return (
    <div className="createorg-stage">
      <div className="createorg-scrim">
        <section className="createorg-dialog" role="alert">
          <header className="createorg-header">
            <div>
              <h1>{copy.title}</h1>
              <p className="createorg-subtitle">{copy.detail}</p>
            </div>
          </header>
        </section>
      </div>
    </div>
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
              state === "identity_activation_required" ||
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
  const scope = useScope();

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

export default function App() {
  const path = window.location.pathname;

  if (path === "/setup" || path === "/setup/") {
    return (
      <EntryFrame>
        <ClaimInstance />
      </EntryFrame>
    );
  }

  if (isInvitePath(path)) return <InviteEntry />;

  if (isCreateOrgPath(path)) {
    return (
      <AuthGate>
        <EntryFrame>
          <DirectCreateOrgEntry />
        </EntryFrame>
      </AuthGate>
    );
  }

  return (
    <AuthGate>
      <RouterProvider>
        <ScopeProvider>
          <EntryGate />
        </ScopeProvider>
      </RouterProvider>
    </AuthGate>
  );
}
