/**
 * Scope context — Epic 42 story 42.1 (wired to the real org/project API).
 *
 * Holds the active organization and the project list, and derives the active
 * project from the URL (the router owns projectId). Organization switching and
 * project selection are surfaced by the TopBar ScopeControl. Org branding feeds
 * the OrgThemeProvider (org color tokenization).
 *
 * Data: GET /api/organizations (id, name, brand_* colors, logo_url) + GET
 * /api/projects (id, name, org_id), composed into orgs-with-projects. While the
 * fetch is in flight — or if it fails — the minimal SEED keeps the shell usable
 * so the app never renders without a scope. A project not attached to any org
 * (legacy NULL org_id) still resolves via a graceful fallback so deep links work.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
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

export interface ScopeValue {
  org: OrgRef;
  orgs: OrgRef[];
  activeProject: ProjectRef;
}

const ScopeContext = createContext<ScopeValue | null>(null);

export function useScope(): ScopeValue {
  const ctx = useContext(ScopeContext);
  if (!ctx) throw new Error("useScope must be used within <ScopeProvider>");
  return ctx;
}

/** Minimal seed used while the API loads or if it is unreachable. */
const SEED_ORGS: OrgRef[] = [
  {
    id: "toorow-core",
    name: "Toorow Core",
    branding: null,
    projects: [{ id: "default", name: "Default project" }],
  },
];

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

function brandingOf(o: ApiOrg): OrgBranding | null {
  const accent = o.brand_primary ?? undefined;
  const logoUrl = o.logo_url ?? undefined;
  if (!accent && !logoUrl) return null;
  return { accent, logoUrl };
}

/** Compose orgs + projects into the scope shape (only orgs with ≥1 project are
 *  navigable; projects with no org_id fall into a synthetic bucket so they stay
 *  reachable rather than vanishing). */
function compose(orgs: ApiOrg[], projects: ApiProject[]): OrgRef[] {
  const byOrg = new Map<string, ProjectRef[]>();
  const orphans: ProjectRef[] = [];
  for (const p of projects) {
    const ref = { id: p.id, name: p.name };
    if (p.org_id) {
      const list = byOrg.get(p.org_id) ?? [];
      list.push(ref);
      byOrg.set(p.org_id, list);
    } else {
      orphans.push(ref);
    }
  }
  const composed: OrgRef[] = orgs
    .map((o) => ({
      id: o.id,
      name: o.name,
      branding: brandingOf(o),
      projects: byOrg.get(o.id) ?? [],
    }))
    .filter((o) => o.projects.length > 0);
  if (orphans.length > 0) {
    composed.push({ id: "_unassigned", name: "Unassigned", branding: null, projects: orphans });
  }
  return composed.length > 0 ? composed : SEED_ORGS;
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
  const { route } = useRoute();
  const [fetched, setFetched] = useState<OrgRef[] | null>(null);

  useEffect(() => {
    if (orgsOverride) return; // explicit override (tests) — skip the fetch.
    let alive = true;
    (async () => {
      try {
        const [orgsRes, projRes] = await Promise.all([
          fetch(`${apiBase}/api/organizations`),
          fetch(`${apiBase}/api/projects`),
        ]);
        if (!orgsRes.ok || !projRes.ok) return;
        const orgsBody = (await orgsRes.json()) as { organizations?: ApiOrg[] };
        const projBody = (await projRes.json()) as { projects?: ApiProject[] };
        const composed = compose(orgsBody.organizations ?? [], projBody.projects ?? []);
        if (alive) setFetched(composed);
      } catch {
        /* keep the seed — the shell stays usable offline. */
      }
    })();
    return () => {
      alive = false;
    };
  }, [orgsOverride, apiBase]);

  const orgs = orgsOverride ?? fetched ?? SEED_ORGS;

  const value = useMemo<ScopeValue>(() => {
    const org =
      orgs.find((o) => o.projects.some((p) => p.id === route.projectId)) ?? orgs[0];
    const activeProject =
      org.projects.find((p) => p.id === route.projectId) ??
      org.projects[0] ?? { id: route.projectId, name: route.projectId };
    return { org, orgs, activeProject };
  }, [orgs, route.projectId]);

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}
