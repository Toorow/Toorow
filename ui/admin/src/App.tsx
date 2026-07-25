/**
 * Admin console App — Epic 42 (IA migration, single UI, big-bang).
 *
 * One shell, no legacy transition. Two pre-shell onboarding entries live OUTSIDE
 * the six-workspace project shell (they run before a project scope exists):
 *   - /invite#invite=<bearer>  -> JoinOrg (accept an invitation to an existing org)
 *   - /onboarding | /create-org -> CreateOrg (create one when you have no org)
 * Everything else renders the project shell (RouterProvider > ScopeProvider >
 * OrgThemeProvider > ApplicationShell).
 */
import { CssBaseline } from "@mui/material";
import { RouterProvider } from "./shell/router";
import { ScopeProvider, useScope } from "./shell/scope";
import { OrgThemeProvider } from "./shell/orgTheme";
import ApplicationShell from "./shell/ApplicationShell";
import JoinOrg from "./shell/pages/JoinOrg";
import CreateOrg from "./shell/pages/CreateOrg";
import AuthGate from "./shell/AuthGate";

function isOnboardingPath(p: string): boolean {
  return p.startsWith("/invite") || p.startsWith("/onboarding") || p.startsWith("/create-org");
}

/** Pre-shell onboarding: invitation acceptance (Join) or org creation (Create). */
function OnboardingApp() {
  const path = window.location.pathname;
  const toHome = () => {
    window.location.assign("/");
  };
  const screen = path.startsWith("/invite") ? (
    <JoinOrg onAccepted={toHome} />
  ) : (
    <CreateOrg onCreated={toHome} onCancel={toHome} />
  );
  return (
    <OrgThemeProvider branding={null}>
      <CssBaseline />
      {screen}
    </OrgThemeProvider>
  );
}

function ScopedShell() {
  const { org } = useScope();
  return (
    <OrgThemeProvider branding={org.branding}>
      <CssBaseline />
      <ApplicationShell />
    </OrgThemeProvider>
  );
}

export default function App() {
  const inner = isOnboardingPath(window.location.pathname) ? (
    <OnboardingApp />
  ) : (
    <RouterProvider defaultProject="default">
      <ScopeProvider>
        <ScopedShell />
      </ScopeProvider>
    </RouterProvider>
  );
  // Per-user Google Sign-In: populates localStorage.api_token with a Google ID
  // token (JWT) the connector verifies in TOOROW_AUTH_MODE=oauth.
  return <AuthGate>{inner}</AuthGate>;
}
