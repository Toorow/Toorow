import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronRight, ChevronsUpDown, Menu, Search, Settings } from "lucide-react";
import { Button, Input, NavSubItem, Sheet, SheetContent, SheetTitle, TONE_CONTAINER, TONE_TEXT } from "../ui";
import { getDataSurface, type DataLens } from "../data/dataSurface";
import CommandPalette from "./CommandPalette";
import { lastProjectScope, type ProjectScope } from "./lastProjectScope";
import { findSection, WORKSPACE_BY_KEY } from "./navigation";
import { objectLabel } from "../governance/contracts";
import { rememberRoute } from "./recentRoutes";
import { buildPath, type CanonicalRoute, type GlobalSection, type GlobalSurface, useRoute } from "./router";
import { type OrgRef, type ProjectRef, useScope } from "./scope";
import { SidebarBody } from "./StableSidebar";

const SURFACE_LABELS: Record<GlobalSurface, string> = {
  "project-settings": "Project Settings",
  "project-access": "Project Access",
  "organization-settings": "Organization Settings",
  account: "User Account",
  "getting-started": "Getting Started",
  platform: "Platform",
};
const SECTION_LABELS: Record<string, string> = {
  general: "General", capabilities: "Capabilities", changes: "Changes", people: "People", handoffs: "Handoffs",
  members: "Members", credentials: "Credentials", "account-exposure": "Account exposure",
  "data-access": "Data access", "mcp-hosts": "MCP hosts", actions: "Actions",
  profile: "Profile", authorizations: "Authorizations", journey: "Journey",
  clocks: "Clocks",
};

/** One crumb of the governed path. `mono` marks the segments that are
 *  identifiers rather than words; `full` is what the truncation hides; `go`
 *  is the level this crumb opens, absent when the level has no address of its
 *  own (the object TYPE is a word, not a page). */
interface RailSegment { text: string; mono?: boolean; full?: string; go?: () => void }

/**
 * Which Data lens can NAME an object type.
 *
 * The rail resolves a Datastream to `Site Europe — GA4` (`DataTree.tsx:73`)
 * while the crumb right above it showed `dstr_01KYJ0NP…` truncated to twelve
 * characters: the same object, named on one side of the screen and unnamed on
 * the other. The name is not fabricated — it comes from the same envelope the
 * rail reads, and when that read gives nothing the id stays, which is the
 * honest answer rather than an invented label.
 */
const NAMING_LENS: Record<string, DataLens> = {
  datastream: "datastreams",
  "event-configuration": "events",
  "source-account": "sources",
  import: "imports",
  connector: "connectors",
};

/** Names already read, keyed `${projectId}:${objectId}`, and the collections
 *  already asked for. Module-level and deliberately so: walking through five
 *  Datastreams costs ONE read of the collection, not five, and coming back to
 *  one already visited costs none. Never a read per render. */
const OBJECT_NAMES = new Map<string, string>();
const COLLECTIONS_READ = new Set<string>();

/** Test seam. The cache outlives a `render()`, so a case that asserts the
 *  unresolved id must be able to start from nothing. */
export function resetObjectNameCache(): void {
  OBJECT_NAMES.clear();
  COLLECTIONS_READ.clear();
}

/** The human name of the object in the address, or null while it is unknown. */
function useObjectName(
  projectId: string | null,
  objectType: string | null,
  objectId: string | null,
): string | null {
  const lens = objectType ? NAMING_LENS[objectType] : undefined;
  const key = projectId && objectId ? `${projectId}:${objectId}` : null;
  const [, resolved] = useState(0);
  useEffect(() => {
    if (!projectId || !lens || !key || OBJECT_NAMES.has(key)) return;
    const collection = `${projectId}:${lens}`;
    if (COLLECTIONS_READ.has(collection)) return;
    // Marked BEFORE the read, and never unmarked: a failed read that re-armed
    // itself would ask again on every render of every page of that Project.
    COLLECTIONS_READ.add(collection);
    let alive = true;
    void getDataSurface(projectId, lens)
      .then((envelope) => {
        for (const item of envelope.items) {
          const name = item.name ?? item.label;
          if (name) OBJECT_NAMES.set(`${projectId}:${item.object_ref.id}`, name);
        }
        if (alive) resolved((count) => count + 1);
      })
      .catch(() => {
        // A breadcrumb is not worth an error surface. The id keeps the crumb
        // truthful on its own.
      });
    return () => { alive = false; };
  }, [projectId, lens, key]);
  return key ? OBJECT_NAMES.get(key) ?? null : null;
}

/* `sentenceCase` was here, turning `source-account` into `Source account` for a
   type the registry could not name. Story 76-3 removed it rather than leaving it
   unreachable: `objectLabel()` answers for every type, declared or not, and a
   dead second answer is what the next reader restores by accident. The argument
   it carried — sentence case, not Title Case, "because every tab and lens label
   in navigation.ts already uses it" — holds for a TAB and not for an object
   noun, which `glossary.md` spells `Source Account`. */

/**
 * The Organization and Project this bar acts on.
 *
 * `useScope().activeProject` is null on any address whose scope is not a
 * project — which is every scope surface. The menu read it directly, so on
 * Organization Settings and User Account **three of its five entries silently
 * disappeared** (Getting Started, Project Settings, Project Access) and the
 * switcher collapsed to the word "Organization". Jean, 2026-08-04, looking at
 * the two that were left: *"pourquoi le menu est pas pareil"*.
 *
 * Same root cause as the rail, so the same answer: the Project the person was
 * last in. Its NAME is resolved from `orgs`, the real scope payload — never
 * fabricated from an id, and null when the remembered Project is no longer in
 * the payload, which is what losing access looks like.
 */
export function resolveBarScope(
  org: OrgRef | null,
  orgs: OrgRef[],
  activeProject: ProjectRef | null,
  remembered: { organizationId: string; projectId: string } | null,
): { org: OrgRef | null; project: ProjectRef | null } {
  if (activeProject) return { org, project: activeProject };
  if (!remembered) return { org, project: null };
  const owner = orgs.find((candidate) => candidate.projects.some((project) => project.id === remembered.projectId));
  const project = owner?.projects.find((candidate) => candidate.id === remembered.projectId) ?? null;
  return { org: org ?? owner ?? null, project };
}

/** What to print on the palette's key hint. A Mac keyboard has no Ctrl key in
 *  that position, and a hint naming a key the person does not have is worse
 *  than no hint: the shortcut itself accepts both. */
function shortcutHint(): string {
  try {
    return /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent) ? "⌘ K" : "Ctrl K";
  } catch {
    return "Ctrl K";
  }
}

export default function TopBar({ railScope = null }: { railScope?: ProjectScope | null }) {
  const { org: routedOrg, orgs, activeProject: routedProject } = useScope();
  const { org, project: activeProject } = resolveBarScope(routedOrg, orgs, routedProject, lastProjectScope());
  const { route, navigate } = useRoute();
  const [open, setOpen] = useState<null | "scope" | "actions">(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Cleared every time the dialog opens: a filter remembered from last week
  // reads as a list that shrank.
  const [scopeFilter, setScopeFilter] = useState("");
  useEffect(() => { if (open === "scope") setScopeFilter(""); }, [open]);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (ref.current && !ref.current.contains(event.target as Node)) setOpen(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(null); };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open]);

  const filteredOrgs = useMemo(() => {
    const needle = scopeFilter.trim().toLowerCase();
    if (!needle) return orgs;
    return orgs
      .map((organization) => organization.name.toLowerCase().includes(needle)
        ? organization
        : { ...organization, projects: organization.projects.filter((project) => project.name.toLowerCase().includes(needle)) })
      .filter((organization) => organization.projects.length > 0
        || organization.name.toLowerCase().includes(needle));
  }, [orgs, scopeFilter]);

  const goProject = (organizationId: string, projectId: string) => {
    setOpen(null);
    navigate({ organizationId, projectId, workspace: "overview", section: "project-overview", objectType: null, objectId: null, tab: null, versionId: null, action: null, globalSurface: null, globalSection: null });
  };
  const openSurface = (globalSurface: GlobalSurface, globalSection: GlobalSection) => {
    setOpen(null);
    navigate({
      organizationId: globalSurface === "account" ? null : org?.id ?? route.organizationId,
      projectId: globalSurface === "organization-settings" || globalSurface === "account" ? null : activeProject?.id ?? route.projectId,
      globalSurface,
      globalSection,
      objectType: null, objectId: null, tab: null, versionId: null, action: null,
    } as Partial<CanonicalRoute>);
  };
  const workspace = WORKSPACE_BY_KEY[route.workspace];
  const section = findSection(route.workspace, route.section);
  const objectName = useObjectName(route.projectId, route.objectType, route.objectId);
  /* A CRUMB THAT CANNOT BE CLICKED IS NOT A PATH, it is a caption. The bar drew
     the whole governed path as `<span>`s, so the one gesture a breadcrumb exists
     for — going back up a level — was nowhere on an object page: leaving a
     Datastream meant finding it again in the rail.
     Each non-terminal crumb now opens its own level, and only its own: a
     workspace opens its first section (a workspace has no page of its own), a
     section opens its collection with the object dropped. The object TYPE keeps
     no gesture — its address IS the collection, and a second crumb going to the
     same place would be a promise of a different one. */
  const firstSection: string | undefined = workspace?.subnav[0]?.slug;
  const openWorkspace = firstSection
    ? () => navigate({
        workspace: route.workspace, section: firstSection,
        objectType: null, objectId: null, tab: null, versionId: null, action: null,
      })
    : undefined;
  const openCollection = () => navigate({
    workspace: route.workspace, section: route.section,
    objectType: null, objectId: null, tab: null, versionId: null, action: null,
  });
  /* The governed path carries two natures and used to render them as one.
     `route.objectType` is a WORD — `source-account`, `golden-question` — and it
     was drawn in the monospace face reserved for immutable ids, dashes and all.
     `route.objectId` is an id, and it was drawn at full length: on this
     product's ULIDs that is a 30-character string in the middle of the
     breadcrumb, unreadable and impossible to shorten by eye.
     `DESIGN.md:96` splits them — mono is for ids, hashes and raw versions, not
     for nouns — so the rail now says which segment is which, and the id keeps
     its full value on hover rather than on screen. */
  const segments: (RailSegment | null)[] = route.globalSurface
    ? [{ text: SURFACE_LABELS[route.globalSurface] }, { text: SECTION_LABELS[route.globalSection] }]
    : [
        workspace?.label ? { text: workspace.label, go: openWorkspace } : null,
        section?.label ? { text: section.label, go: openCollection } : null,
        // The DECLARED noun wins over the sentence-cased token. `sentenceCase`
        // turned a wire token into English and nothing more, so a type whose
        // displayed noun differs from its token — `tracked-entity`, read
        // `Competitor` since 2026-09-01 — was named one way here and another way
        // in the workbench. The registry declares it once; this reads it.
        //
        // ONE ANSWER, AND IT IS `objectLabel`'s. Until story 76-3 this fell back
        // to `sentenceCase` while `governance/contracts.ts` answered
        // `Unknown object type` for the same token — two readings of the same
        // undeclared type, on two halves of one screen, which is the exact
        // defect the registry was made to end. All 37 delivered types now
        // declare a label, so the fallback is unreachable; it stays as ONE
        // answer rather than a second.
        route.objectType ? { text: objectLabel(route.objectType) } : null,
        route.objectId
          ? objectName
            // Named, and the exact id stays one hover away — Governance needs
            // the pointer, a reader needs the noun.
            ? { text: objectName, full: route.objectId }
            : { text: route.objectId, mono: true, full: route.objectId }
          : null,
        route.versionId ? { text: `v ${route.versionId}`, mono: true, full: route.versionId } : null,
      ];
  const rail = segments.filter((segment): segment is RailSegment => segment !== null);

  /* WHERE THE HISTORY IS WRITTEN, and why here rather than in the shell.
     `ApplicationShell` also sees every route change — it already writes
     `rememberProjectScope` there — but it does not have the LABEL. The bar is
     the one component that resolves the governed path into words and the object
     id into the name the person recognises, and it is mounted on every address.
     Recording from the shell would mean either storing an address with no name
     or rebuilding the object-name read that has just happened two lines above.
     The label re-lands when that read answers, and `rememberRoute` dedupes by
     address, so the entry is updated rather than doubled. */
  const address = useMemo(() => {
    try {
      return buildPath(route);
    } catch {
      // An address that cannot be built is not one that can be reopened.
      return null;
    }
  }, [route]);
  const trail = rail.map((segment) => segment.text).join(" › ");
  useEffect(() => {
    if (address) rememberRoute({ path: address, label: trail });
  }, [address, trail]);

  /* The drawer is a way IN, never a thing to dismiss afterwards. Closing it on
     the address changing covers every row it renders — including the Datastream
     tree, which navigates on its own and knows nothing about a host. */
  useEffect(() => setDrawerOpen(false), [address]);
  /* The drawer must show exactly what the rail would have shown, so the shell
     hands down the scope IT resolved (`resolveRailScope`) rather than the bar
     answering the same question a second time and drifting. The fallback covers
     the bar rendered on its own — in a test, in the sandbox — where the bar's
     own resolution is the only one there is. */
  const drawerScope: ProjectScope | null = railScope
    ?? (org && activeProject ? { organizationId: org.id, projectId: activeProject.id } : null);

  const scopeName = route.scope === "account" ? "Personal scope" : route.scope === "platform" ? "Platform scope" : org?.name ?? "Organization";
  /* THE DEAD END. The switcher was gated on `org && activeProject`, and on
     User Account `useScope()` returns neither — so in a fresh session, where
     `lastProjectScope()` remembers nothing, the one control that opens a
     Project VANISHED, the gear menu kept only "User Account" (its Project
     entries are gated on `activeProject`, Organization Settings on `org`), and
     the left rail renders nothing without a Project. `/account/profile` was a
     room with no door.
     What the switcher needs is not a current Project, it is a LIST of them to
     offer. That is `orgs`, which the scope payload carries on every surface,
     account included. */
  const canSwitch = orgs.some((organization) => organization.projects.length > 0);

  return (
    <header className="sticky top-0 z-40 flex h-[4.5rem] items-center gap-2 border-b border-divider-base bg-surface-light/95 px-4 backdrop-blur dark:border-divider-medium dark:bg-surface-dark/95 sm:gap-4 sm:px-8">
      {/* THE PRODUCT HAD NO NAVIGATION BELOW `lg`. The rail is `hidden lg:flex`
          (`StableSidebar.tsx`) and nothing replaced it: no workspaces, no
          sections, no account menu — a person on a laptop at 1280 kept the rail,
          a person on a tablet lost the console. The same rows, on demand. */}
      {drawerScope ? (
        <Button
          type="button"
          variant="secondary"
          size="icon"
          className="shrink-0 lg:hidden"
          aria-label="Open navigation"
          aria-expanded={drawerOpen}
          title="Open navigation"
          onClick={() => setDrawerOpen(true)}
        >
          <Menu className="size-4" />
        </Button>
      ) : null}
      <nav className="min-w-0 flex-1" aria-label="Governed path">
        <ol className="flex min-w-0 items-center gap-1.5 text-xs text-text-secondary">
          {rail.map((segment, index) => {
            const face = segment.mono ? "max-w-[12ch] truncate font-mono" : "truncate font-semibold";
            const terminal = index === rail.length - 1;
            return (
              <li key={`${segment.text}-${index}`} className="flex min-w-0 items-center gap-1.5">
                {index > 0 ? <ChevronRight className="size-3 shrink-0" aria-hidden="true" /> : null}
                {!terminal && segment.go ? (
                  <button type="button" className={`${face} rounded-small outline-none hover:text-text hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus`} title={segment.full} onClick={segment.go}>{segment.text}</button>
                ) : (
                  <span className={face} title={segment.full} aria-current={terminal ? "page" : undefined}>{segment.text}</span>
                )}
              </li>
            );
          })}
        </ol>
      </nav>
      {/* The shortcut is not the feature — a control nobody can see is a control
          nobody has. The key hint says the shortcut exists, which is how the
          second one is learnt. */}
      <Button
        type="button"
        variant="secondary"
        className="shrink-0 gap-2 px-2.5 font-normal text-text-secondary sm:px-3"
        aria-label="Search and jump to"
        aria-keyshortcuts="Control+K Meta+K"
        title={`Search and jump to (${shortcutHint()})`}
        onClick={() => setPaletteOpen(true)}
      >
        <Search className="size-4 shrink-0" aria-hidden="true" />
        <span className="hidden lg:inline">Search</span>
        <kbd className="hidden rounded-small border border-divider-base px-1.5 font-mono text-caption text-text-secondary dark:border-divider-medium lg:inline">
          {shortcutHint()}
        </kbd>
      </Button>
      <div ref={ref} className="relative flex items-center">
        {canSwitch ? (
          <Button type="button" variant="secondary" className="max-w-[28rem] gap-2 rounded-r-none border-r-0" aria-haspopup="dialog" aria-expanded={open === "scope"} aria-label={org && activeProject ? `Switch organization or project: ${org.name}, ${activeProject.name}` : "Switch organization or project"} onClick={() => setOpen(open === "scope" ? null : "scope")}>
            {org ? <span className={`grid size-6 shrink-0 place-items-center rounded-small text-xs font-bold ${TONE_CONTAINER.info} ${TONE_TEXT.info}`}>{org.name.charAt(0)}</span> : null}
            <span className="truncate">{org?.name ?? scopeName}</span>
            {activeProject
              ? <><ChevronRight className="size-3 text-text-secondary" aria-hidden="true" /><span className="truncate">{activeProject.name}</span></>
              // No Project in hand and none remembered: the control names the
              // gesture instead of a scope, which is what it is for here.
              : <><ChevronRight className="size-3 text-text-secondary" aria-hidden="true" /><span className="truncate text-text-secondary">Open a project</span></>}
            <ChevronsUpDown className="size-3.5 shrink-0 text-text-secondary" aria-hidden="true" />
          </Button>
        ) : <span className="hidden rounded-l-control border border-r-0 border-divider-base px-3 py-2 text-sm font-semibold sm:block">{scopeName}</span>}
        <Button type="button" variant="secondary" size="icon" className="rounded-l-none" aria-haspopup="menu" aria-expanded={open === "actions"} aria-label="Scope and account menu" title="Scope and account menu" onClick={() => setOpen(open === "actions" ? null : "actions")}><Settings className="size-4" /></Button>
        {open === "scope" && canSwitch ? (
          <div className="absolute right-0 top-12 z-50 w-80 rounded-menu border border-divider-base bg-surface-light p-2 shadow-overlay dark:border-divider-medium dark:bg-surface-dark-elevated" role="dialog" aria-label="Switch organization or project">
            <div className="px-2 pb-2 pt-1"><strong className="block text-sm">Switch organization or project</strong><span className="text-xs text-text-secondary">Choose an Organization, then a Project.</span></div>
            {/* The list is already in memory, so narrowing it is pure. An org
                whose name matches keeps every project; otherwise it keeps the
                matching projects and stays as their heading. */}
            <div className="px-2 pb-2">
              <Input
                autoFocus
                value={scopeFilter}
                placeholder="Filter by name"
                aria-label="Filter organizations and projects"
                onChange={(event) => setScopeFilter(event.target.value)}
              />
            </div>
            {/* Rows, not bordered pills: choosing a Project is navigating, and
                the rail already says what navigating looks like here. */}
            {filteredOrgs.map((organization) => <div key={organization.id} className="border-t border-divider-base py-1 first:border-t-0 dark:border-divider-medium"><p className="px-2 py-1 text-xs font-semibold text-text-secondary">{organization.name}</p>{organization.projects.map((project) => <NavSubItem key={project.id} active={project.id === activeProject?.id} trailing={project.id === activeProject?.id ? <Check className={`size-4 ${TONE_TEXT.success}`} aria-hidden="true" /> : null} onClick={() => goProject(organization.id, project.id)}>{project.name}</NavSubItem>)}</div>)}
            {filteredOrgs.length === 0 ? (
              <p className="px-2 py-2 text-xs text-text-secondary" data-testid="scope-filter-empty">
                Nothing is named “{scopeFilter.trim()}”. Clear the filter to see every
                Organization and Project again.
              </p>
            ) : null}
          </div>
        ) : null}
        {open === "actions" ? (
          <div className="absolute right-0 top-12 z-50 w-72 rounded-menu border border-divider-base bg-surface-light p-2 shadow-overlay dark:border-divider-medium dark:bg-surface-dark-elevated" role="menu" aria-label="Scope and account menu">
            <div className="px-2 py-2"><span className="block text-xs text-text-secondary">{scopeName}</span>{activeProject ? <strong className="block truncate text-sm">{activeProject.name}</strong> : null}</div>
            {activeProject ? <><NavSubItem role="menuitem" active={route.globalSurface === "getting-started"} onClick={() => openSurface("getting-started", "journey")}>Getting Started</NavSubItem><NavSubItem role="menuitem" active={route.globalSurface === "project-settings"} onClick={() => openSurface("project-settings", "general")}>Project Settings</NavSubItem><NavSubItem role="menuitem" active={route.globalSurface === "project-access"} onClick={() => openSurface("project-access", "people")}>Project Access</NavSubItem></> : null}
            {/* The way back in, from the menu that is the only control left on a
                surface with no Project. Offered only when there is none: with a
                Project in hand the switcher beside this menu already names it,
                and asking the same question twice is asking it once too often. */}
            {canSwitch && !activeProject ? <NavSubItem role="menuitem" onClick={() => setOpen("scope")}>Open a project…</NavSubItem> : null}
            {org ? <NavSubItem role="menuitem" active={route.globalSurface === "organization-settings"} onClick={() => openSurface("organization-settings", "general")}>Organization Settings</NavSubItem> : null}
            <NavSubItem role="menuitem" active={route.globalSurface === "account"} onClick={() => openSurface("account", "profile")}>User Account</NavSubItem>
          </div>
        ) : null}
      </div>
      {/* Mounted unconditionally: it owns the Ctrl+K / Cmd+K listener, and a
          shortcut that only works once the button has been pressed is not one. */}
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} org={org} project={activeProject} />
      {drawerScope ? (
        <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
          {/* The rail's own width, not the Sheet's 384px: this IS the rail. */}
          <SheetContent side="left" className="w-64" aria-describedby={undefined}>
            <SheetTitle className="sr-only">Project navigation</SheetTitle>
            <SidebarBody scope={drawerScope} onNavigate={() => setDrawerOpen(false)} />
          </SheetContent>
        </Sheet>
      ) : null}
    </header>
  );
}