/**
 * Scope context — Epic 42 story 42.1 (wired to the real org/project API).
 *
 * Holds the active organization and the project list, and derives the active
 * project from the URL (the router owns projectId). Organization switching and
 * project selection are surfaced by the TopBar ScopeControl. Org branding feeds
 * the OrgThemeProvider (org color tokenization).
 *
 * Data: GET /api/organizations (id, name, brand_* colors, logo_url) + GET
 * /api/projects (id, name, org_id), composed into orgs-with-projects, both via
 * `apiFetch` so the bearer token is always attached. A project not attached to
 * any org (legacy NULL org_id) still resolves via a graceful fallback so deep
 * links work.
 *
 * There is NO fallback scope. Finding F-010: this provider used to seed a
 * hard-coded "Toorow Core / Default project" whenever the fetch failed, which
 * meant an unauthenticated 401 rendered a console full of an organization the
 * user did not own. The load outcome is now reported explicitly through
 * `state`, and `org` / `activeProject` are null until it is "ready" — the shell
 * must show a loading, error or onboarding surface instead of inventing data.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { apiFetch } from "../lib/apiFetch";
import type { OrgBranding } from "./orgTheme";
import { registerScopeAliases } from "./scopeAliases";
import { useRoute } from "./router";

export interface ProjectRef {
  id: string;
  name: string;
  /** The immutable, URL-safe name. Present since the address bar shows it
   *  instead of the ULID -- see `shell/scopeAliases.ts`. */
  slug?: string | null;
}

export interface OrgRef {
  id: string;
  name: string;
  slug?: string | null;
  branding: OrgBranding | null;
  projects: ProjectRef[];
}

/**
 * Load outcome of the org/project fetch.
 *   "loading" — the request is in flight.
 *   "error"   — the request failed (network, 401, 500, …).
 *   "empty"   — the request succeeded and the user has NO organization.
 *   "ready"   — the request succeeded and there is at least one organization.
 * `org` and `activeProject` are non-null only when state is "ready".
 */
export type ScopeState = "loading" | "error" | "empty" | "project_required" | "denied" | "ready";

export interface ScopeValue {
  state: ScopeState;
  org: OrgRef | null;
  orgs: OrgRef[];
  activeProject: ProjectRef | null;
  /** Re-runs the fetch — for the retry affordance of the error surface. */
  reload: () => void;
}

const ScopeContext = createContext<ScopeValue | null>(null);

export function useScope(): ScopeValue {
  const ctx = useContext(ScopeContext);
  if (!ctx) throw new Error("useScope must be used within <ScopeProvider>");
  return ctx;
}

/**
 * The scope when there is one, `null` otherwise — never throws.
 *
 * For screens that merely ENRICH themselves from the scope (resolving an id to
 * the name a person actually recognises) rather than depending on it to
 * function. Those screens must still render outside a provider: in isolation
 * tests, and in any future surface mounted before the shell. `useScope` keeps
 * throwing, deliberately — a screen that genuinely cannot work without a scope
 * should fail loudly rather than render something half-true.
 */
export function useOptionalScope(): ScopeValue | null {
  return useContext(ScopeContext);
}

interface ApiOrg {
  id: string;
  name: string;
  /** `GET /api/organizations` has always selected it (`server/core/organizations_api.py#_list_orgs`);
   *  this shape simply never read it, so no address could be readable. */
  slug?: string | null;
  brand_primary?: string | null;
  /** Story 50.5 AC17: carried through to `--viz-brand-secondary`. The API has
   *  always returned it (`ui/admin/src/orgs/types.ts`); this shape dropped it,
   *  so a branded org's second series silently fell back to a toorow default. */
  brand_secondary?: string | null;
  brand_accent?: string | null;
  logo_url?: string | null;
}
interface ApiProject {
  id: string;
  name: string;
  slug?: string | null;
  org_id?: string | null;
}

const INERT_SEED_ORG_IDS = new Set(["org_default", "org_integ-test-project"]);
const INERT_SEED_PROJECT_IDS = new Set(["default", "integ-test-project"]);

function brandingOf(o: ApiOrg): OrgBranding | null {
  const accent = o.brand_primary ?? undefined;
  const secondary = o.brand_secondary ?? undefined;
  const brandAccent = o.brand_accent ?? undefined;
  const logoUrl = o.logo_url ?? undefined;
  if (!accent && !secondary && !brandAccent && !logoUrl) return null;
  return { accent, secondary, brandAccent, logoUrl };
}

/** Compose orgs + projects into the scope shape (only orgs with ≥1 project are
 *  navigable; projects with no org_id fall into a synthetic bucket so they stay
 *  reachable rather than vanishing). An empty result stays empty — a user with
 *  no organization must be told so, not handed one. */
function compose(orgs: ApiOrg[], projects: ApiProject[]): OrgRef[] {
  const visibleOrgs = orgs.filter((org) => !INERT_SEED_ORG_IDS.has(org.id));
  const byOrg = new Map<string, ProjectRef[]>();
  const orphans: ProjectRef[] = [];
  for (const p of projects) {
    if (INERT_SEED_PROJECT_IDS.has(p.id)) continue;
    const ref = { id: p.id, name: p.name, slug: p.slug ?? null };
    if (p.org_id) {
      const list = byOrg.get(p.org_id) ?? [];
      list.push(ref);
      byOrg.set(p.org_id, list);
    } else {
      orphans.push(ref);
    }
  }
  const composed: OrgRef[] = visibleOrgs.map((o) => ({
      id: o.id,
      name: o.name,
      slug: o.slug ?? null,
      branding: brandingOf(o),
      projects: byOrg.get(o.id) ?? [],
    }));
  if (orphans.length > 0) {
    composed.push({ id: "_unassigned", name: "Unassigned", branding: null, projects: orphans });
  }
  return composed;
}

export function ScopeProvider({
  orgs: orgsOverride,
  apiBase = "",
  children,
}: {
  /** Test/override seam: when provided, the API is not fetched. */
  orgs?: OrgRef[];
  apiBase?: string;
  children: ReactNode;
}) {
  const { result, route, replace } = useRoute();
  const [loaded, setLoaded] = useState<OrgRef[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const reload = useCallback(() => {
    setLoaded(null);
    setFailed(false);
    setAttempt((n) => n + 1);
  }, []);

  // The override seam feeds the sandbox and the tests, and it skips the fetch
  // below, so without this it is the one path where the address never becomes
  // readable -- and worse, where a slug address never resolves to its id.
  useEffect(() => {
    if (orgsOverride) registerScopeAliases(orgsOverride);
  }, [orgsOverride]);

  useEffect(() => {
    if (orgsOverride) return; // explicit override (tests) — skip the fetch.
    let alive = true;
    (async () => {
      try {
        const [orgsRes, projRes] = await Promise.all([
          apiFetch(`${apiBase}/api/organizations`),
          apiFetch(`${apiBase}/api/projects`),
        ]);
        // A 401/403/5xx is a load FAILURE, never an empty scope.
        if (!orgsRes.ok || !projRes.ok) {
          if (alive) setFailed(true);
          return;
        }
        const orgsBody = (await orgsRes.json()) as { organizations?: ApiOrg[] };
        const projBody = (await projRes.json()) as { projects?: ApiProject[] };
        const composed = compose(orgsBody.organizations ?? [], projBody.projects ?? []);
        // Publish before the state lands: the router reads this registry
        // synchronously, and the address should already be readable on the
        // first render that follows.
        registerScopeAliases(composed);
        if (alive) setLoaded(composed);
      } catch {
        if (alive) setFailed(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [orgsOverride, apiBase, attempt]);

  const scopeOrgs = orgsOverride ?? loaded;
  const routedOrg = result.kind === "resolved"
    ? route.scope === "account" || route.scope === "platform"
      ? undefined
      : scopeOrgs?.find((organization) =>
          organization.id === route.organizationId &&
          (route.scope === "organization"
            || organization.projects.some((project) => project.id === route.projectId)),
        )
    : undefined;
  const firstProjectOrg = scopeOrgs?.find((organization) => organization.projects.length > 0);

  useEffect(() => {
    if (!scopeOrgs || result.kind !== "bootstrap" || !firstProjectOrg) return;
    replace({
      organizationId: firstProjectOrg.id,
      projectId: firstProjectOrg.projects[0].id,
      // Explicit: the bootstrap lands on a Project workspace, never on whatever
      // global surface the unresolved placeholder route happened to carry.
      globalSurface: null,
      globalSection: null,
      workspace: "overview",
      section: "project-overview",
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
    });
  }, [scopeOrgs, result.kind, firstProjectOrg, replace]);

  const value = useMemo<ScopeValue>(() => {
    if (result.kind === "resolved" && (route.scope === "account" || route.scope === "platform")) {
      return {
        state: "ready",
        org: null,
        orgs: scopeOrgs ?? [],
        activeProject: null,
        reload,
      };
    }
    if (!scopeOrgs) {
      return {
        state: failed ? "error" : "loading",
        org: null,
        orgs: [],
        activeProject: null,
        reload,
      };
    }
    if (scopeOrgs.length === 0) {
      return { state: "empty", org: null, orgs: scopeOrgs, activeProject: null, reload };
    }
    if (result.kind !== "bootstrap" && !routedOrg && firstProjectOrg) {
      return { state: "denied", org: null, orgs: scopeOrgs, activeProject: null, reload };
    }
    if (routedOrg) {
      const activeProject = route.scope === "project"
        ? routedOrg.projects.find((p) => p.id === route.projectId) ?? null
        : null;
      return { state: "ready", org: routedOrg, orgs: scopeOrgs, activeProject, reload };
    }
    if (firstProjectOrg) {
      return { state: "loading", org: null, orgs: scopeOrgs, activeProject: null, reload };
    }
    return {
      state: "project_required",
      org: scopeOrgs[0],
      orgs: scopeOrgs,
      activeProject: null,
      reload,
    };
  }, [scopeOrgs, failed, reload, result.kind, route, routedOrg, firstProjectOrg]);

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}
