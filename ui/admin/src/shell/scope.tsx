/**
 * Scope context — Epic 42 story 42.1 (wired to the real org/project API).
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
 * `state`, and `org` / `activeProject` are null until it is "ready" — the shell
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
import { useRoute } from "./router";

export interface ProjectRef {
  id: string;
  name: string;
}

export interface OrgRef {
  id: string;
  name: string;
  branding: OrgBranding | null;
  projects: ProjectRef[];
}

/**
 * Load outcome of the org/project fetch.
 *   "loading" — the request is in flight.
 *   "error"   — the request failed (network, 401, 500, …).
 *   "empty"   — the request succeeded and the user has NO organization.
 *   "ready"   — the request succeeded and there is at least one organization.
 * `org` and `activeProject` are non-null only when state is "ready".
 */
export type ScopeState = "loading" | "error" | "empty" | "project_required" | "ready";

export interface ScopeValue {
  state: ScopeState;
  org: OrgRef | null;
  orgs: OrgRef[];
  activeProject: ProjectRef | null;
  /** Re-runs the fetch — for the retry affordance of the error surface. */
  reload: () => void;
}

const ScopeContext = createContext<ScopeValue | null>(null);

export function useScope(): ScopeValue {
  const ctx = useContext(ScopeContext);
  if (!ctx) throw new Error("useScope must be used within <ScopeProvider>");
  return ctx;
}

interface ApiOrg {
  id: string;
  name: string;
  brand_primary?: string | null;
  logo_url?: string | null;
}
interface ApiProject {
  id: string;
  name: string;
  org_id?: string | null;
}

const INERT_SEED_ORG_IDS = new Set(["org_default", "org_integ-test-project"]);
const INERT_SEED_PROJECT_IDS = new Set(["default", "integ-test-project"]);

function brandingOf(o: ApiOrg): OrgBranding | null {
  const accent = o.brand_primary ?? undefined;
  const logoUrl = o.logo_url ?? undefined;
  if (!accent && !logoUrl) return null;
  return { accent, logoUrl };
}

/** Compose orgs + projects into the scope shape (only orgs with ≥1 project are
 *  navigable; projects with no org_id fall into a synthetic bucket so they stay
 *  reachable rather than vanishing). An empty result stays empty — a user with
 *  no organization must be told so, not handed one. */
function compose(orgs: ApiOrg[], projects: ApiProject[]): OrgRef[] {
  const visibleOrgs = orgs.filter((org) => !INERT_SEED_ORG_IDS.has(org.id));
  const byOrg = new Map<string, ProjectRef[]>();
  const orphans: ProjectRef[] = [];
  for (const p of projects) {
    if (INERT_SEED_PROJECT_IDS.has(p.id)) continue;
    const ref = { id: p.id, name: p.name };
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
  const { route, replace } = useRoute();
  const [loaded, setLoaded] = useState<OrgRef[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const reload = useCallback(() => {
    setLoaded(null);
    setFailed(false);
    setAttempt((n) => n + 1);
  }, []);

  useEffect(() => {
    if (orgsOverride) return; // explicit override (tests) — skip the fetch.
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
  const routedOrg = scopeOrgs?.find((o) => o.projects.some((p) => p.id === route.projectId));
  const firstProjectOrg = scopeOrgs?.find((o) => o.projects.length > 0);

  useEffect(() => {
    if (!scopeOrgs || routedOrg || !firstProjectOrg) return;
    replace({ projectId: firstProjectOrg.projects[0].id });
  }, [scopeOrgs, routedOrg, firstProjectOrg, replace]);

  const value = useMemo<ScopeValue>(() => {
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
    if (routedOrg) {
      const activeProject = routedOrg.projects.find((p) => p.id === route.projectId) ?? null;
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
  }, [scopeOrgs, failed, reload, route.projectId, routedOrg, firstProjectOrg]);

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}
