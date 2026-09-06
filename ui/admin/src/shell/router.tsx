/** Native History API transport for canonical Project, Organization and Account routes. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { DEFAULT_SECTION, DEFAULT_WORKSPACE, defaultLens, defaultObjectTab, findObjectContract, findSection, sectionOwnsAction, sectionOwnsLens, sectionQueryContract, validateSectionQuery, type WorkspaceKey } from "./navigation";
import { onScopeAliasesChange, organizationIdFor, organizationSegmentFor, projectIdFor, projectSegmentFor } from "./scopeAliases";
import { conditionalTabIsAddressable, onCapabilityTabsChange } from "./capabilityTabs";

export type ProjectSettingsSection = "general" | "capabilities" | "changes" | "ai";
export type ProjectAccessSection = "people" | "handoffs";
export type OrganizationSettingsSection = "general" | "members" | "credentials" | "account-exposure" | "data-access" | "mcp-hosts" | "plan" | "ai" | "actions";
export type AccountSection = "profile" | "authorizations" | "actions";
/** The deployment's own surfaces. No org and no project: a platform clock
 *  serves every organization at once -- that is migration 195's scope
 *  decision, and a tenant-bound route type would contradict it. */
export type PlatformSection = "clocks";
export type GlobalSurface = "project-settings" | "project-access" | "organization-settings" | "account" | "getting-started" | "platform";
export type GlobalSection = ProjectSettingsSection | ProjectAccessSection | OrganizationSettingsSection | AccountSection | PlatformSection | "journey";

interface RouteTail {
  workspace: WorkspaceKey;
  section: string;
  /** Collection lens. Exclusive with an object: a lens selects WHAT the
   *  collection lists, an object route has already left the collection. */
  lens: string | null;
  objectType: string | null;
  objectId: string | null;
  tab: string | null;
  versionId: string | null;
  /** An exact evidence target inside an object tab (Story 50.2 AC5).
   *
   *  A Result lens addresses one datum: `/tab/data/evidence/{evidence_id}`.
   *  It is a PATH tail rather than section query state because it belongs to one
   *  object's one lens — carried as `?evidence=` it would survive leaving the
   *  Result and point at a datum of something else.
   *
   *  Optional in the type on purpose: every route literal in the shell and in
   *  the existing tests predates it, and making it required would rewrite files
   *  three other sessions are holding right now to add `evidenceId: null`.
   *  Absent and `null` mean the same thing, and every read below normalizes. */
  evidenceId?: string | null;
  action: string | null;
  /** URL-stable query state the SECTION declares (Story 49.3). Validated on
   *  the way in and rebuilt in declaration order on the way out, so the
   *  address a person copies is the address that reopens what they were
   *  looking at. A section that declares nothing carries nothing. */
  query: Record<string, string>;
}
export type ProjectWorkspaceRoute = RouteTail & { scope: "project"; organizationId: string; projectId: string; globalSurface: null; globalSection: null };
export type ProjectGlobalRoute = RouteTail & { scope: "project"; organizationId: string; projectId: string; globalSurface: "project-settings" | "project-access" | "getting-started"; globalSection: ProjectSettingsSection | ProjectAccessSection | "journey" };
export type OrganizationGlobalRoute = RouteTail & { scope: "organization"; organizationId: string; projectId: null; globalSurface: "organization-settings"; globalSection: OrganizationSettingsSection };
export type AccountGlobalRoute = RouteTail & { scope: "account"; organizationId: null; projectId: null; globalSurface: "account"; globalSection: AccountSection };
export type PlatformGlobalRoute = RouteTail & { scope: "platform"; organizationId: null; projectId: null; globalSurface: "platform"; globalSection: PlatformSection };
export type CanonicalRoute = ProjectWorkspaceRoute | ProjectGlobalRoute | OrganizationGlobalRoute | AccountGlobalRoute | PlatformGlobalRoute;
export type Route = CanonicalRoute;
export type RouteResult =
  | { kind: "bootstrap"; pathname: "/" }
  | { kind: "resolved"; route: CanonicalRoute }
  | { kind: "unknown"; pathname: string; reason: string }
  | { kind: "denied"; pathname: string; reason: string }
  | { kind: "stale"; pathname: string; reason: string; route?: CanonicalRoute };

const EMPTY_ROUTE: AccountGlobalRoute = { scope: "account", organizationId: null, projectId: null, workspace: DEFAULT_WORKSPACE, section: DEFAULT_SECTION, lens: null, objectType: null, objectId: null, tab: null, versionId: null, evidenceId: null, action: null, query: {}, globalSurface: "account", globalSection: "profile" };

/** Which tabs of one object contract may carry a `/version/{id}` or an
 *  `/evidence/{id}` tail.
 *
 *  Before Story 50.2 the router hardcoded one answer: a version hangs from
 *  `versions` and from nowhere else. That was right for Governance and wrong for
 *  Analyze, where the Query Spec's *only* tab is `query` and it is the one that
 *  pins an immutable version. So the capability moved from a literal in this
 *  file to a declaration on the contract — and the historical rule is kept as
 *  the fallback, so a contract that declares nothing behaves exactly as before
 *  rather than silently losing its version routes.
 *
 *  The two capabilities are separate lists because they are different questions:
 *  a Query tab pins WHICH INTENT, a Result lens points at WHICH DATUM. A single
 *  "has a tail" flag would let `/tab/query/evidence/{id}` parse. */
type TailContract = { tabs?: readonly string[]; versionTabs?: readonly string[]; evidenceTabs?: readonly string[] };
function versionBearingTabs(contract: TailContract): readonly string[] {
  if (contract.versionTabs) return contract.versionTabs;
  return contract.tabs?.includes("versions") ? ["versions"] : [];
}
function evidenceBearingTabs(contract: TailContract): readonly string[] {
  return contract.evidenceTabs ?? [];
}

/** Does the contract declare this tab, AND may this Project open it?
 *
 *  THE CONTRACT BECAME CAPABILITY-AWARE (story 58.6, arbitrage 5). A tab a
 *  capability opens is declared like any other — otherwise it would be
 *  unreachable when the capability IS on — and refused here when the capability
 *  is measured off. Hiding the tab and leaving its address alive would land a
 *  typed URL on a panel explaining the extinction, and a panel explaining the
 *  extinction is a panel: exactly what « ni onglet, ni panneau » forbids.
 *
 *  `Placements` will ask this same question. It is asked in one place. */
function tabIsAddressable(
  contract: TailContract,
  tab: string,
  objectType: string | null,
  projectId: string | null,
): boolean {
  if (!contract.tabs?.includes(tab)) return false;
  return conditionalTabIsAddressable(objectType, tab, projectId);
}
const PROJECT_SETTINGS = new Set<ProjectSettingsSection>(["general", "capabilities", "changes", "ai"]);
const PROJECT_ACCESS = new Set<ProjectAccessSection>(["people", "handoffs"]);
/** `ai` is the ORGANIZATION twin of `project-settings/ai` (story 75-4). The
 *  cascade it edits is PLATFORM > ORG > PROJECT, so the scope in the middle has
 *  to be addressable or the org layer can only be written through the API. */
const ORGANIZATION_SETTINGS = new Set<OrganizationSettingsSection>(["general", "members", "credentials", "account-exposure", "data-access", "mcp-hosts", "plan", "ai", "actions"]);
const ACCOUNT = new Set<AccountSection>(["profile", "authorizations", "actions"]);
const PLATFORM = new Set<PlatformSection>(["clocks"]);
function unknown(pathname: string, reason: string): RouteResult { return { kind: "unknown", pathname, reason }; }
function decode(value: string): string | null { try { const decoded = decodeURIComponent(value); return decoded.length > 0 ? decoded : null; } catch { return null; } }
function tail(): RouteTail { return { workspace: DEFAULT_WORKSPACE, section: DEFAULT_SECTION, lens: null, objectType: null, objectId: null, tab: null, versionId: null, evidenceId: null, action: null, query: {} }; }

/** Parse a `?a=b` string into a plain record. Repeated keys keep the FIRST
 *  value: honouring the last one silently lets an appended parameter override
 *  the one the person actually shared. */
function parseSearch(search: string): Record<string, string> {
  const raw: Record<string, string> = {};
  for (const [key, value] of new URLSearchParams(search)) {
    if (!(key in raw)) raw[key] = value;
  }
  return raw;
}

/** Rebuild the query string in DECLARATION order, so two addresses carrying the
 *  same state are the same string and a shared link is stable. */
function buildSearch(query: Record<string, string>): string {
  const entries = Object.entries(query).filter(([, value]) => value !== "");
  if (entries.length === 0) return "";
  const params = new URLSearchParams();
  for (const [key, value] of entries) params.append(key, value);
  return "?" + params.toString();
}

export function parsePath(pathname: string, search = ""): RouteResult {
  if (pathname === "/" || pathname === "") return { kind: "bootstrap", pathname: "/" };
  if (!pathname.startsWith("/") || pathname.endsWith("/") || pathname.includes("//")) return unknown(pathname, "Malformed route shape");
  const parts = pathname.slice(1).split("/");
  if (parts[0] === "account") {
    if (parts.length > 2) return unknown(pathname, "Unknown account route shape");
    const globalSection = (parts[1] ?? "profile") as AccountSection;
    if (!ACCOUNT.has(globalSection)) return unknown(pathname, "Unknown account section");
    return { kind: "resolved", route: { ...tail(), scope: "account", organizationId: null, projectId: null, globalSurface: "account", globalSection } };
  }
  if (parts[0] === "platform") {
    if (parts.length > 2) return unknown(pathname, "Unknown platform route shape");
    const globalSection = (parts[1] ?? "clocks") as PlatformSection;
    if (!PLATFORM.has(globalSection)) return unknown(pathname, "Unknown platform section");
    return { kind: "resolved", route: { ...tail(), scope: "platform", organizationId: null, projectId: null, globalSurface: "platform", globalSection } };
  }
  if (parts[0] !== "org") return unknown(pathname, "Expected an organization route");
  // The address may carry the slug or the id; the route always carries the id.
  const organizationSegment = decode(parts[1] ?? "");
  if (!organizationSegment) return unknown(pathname, "Invalid organization identifier");
  const organizationId = organizationIdFor(organizationSegment);
  if (parts[2] === "settings") {
    if (parts.length > 4) return unknown(pathname, "Unknown organization settings route shape");
    const globalSection = (parts[3] ?? "general") as OrganizationSettingsSection;
    if (!ORGANIZATION_SETTINGS.has(globalSection)) return unknown(pathname, "Unknown organization settings section");
    return { kind: "resolved", route: { ...tail(), scope: "organization", organizationId, projectId: null, globalSurface: "organization-settings", globalSection } };
  }
  if (parts[2] !== "project") return unknown(pathname, "Expected a Project route");
  const projectSegment = decode(parts[3] ?? "");
  if (!projectSegment) return unknown(pathname, "Invalid Project identifier");
  const projectId = projectIdFor(projectSegment);
  if (parts[4] === "settings") {
    if (parts.length > 6) return unknown(pathname, "Unknown project settings route shape");
    const globalSection = (parts[5] ?? "general") as ProjectSettingsSection;
    if (!PROJECT_SETTINGS.has(globalSection)) return unknown(pathname, "Unknown project settings section");
    return { kind: "resolved", route: { ...tail(), scope: "project", organizationId, projectId, globalSurface: "project-settings", globalSection } };
  }
  if (parts[4] === "access") {
    if (parts.length > 6) return unknown(pathname, "Unknown Project Access route shape");
    const globalSection = (parts[5] ?? "people") as ProjectAccessSection;
    if (!PROJECT_ACCESS.has(globalSection)) return unknown(pathname, "Unknown Project Access section");
    return { kind: "resolved", route: { ...tail(), scope: "project", organizationId, projectId, globalSurface: "project-access", globalSection } };
  }
  if (parts[4] === "getting-started") {
    if (parts.length !== 5) return unknown(pathname, "Unknown Getting Started route shape");
    return { kind: "resolved", route: { ...tail(), scope: "project", organizationId, projectId, globalSurface: "getting-started", globalSection: "journey" } };
  }
  if (parts.length < 6) return unknown(pathname, "Expected a workspace section");
  const workspace = parts[4] as WorkspaceKey;
  const section = parts[5];
  if (!findSection(workspace, section)) return unknown(pathname, "Unknown workspace or section");
  // The section declares which parameters it owns; everything else is dropped
  // rather than carried into an address that promises state nothing honours.
  const { query } = validateSectionQuery(workspace, section, parseSearch(search));
  const route: ProjectWorkspaceRoute = { scope: "project", organizationId, projectId, workspace, section, lens: null, objectType: null, objectId: null, tab: null, versionId: null, evidenceId: null, action: null, query, globalSurface: null, globalSection: null };
  const rest = parts.slice(6);
  // A bare collection route canonicalizes to the DECLARED default lens. The
  // provider's replaceState turns that into the shareable address; a lens the
  // registry does not declare stays Unknown and is never repaired into this one.
  if (rest.length === 0) { route.lens = defaultLens(workspace, section); return { kind: "resolved", route }; }
  // A URL-stable collection lens: /:workspace/:section/lens/:lensSlug.
  if (rest[0] === "lens") {
    if (rest.length !== 2) return unknown(pathname, "Unknown collection lens route shape");
    const lensValue = decode(rest[1]);
    if (!lensValue || !sectionOwnsLens(workspace, section, lensValue)) return unknown(pathname, "Unknown collection lens");
    route.lens = lensValue;
    return { kind: "resolved", route };
  }
  // A collection-level contextual action: /:workspace/:section/action/:slug.
  if (rest[0] === "action") {
    if (rest.length !== 2 || !sectionOwnsAction(workspace, section, rest[1])) {
      return unknown(pathname, "Unknown collection action");
    }
    route.action = rest[1];
    return { kind: "resolved", route };
  }
  if (rest[0] !== "object" || rest.length < 3) return unknown(pathname, "Unknown trailing route shape");
  const objectType = rest[1]; const objectId = decode(rest[2]); const contract = findObjectContract(workspace, section, objectType);
  if (!contract || !objectId) return unknown(pathname, "Unknown object owner or identifier");
  route.objectType = objectType; route.objectId = objectId;
  if (rest.length === 3) { route.tab = defaultObjectTab(workspace, section, objectType); return { kind: "resolved", route }; }
  if (rest.length === 5 && rest[3] === "tab") { const tabValue = rest[4]; if (!tabIsAddressable(contract, tabValue, objectType, projectId)) return unknown(pathname, "Unknown object tab"); route.tab = tabValue; return { kind: "resolved", route }; }
  if (rest.length === 7 && rest[3] === "tab" && (rest[5] === "version" || rest[5] === "evidence")) {
    // A tail hangs only from a tab whose contract DECLARES it. A version pinned
    // under `mapping`, or an evidence id pinned under `query`, names something
    // the contract never declared — so it stays Unknown rather than being
    // dropped, which would open a different address than the one shared.
    const tabValue = rest[4]; const tailId = decode(rest[6]);
    if (!tailId || !tabIsAddressable(contract, tabValue, objectType, projectId)) return unknown(pathname, "Unknown tab or version");
    if (rest[5] === "version") {
      if (!versionBearingTabs(contract).includes(tabValue)) return unknown(pathname, "Unknown tab or version");
      route.tab = tabValue; route.versionId = tailId; return { kind: "resolved", route };
    }
    if (!evidenceBearingTabs(contract).includes(tabValue)) return unknown(pathname, "Unknown tab or evidence target");
    route.tab = tabValue; route.evidenceId = tailId; return { kind: "resolved", route };
  }
  if (rest.length === 5 && rest[3] === "action") { const actionValue = rest[4]; if (!contract.actions?.includes(actionValue)) return unknown(pathname, "Unknown contextual action"); route.action = actionValue; return { kind: "resolved", route }; }
  return unknown(pathname, "Unknown object route shape");
}

function validateRoute(route: CanonicalRoute): void {
  if (route.scope === "account") { if (route.organizationId !== null || route.projectId !== null || route.globalSurface !== "account" || !ACCOUNT.has(route.globalSection)) throw new Error("Cannot build an unregistered Account route"); return; }
  if (route.scope === "platform") { if (route.organizationId !== null || route.projectId !== null || route.globalSurface !== "platform" || !PLATFORM.has(route.globalSection)) throw new Error("Cannot build an unregistered Platform route"); return; }
  if (route.scope === "organization") { if (!route.organizationId || route.projectId !== null || route.globalSurface !== "organization-settings" || !ORGANIZATION_SETTINGS.has(route.globalSection)) throw new Error("Cannot build an unregistered Organization route"); return; }
  if (!route.organizationId || !route.projectId) throw new Error("Project routes require exact scope");
  if (route.globalSurface === "project-settings") { if (!PROJECT_SETTINGS.has(route.globalSection as ProjectSettingsSection)) throw new Error("Unknown Project Settings section"); return; }
  if (route.globalSurface === "project-access") { if (!PROJECT_ACCESS.has(route.globalSection as ProjectAccessSection)) throw new Error("Unknown Project Access section"); return; }
  if (route.globalSurface === "getting-started") return;
  if (!findSection(route.workspace, route.section)) throw new Error("Cannot build an unregistered route");
  const queryKeys = Object.keys(route.query ?? {});
  if (queryKeys.length > 0) {
    if (!sectionQueryContract(route.workspace, route.section)) throw new Error("This section declares no query state");
    const { query: accepted } = validateSectionQuery(route.workspace, route.section, route.query);
    // Building an address from state the contract refuses would produce a
    // link that reopens something else. Refuse to build it at all.
    if (Object.keys(accepted).length !== queryKeys.length) throw new Error("Unknown or incomplete query state");
  }
  if (route.lens) {
    if (!sectionOwnsLens(route.workspace, route.section, route.lens)) throw new Error("Unknown collection lens");
    if (route.objectType || route.objectId || route.tab || route.versionId || route.evidenceId || route.action) {
      throw new Error("Collection lens and object routes are exclusive");
    }
    return;
  }
  if (!route.objectType && !route.objectId && !route.tab && !route.versionId && !route.evidenceId && !route.action) return;
  if (!route.objectType && !route.objectId && route.action && !route.tab && !route.versionId && !route.evidenceId) {
    if (!sectionOwnsAction(route.workspace, route.section, route.action)) {
      throw new Error("Unknown collection action");
    }
    return;
  }
  if (!route.objectType || !route.objectId) throw new Error("Object routes require type and identifier");
  const contract = findObjectContract(route.workspace, route.section, route.objectType);
  if (!contract) throw new Error("Object type does not belong to this owner");
  // Refusing to BUILD a closed capability tab matters as much as refusing to
  // parse one: an address assembled while the capability is off would be copied
  // out of a link and reopen nothing.
  if (route.tab && !tabIsAddressable(contract, route.tab, route.objectType, route.projectId)) throw new Error("Unknown object tab");
  // Refusing to BUILD is the point: an address assembled from state the contract
  // refuses would reopen something other than what it names, and would do it
  // from inside a copied link where nobody can see the mistake.
  if (route.versionId && !versionBearingTabs(contract).includes(route.tab ?? "")) throw new Error("Version routes require the versions tab");
  if (route.evidenceId && !evidenceBearingTabs(contract).includes(route.tab ?? "")) throw new Error("Evidence routes require an evidence-bearing tab");
  if (route.versionId && route.evidenceId) throw new Error("Version and evidence tails are exclusive");
  if (route.action && !contract.actions?.includes(route.action)) throw new Error("Unknown contextual action");
  if (route.action && (route.tab || route.versionId || route.evidenceId)) throw new Error("Action and tab routes are exclusive");
}
export function buildPath(route: CanonicalRoute): string {
  validateRoute(route);
  if (route.scope === "account") return `/account/${route.globalSection}`;
  if (route.scope === "platform") return `/platform/${route.globalSection}`;
  if (route.scope === "organization") return `/org/${encodeURIComponent(organizationSegmentFor(route.organizationId))}/settings/${route.globalSection}`;
  // The ADDRESS carries the slug; the route object keeps the id. Nothing
  // downstream of this line learns that slugs exist.
  const root = `/org/${encodeURIComponent(organizationSegmentFor(route.organizationId))}`
    + `/project/${encodeURIComponent(projectSegmentFor(route.projectId))}`;
  if (route.globalSurface === "project-settings") return `${root}/settings/${route.globalSection}`;
  if (route.globalSurface === "project-access") return `${root}/access/${route.globalSection}`;
  if (route.globalSurface === "getting-started") return `${root}/getting-started`;
  let path = `${root}/${route.workspace}/${route.section}`;
  const search = buildSearch(route.query ?? {});
  if (route.lens) return `${path}/lens/${encodeURIComponent(route.lens)}${search}`;
  if (!route.objectType || !route.objectId) {
    return route.action ? `${path}/action/${route.action}${search}` : `${path}${search}`;
  }
  path += `/object/${route.objectType}/${encodeURIComponent(route.objectId)}`;
  if (route.action) return `${path}/action/${route.action}${search}`;
  if (route.tab) path += `/tab/${route.tab}`;
  if (route.versionId) path += `/version/${encodeURIComponent(route.versionId)}`;
  if (route.evidenceId) path += `/evidence/${encodeURIComponent(route.evidenceId)}`;
  return `${path}${search}`;
}

interface RouterValue { result: RouteResult; route: CanonicalRoute; navigate: (to: Partial<CanonicalRoute>) => void; replace: (to: Partial<CanonicalRoute>) => void; }
const RouterContext = createContext<RouterValue | null>(null);
export function useRoute(): RouterValue { const value = useContext(RouterContext); if (!value) throw new Error("useRoute must be used within <RouterProvider>"); return value; }
function nextRoute(current: CanonicalRoute, to: Partial<CanonicalRoute>): CanonicalRoute {
  // A caller that names a workspace or a section is asking for a workspace
  // route, so it must not inherit a global surface. This matters most from the
  // `/` bootstrap: the unresolved placeholder is an Account route, and silently
  // inheriting its surface sent the entry navigation to /account/profile
  // instead of the first authorized Project Overview.
  const asksForWorkspace = to.workspace !== undefined || to.section !== undefined;
  const requestedSurface =
    to.globalSurface !== undefined
      ? to.globalSurface
      : asksForWorkspace
        ? null
        : current.globalSurface;
  if (requestedSurface === "account") return { ...EMPTY_ROUTE, ...to, scope: "account", organizationId: null, projectId: null, globalSurface: "account", globalSection: (to.globalSection ?? (current.globalSurface === "account" ? current.globalSection : "profile")) as AccountSection };
  if (requestedSurface === "platform") return { ...EMPTY_ROUTE, ...to, scope: "platform", organizationId: null, projectId: null, globalSurface: "platform", globalSection: (to.globalSection ?? "clocks") as PlatformSection } as PlatformGlobalRoute;
  if (requestedSurface === "organization-settings") { const organizationId = to.organizationId ?? current.organizationId; if (!organizationId) throw new Error("Organization Settings requires an Organization"); return { ...tail(), ...to, scope: "organization", organizationId, projectId: null, globalSurface: "organization-settings", globalSection: (to.globalSection ?? "general") as OrganizationSettingsSection }; }
  const organizationId = to.organizationId ?? current.organizationId; const projectId = to.projectId ?? current.projectId;
  if (!organizationId || !projectId) throw new Error("Project navigation requires exact scope");
  if (requestedSurface === "project-settings" || requestedSurface === "project-access" || requestedSurface === "getting-started") {
    const defaults: Record<string, GlobalSection> = { "project-settings": "general", "project-access": "people", "getting-started": "journey" };
    return { ...tail(), ...to, scope: "project", organizationId, projectId, globalSurface: requestedSurface, globalSection: (to.globalSection ?? (current.globalSurface === requestedSurface ? current.globalSection : defaults[requestedSurface])) as ProjectGlobalRoute["globalSection"] } as ProjectGlobalRoute;
  }
  const scopeChanged = organizationId !== current.organizationId || projectId !== current.projectId; const workspace = to.workspace ?? current.workspace; const section = to.section ?? current.section; const clear = scopeChanged || workspace !== current.workspace || section !== current.section || current.globalSurface !== null;
  const objectType = to.objectType !== undefined ? to.objectType : clear ? null : current.objectType;
  const objectId = to.objectId !== undefined ? to.objectId : clear ? null : current.objectId;
  const action = to.action !== undefined ? to.action : clear ? null : current.action;
  const tab = to.tab !== undefined ? to.tab : clear ? null : current.tab;
  const versionId = to.versionId !== undefined ? to.versionId : clear ? null : current.versionId;
  const evidenceId = to.evidenceId !== undefined ? to.evidenceId : clear ? null : (current.evidenceId ?? null);
  // Leaving the collection drops its lens; arriving at one without naming a lens
  // lands on the section's declared default rather than on no lens at all.
  //
  // A Project switch is NOT a reason to change lens. An object id belongs to one
  // Project and is cleared with `clear`; a lens belongs to the SCREEN, and the
  // person who switches Project while reading Audit Activity is still reading
  // Audit Activity. Only leaving the section resets it.
  const onCollection = !objectType && !objectId && !action;
  const sameSection = workspace === current.workspace && section === current.section;
  const lens = to.lens !== undefined
    ? to.lens
    : !onCollection
      ? null
      : (sameSection && current.lens) || defaultLens(workspace, section);
  const resolvedTab = objectType && objectId ? tab ?? defaultObjectTab(workspace, section, objectType) : null;
  // Query state belongs to the SECTION that declares it. Leaving the section
  // drops it -- carrying a Semantic View pin into Reports would build an
  // address Reports declares nothing to honour. Staying keeps it, so
  // switching tab or object does not lose the version being looked at.
  const query = to.query !== undefined
    ? to.query
    : sameSection && !scopeChanged && current.globalSurface === null
      ? current.query ?? {}
      : {};
  return { scope: "project", organizationId, projectId, workspace, section, lens, objectType, objectId, tab: resolvedTab, versionId: resolvedTab ? versionId : null, evidenceId: resolvedTab ? evidenceId : null, action, query, globalSurface: null, globalSection: null };
}
export function RouterProvider({ children }: { defaultProject?: string; children: ReactNode }) {
  // Address, not just path. Tracking `pathname` alone dropped the query state on
  // every refresh and every back/forward, which is exactly what an exact version
  // pin has to survive.
  const [address, setAddress] = useState(() => `${window.location.pathname}${window.location.search}`);
  /** Bumped when the scope publishes slugs, so the parse below re-runs even
   *  though the address string is unchanged. */
  const [aliasEpoch, setAliasEpoch] = useState(0);
  const [pathname, search] = useMemo(() => { const index = address.indexOf("?"); return index === -1 ? [address, ""] : [address.slice(0, index), address.slice(index)]; }, [address]);
  useEffect(() => { const onPop = () => setAddress(`${window.location.pathname}${window.location.search}`); window.addEventListener("popstate", onPop); return () => window.removeEventListener("popstate", onPop); }, []);
  const result = useMemo(() => parsePath(pathname, search), [pathname, search, aliasEpoch]); const route = result.kind === "resolved" ? result.route : EMPTY_ROUTE;
  useEffect(() => { if (result.kind !== "resolved") return; const canonical = buildPath(result.route); if (address !== canonical) { window.history.replaceState({}, "", canonical); setAddress(canonical); } }, [address, result]);
  // When the scope publishes its slugs, an address must be RE-READ even if not a
  // character of it changes — because what its segments MEAN changed.
  //
  // Both directions depend on it. A person who arrived on `/org/org_01KYJ0NP…/…`
  // needs the address rewritten to the readable form, and the rewrite above does
  // that on its own once `buildPath` knows a slug. But a person who arrived on a
  // shared `/org/acme/…` link has the opposite problem and a worse one: that
  // address is already its own canonical form, so nothing changes, the memo
  // below never re-runs, and `route.organizationId` stays the literal string
  // `"acme"` — a slug handed to every API call that expects an id.
  //
  // So this bumps a counter the parse depends on, rather than relying on the
  // address string to differ.
  //
  // The bump on mount is not belt-and-braces. `ScopeProvider` is a CHILD of this
  // provider, so React runs its effects FIRST: through the override seam it
  // publishes its aliases before this subscription exists, and the notification
  // goes to nobody. Catching up once on mount covers everything already
  // registered; the subscription covers everything that lands later, which is
  // the fetch path.
  useEffect(() => {
    setAliasEpoch((epoch) => epoch + 1);
    return onScopeAliasesChange(() => setAliasEpoch((epoch) => epoch + 1));
  }, []);
  // AND WHEN A CAPABILITY ANSWERS, the address must be RE-READ for the same
  // reason (story 58.6): not a character of it changes, but whether it may be
  // opened does. Without this, a Datastream whose `tax_fees` is off keeps serving
  // `…/tab/cost` until the next navigation — the address the amendment says must
  // not exist, alive for as long as somebody stays on it.
  useEffect(() => onCapabilityTabsChange(() => setAliasEpoch((epoch) => epoch + 1)), []);
  const change = useCallback((to: Partial<CanonicalRoute>, mode: "push" | "replace") => { const current = result.kind === "resolved" ? result.route : EMPTY_ROUTE; const next = nextRoute(current, to); const nextPath = buildPath(next); window.history[mode === "push" ? "pushState" : "replaceState"]({}, "", nextPath); setAddress(nextPath); }, [result]);
  const navigate = useCallback((to: Partial<CanonicalRoute>) => change(to, "push"), [change]); const replace = useCallback((to: Partial<CanonicalRoute>) => change(to, "replace"), [change]);
  const value = useMemo(() => ({ result, route, navigate, replace }), [result, route, navigate, replace]); return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}