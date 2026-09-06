import { useEffect, useRef, useState, type ComponentType } from "react";
import {
  BarChart3,
  BookOpen,
  ChevronDown,
  Database,
  FlaskConical,
  Grid2X2,
  LogOut,
  Moon,
  MoreHorizontal,
  Network,
  Sun,
  Timer,
} from "lucide-react";
import { Avatar, AvatarFallback, AvatarImage, Badge, Button, NavBranch, NavItem, NavSubItem, ScrollArea, Switch } from "../ui";
import { WORKSPACES, type WorkspaceKey } from "./navigation";
import type { ProjectScope } from "./lastProjectScope";
import { useRoute } from "./router";
import DataTree from "./DataTree";
import { usePlatformAdmin } from "./platformAdmin";
import { useThemePreference, type ThemeMode } from "./themeMode";

type BrowserIdentity = { name?: string; email?: string; given_name?: string; family_name?: string; picture?: string };

function currentUser(): { name: string; email: string; initials: string; picture?: string } {
  const fallback = { name: "Signed out", email: "Not signed in", initials: "?" };
  const browserIdentity = sessionStorage.getItem("toorow_browser_identity");
  const token = localStorage.getItem("api_token");
  try {
    const identity = token
      ? JSON.parse(atob(token.split(".")[1] ?? "")) as BrowserIdentity
      : browserIdentity
        ? JSON.parse(browserIdentity) as BrowserIdentity
        : null;
    if (!identity) return fallback;
    const name = identity.name || [identity.given_name, identity.family_name].filter(Boolean).join(" ") || identity.email || "Account";
    const initials = name.split(/\s+/).slice(0, 2).map((part) => part[0] ?? "").join("").toUpperCase();
    return { name, email: identity.email ?? "", initials: initials || "?", picture: identity.picture };
  } catch {
    return fallback;
  }
}

const ICONS: Record<WorkspaceKey, ComponentType<{ className?: string }>> = {
  overview: Grid2X2,
  analyze: BarChart3,
  test: FlaskConical,
  data: Database,
  governance: Network,
  "context-hub": BookOpen,
};

/**
 * Appearance — one switch between day and night, and one line saying which rule
 * is in force.
 *
 * It was three pills of equal weight — `Light` `Dark` `System` — for a setting
 * with exactly two outcomes. Jean, 2026-08-11: *"le menu apparence n'est pas
 * propre, mettre un truc jour et nuit avec des icônes et un switch"*.
 *
 * The switch reads the RESOLVED scheme (`resolvedScheme()`), not the stored
 * mode, so it is never off while the page is dark — which is what `system` on a
 * dark OS did to the three pills: `System` looked selected and neither `Dark`
 * nor the switch position said what you were actually looking at.
 *
 * `system` is NOT dropped: it is the default, it follows the OS, and
 * `themeMode.ts` exists almost entirely to keep it honest. It stops being a
 * third button of equal weight and becomes what it is — the state you are in
 * until you override it, and the way back is offered only once there is
 * something to come back from.
 */
const THEME_COPY: Record<ThemeMode, string> = {
  system: "Following your system",
  light: "Always light",
  dark: "Always dark",
};

/**
 * The control itself, exported because it now has TWO hosts — the account menu
 * below and `User Account > Preferences` (`pages/AccountSettings.tsx`).
 *
 * Same reasoning as `SidebarBody`: a second declaration of this row is a second
 * thing to keep in step with `themeMode.ts`, and the first divergence would be
 * exactly the defect A.7.4 was written against — a switch in one position while
 * the page renders the other. `useThemePreference()` is a subscription to one
 * store, so the two hosts move together even when both are mounted.
 *
 * The host supplies the heading and the surrounding frame; the control supplies
 * the switch, the state sentence and the escape hatch, and nothing else.
 */
export function AppearanceControl({ className = "" }: { className?: string }) {
  const { mode, scheme, setMode } = useThemePreference();
  return (
    <div className={`grid gap-1 ${className}`}>
      {/* The sentence sits UNDER the switch, not beside it: the rail is 16rem
          and its menu is narrower still, so "Following your system" truncated to
          "Following your…" on the first capture — a state line that cannot be
          read states nothing. */}
      <div className="flex items-center gap-3">
        <Sun className={`size-4 shrink-0 ${scheme === "dark" ? "text-text-secondary" : "text-text"}`} aria-hidden />
        <Switch
          checked={scheme === "dark"}
          aria-label="Dark theme"
          onCheckedChange={(checked) => setMode(checked ? "dark" : "light")}
        />
        <Moon className={`size-4 shrink-0 ${scheme === "dark" ? "text-text" : "text-text-secondary"}`} aria-hidden />
      </div>
      <span className="text-xs text-text-secondary">{THEME_COPY[mode]}</span>
      {/* Only once there is something to come back FROM. In `system` this
          button would undo nothing, and an inert control is furniture. */}
      {mode !== "system" ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="mt-1 justify-self-start"
          onClick={() => setMode("system")}
        >
          Follow my system again
        </Button>
      ) : null}
    </div>
  );
}

/**
 * WHICH INSTANCE AM I LOOKING AT — and the honest edge of that answer.
 *
 * Nothing in the shell said. A console open on a development build and one open
 * on the deployed product were the same pixels, which is how an operator ends
 * up doing a real thing in a place they thought was a rehearsal.
 *
 * What the client can TRULY know is one fact: `import.meta.env.MODE`, the mode
 * the bundle was built in. It is not a version and not an environment name:
 *
 *   - no route serves either. `/api/health`
 *     (`server/core/platform_maintenance_api.py:382`) answers quotas, circuit
 *     breakers and mirror-sync lag for one Project — it carries no build id and
 *     no instance name. `grep -rn "api/version\|build_id" server/core` finds
 *     nothing that a browser could read;
 *   - `package.json`'s `0.1.0` is never injected: `vite.config.ts` declares no
 *     `define`, so that string does not exist in the bundle. Printing it would
 *     be printing a number nobody bumps.
 *
 * So the badge names the build mode, and **only when that mode is not
 * `production`**. On the production build the mode word adds no information a
 * person can act on, while on every other build it is the whole warning. A chip
 * that says "production" on every page of every session is furniture; one that
 * says "development" on a console that looks deployed is the fact.
 *
 * The mode is a parameter so this is testable without stubbing the module
 * environment — the default is the only value the product ever passes.
 */
export function instanceBadge(mode: string = import.meta.env.MODE): { label: string; title: string } | null {
  const built = (mode ?? "").trim();
  if (!built || built === "production") return null;
  return {
    label: `${built} build`,
    title: `This console was built in ${built} mode. It is not the production build.`,
  };
}

/**
 * Signing out of THIS window — the first of the three distinct gestures
 * `organization-settings.md:138-142` ratifies. `POST /api/auth/logout` tears up
 * the one ticket this window holds and leaves my other windows open; cutting
 * every window is a deliberate second request, `POST /api/me/sessions/revoke`,
 * offered as its own control in User Account > Session.
 *
 * Leaving by this gesture is one act wherever it is asked from — the rail's
 * account menu and User Account > Session both call THIS. Two sign-outs that
 * cleared different keys would strand half a session behind.
 */
export function signOutThisWindow(): void {
  localStorage.removeItem("api_token");
  sessionStorage.removeItem("toorow_browser_identity");
  void fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).finally(() => window.location.assign("/"));
}

/**
 * The rail's CONTENT, extracted so it can be rendered in a second host.
 *
 * Below `lg` the `<aside>` below is `hidden`, and the product then had no
 * navigation at all: no workspaces, no sections, no account menu, no way out of
 * the page except the browser's own back button. The answer is a drawer in the
 * top bar (`TopBar.tsx`, `page-structure.md §G.2.3`) — and a drawer that
 * re-declared these six rows would be a second navigation to keep in step with
 * the registry. Same component, two hosts: the `<aside>` here and the `Sheet`
 * there, each supplying its own frame and its own flex column.
 *
 * `onNavigate` is what the second host needs and the first does not: a drawer
 * that stayed open over the page it just opened would hide the answer it was
 * asked for. The rail passes nothing and nothing happens.
 */
export function SidebarBody({ scope, onNavigate }: { scope: ProjectScope; onNavigate?: () => void }) {
  const { route, navigate } = useRoute();
  const user = currentUser();
  // `null` until the server answers, and `null` renders nothing: an entry that
  // appeared a beat after every reload would read as a glitch, and one shown to a
  // person the route refuses is a dead link. See `platformAdmin.ts`.
  const isPlatformAdmin = usePlatformAdmin();
  const [menuOpen, setMenuOpen] = useState(false);
  const userRef = useRef<HTMLDivElement>(null);
  // The build the console was served from, or null when nothing honest can be
  // said about it. See `instanceBadge`.
  const build = instanceBadge();

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: MouseEvent) => {
      if (userRef.current && !userRef.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [menuOpen]);

  // The Project is passed EXPLICITLY, never inherited from the address. On
  // Organization Settings and User Account the address carries none, and
  // `nextRoute` throws "Project navigation requires exact scope" — so a rail
  // that inherited would render six rows and break on the first click.
  const openCollection = (workspace: WorkspaceKey, section: string) => {
    onNavigate?.();
    navigate({
      organizationId: scope.organizationId,
      projectId: scope.projectId,
      workspace,
      section,
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
    });
  };

  const signOut = signOutThisWindow;

  return (
    <>
      <div className="flex h-[4.5rem] items-center border-b border-divider-base px-5 dark:border-divider-medium">
        <img className="h-7 w-auto dark:hidden" src="/brand/toorow-logo-horizontal-dark.png" alt="toorow" />
        <img className="hidden h-7 w-auto dark:block" src="/brand/toorow-logo-horizontal-light.png" alt="" aria-hidden="true" />
      </div>

      <ScrollArea className="min-h-0 flex-1 px-3 py-4">
        <nav className="space-y-0.5" aria-label="Project">
          {WORKSPACES.map((workspace) => {
            // On a global surface the route still carries a default workspace
            // from `tail()`. Marking Overview active there would claim the
            // person is somewhere they are not, so nothing is current.
            const active = route.globalSurface === null && route.workspace === workspace.key;
            const Icon = ICONS[workspace.key];
            return (
              <div key={workspace.key}>
                <NavItem
                  active={active}
                  icon={<Icon aria-hidden="true" />}
                  trailing={<ChevronDown className={`size-4 transition-transform ${active ? "rotate-180" : ""}`} aria-hidden="true" />}
                  aria-expanded={active}
                  data-testid={`ws-${workspace.key}`}
                  onClick={() => openCollection(workspace.key, workspace.subnav[0].slug)}
                >
                  {workspace.label}
                </NavItem>
                {active ? (
                  <NavBranch>
                    {workspace.subnav.map((item) => {
                      const selected = route.section === item.slug;
                      return (
                        <div key={item.slug}>
                          <NavSubItem
                            active={selected}
                            data-testid={`sec-${workspace.key}-${item.slug}`}
                            onClick={() => openCollection(workspace.key, item.slug)}
                          >
                            {item.label}
                          </NavSubItem>
                          {workspace.key === "data" && item.slug === "datastreams" && selected ? <DataTree /> : null}
                        </div>
                      );
                    })}
                  </NavBranch>
                ) : null}
              </div>
            );
          })}
        </nav>
      </ScrollArea>

      <div ref={userRef} className="relative border-t border-divider-base p-3 dark:border-divider-medium">
        {build ? (
          <p className="px-2 pb-2">
            <Badge outline title={build.title} data-testid="build-badge">{build.label}</Badge>
          </p>
        ) : null}
        <div className="flex items-center gap-3 rounded-control p-2">
          <Avatar className="size-9">
            {user.picture ? <AvatarImage src={user.picture} alt="" referrerPolicy="no-referrer" /> : null}
            <AvatarFallback>{user.initials}</AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <strong className="block truncate text-sm">{user.name}</strong>
            <span className="block truncate text-xs text-text-secondary">{user.email || "My account"}</span>
          </div>
          <Button variant="ghost" size="icon" aria-label="Account menu" aria-haspopup="menu" aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)}>
            <MoreHorizontal className="size-4" />
          </Button>
        </div>
        {menuOpen ? (
          <div className="absolute bottom-[4.75rem] left-3 right-3 z-50 rounded-menu border border-divider-base bg-surface-light p-2 shadow-overlay dark:border-divider-medium dark:bg-surface-dark-elevated" role="menu">
            <p className="px-2 py-1 text-xs font-semibold text-text-secondary">Appearance</p>
            {/* The SAME control User Account > Preferences renders. One store,
                two hosts — see `AppearanceControl`. */}
            <AppearanceControl className="px-2 py-1.5" />
            {isPlatformAdmin ? (
              // The platform surface carries no Organization and no Project (migration
              // 195 declares neither), so it belongs beside the account, not inside a
              // workspace. `globalSurface: "platform"` defaults its section to "clocks".
              <Button
                type="button"
                variant="ghost"
                className="mt-2 w-full justify-start gap-2"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onNavigate?.();
                  navigate({ globalSurface: "platform" });
                }}
              >
                <Timer className="size-4" /> Platform clocks
              </Button>
            ) : null}
            <Button type="button" variant="ghost" className="mt-2 w-full justify-start gap-2" role="menuitem" onClick={signOut}>
              <LogOut className="size-4" /> Sign out
            </Button>
          </div>
        ) : null}
      </div>
    </>
  );
}

/**
 * The rail itself — the frame, and nothing else.
 *
 * Hidden below `lg` on purpose: 16rem of a 375px viewport is the page. What was
 * missing is not a narrower rail, it is a way to open the same rows on demand,
 * and that is the top bar's drawer.
 */
export default function StableSidebar({ scope }: { scope: ProjectScope }) {
  return (
    <aside className="sticky top-0 hidden h-screen min-h-[640px] flex-col border-r border-divider-base bg-surface-light text-text dark:border-divider-medium dark:bg-surface-dark dark:text-text-on-dark lg:flex">
      <SidebarBody scope={scope} />
    </aside>
  );
}