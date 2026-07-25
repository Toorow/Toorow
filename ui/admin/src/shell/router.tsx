/**
 * Lightweight deep-linkable router — Epic 42 story 42.1.
 *
 * Ratified 2026-07-24 (Jean): real deep-linkable URLs (scope + workspace +
 * section + object + tab), browser history and focus-to-H1 on intentional
 * navigation — implemented on the native History API (no heavy router lib).
 *
 * URL shape:
 *   /p/:projectId/:workspace(/:section)(/o/:objectType/:objectId(/:tab))
 * Example: /p/acme-growth/data/datastreams/o/datastream/ds_123/mapping
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
import { DEFAULT_WORKSPACE, type WorkspaceKey } from "./navigation";

export interface Route {
  projectId: string;
  workspace: WorkspaceKey;
  section: string | null;
  objectType: string | null;
  objectId: string | null;
  tab: string | null;
}

const WORKSPACE_KEYS: WorkspaceKey[] = [
  "overview",
  "analyze",
  "test",
  "data",
  "governance",
  "context",
];

function isWorkspace(v: string): v is WorkspaceKey {
  return (WORKSPACE_KEYS as string[]).includes(v);
}

export function parsePath(pathname: string, fallbackProject: string): Route {
  const parts = pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  // Expect: p / :projectId / :workspace / ...
  const route: Route = {
    projectId: fallbackProject,
    workspace: DEFAULT_WORKSPACE,
    section: null,
    objectType: null,
    objectId: null,
    tab: null,
  };
  if (parts[0] !== "p" || !parts[1]) return route;
  route.projectId = decodeURIComponent(parts[1]);
  if (parts[2] && isWorkspace(parts[2])) route.workspace = parts[2];

  const rest = parts.slice(3);
  const oIdx = rest.indexOf("o");
  const sectionParts = oIdx === -1 ? rest : rest.slice(0, oIdx);
  if (sectionParts[0]) route.section = sectionParts[0];

  if (oIdx !== -1) {
    const obj = rest.slice(oIdx + 1);
    route.objectType = obj[0] ?? null;
    route.objectId = obj[1] ? decodeURIComponent(obj[1]) : null;
    route.tab = obj[2] ?? null;
  }
  return route;
}

export function buildPath(r: Partial<Route> & { projectId: string; workspace: WorkspaceKey }): string {
  let p = `/p/${encodeURIComponent(r.projectId)}/${r.workspace}`;
  if (r.section) p += `/${r.section}`;
  if (r.objectType && r.objectId) {
    p += `/o/${r.objectType}/${encodeURIComponent(r.objectId)}`;
    if (r.tab) p += `/${r.tab}`;
  }
  return p;
}

interface RouterValue {
  route: Route;
  navigate: (to: Partial<Route>) => void;
}

const RouterContext = createContext<RouterValue | null>(null);

export function useRoute(): RouterValue {
  const ctx = useContext(RouterContext);
  if (!ctx) throw new Error("useRoute must be used within <RouterProvider>");
  return ctx;
}

export function RouterProvider({
  defaultProject,
  children,
}: {
  defaultProject: string;
  children: ReactNode;
}) {
  const [pathname, setPathname] = useState<string>(() => window.location.pathname);

  useEffect(() => {
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const route = useMemo(() => parsePath(pathname, defaultProject), [pathname, defaultProject]);

  const navigate = useCallback(
    (to: Partial<Route>) => {
      const next = buildPath({
        projectId: to.projectId ?? route.projectId,
        workspace: to.workspace ?? route.workspace,
        section: to.section ?? null,
        objectType: to.objectType ?? null,
        objectId: to.objectId ?? null,
        tab: to.tab ?? null,
      });
      if (next !== window.location.pathname) {
        window.history.pushState({}, "", next);
        setPathname(next);
      }
    },
    [route.projectId, route.workspace],
  );

  // Normalize a bare "/" into the default deep link once, so the URL is always shareable.
  useEffect(() => {
    if (window.location.pathname === "/" || window.location.pathname === "") {
      const initial = buildPath({ projectId: defaultProject, workspace: DEFAULT_WORKSPACE });
      window.history.replaceState({}, "", initial);
      setPathname(initial);
    }
  }, [defaultProject]);

  const value = useMemo<RouterValue>(() => ({ route, navigate }), [route, navigate]);
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}
