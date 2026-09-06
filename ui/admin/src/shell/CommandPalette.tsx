/**
 * One box that reaches every address of the console.
 *
 * The target is `page-structure.md §G.2.1`, with its `Incomplete if`.
 *
 * The rail is a TREE: to open a Datastream you expand Data, then Datastreams,
 * then the tree underneath it, and you have to already know which of the six
 * workspaces owns what you want. That is the right shape for exploring and the
 * wrong one for going somewhere you can already name — and everything in this
 * console has a name: a workspace, a section, an Organization, a Project, a
 * Datastream, a settings surface.
 *
 * So this is the second way in, and it asks ONE question: what are you looking
 * for. Every answer it offers is a real address, and every one of them is
 * opened through `navigate()` with a canonical route — never an `<a href>` this
 * component composed. `routeHref.ts:9-19` records what a second address grammar
 * has cost this product four times over; the recents are stored as strings and
 * are therefore handed BACK to `parsePath` rather than reopened by hand.
 *
 * WHAT IT LISTS, and where each list comes from:
 *   Recent            `recentRoutes()`, only while the query is empty — the
 *                     history is an answer to "take me back", not to "find me X"
 *   Projects          `useScope().orgs`, the same payload the switcher offers
 *   Datastreams       `getDataSurface(project, "datastreams")`, the SAME
 *                     envelope the rail's tree and the top bar's crumb read
 *   the six workspaces `WORKSPACES` — the registry, never a copied list — one
 *                     group per workspace, its sections as the entries
 *   Settings          the five scope surfaces the gear menu offers
 *
 * THE READ HAPPENS ON OPEN, ONCE PER PROJECT. Filtering is a pure function of
 * what is already in hand: typing seven characters must not be seven requests,
 * which is the defect `useDebouncedValue` exists to patch elsewhere. The cache
 * is module-level, like the top bar's object-name cache, so walking in and out
 * of the palette costs nothing after the first time.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { CornerDownLeft, Search } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../ui";
import { getDataSurface } from "../data/dataSurface";
import { WORKSPACES } from "./navigation";
import { recentRoutes } from "./recentRoutes";
import {
  buildPath,
  parsePath,
  useRoute,
  type CanonicalRoute,
  type GlobalSection,
  type GlobalSurface,
} from "./router";
import { useScope, type OrgRef, type ProjectRef } from "./scope";

/** One offered destination. `keywords` is what the query is matched against —
 *  the label plus everything a person might type INSTEAD of it (the workspace
 *  above a section, the Organization above a Project), so "data" finds
 *  Datastreams and "acme" finds every Project of Acme. */
interface PaletteEntry {
  id: string;
  group: string;
  label: string;
  /** The quiet right-hand side: where this entry lives. */
  hint?: string;
  keywords: string;
  go: () => void;
}

/** The five scope surfaces, in the order the gear menu offers them. `scope`
 *  says what an entry needs in hand: a Project, an Organization, or nothing. */
const SURFACES: ReadonlyArray<{
  surface: GlobalSurface;
  section: GlobalSection;
  label: string;
  needs: "project" | "organization" | "none";
}> = [
  { surface: "getting-started", section: "journey", label: "Getting Started", needs: "project" },
  { surface: "project-settings", section: "general", label: "Project Settings", needs: "project" },
  { surface: "project-access", section: "people", label: "Project Access", needs: "project" },
  { surface: "organization-settings", section: "general", label: "Organization Settings", needs: "organization" },
  { surface: "account", section: "profile", label: "User Account", needs: "none" },
];

/** Datastream names already read, by Project. A second opening of the palette
 *  is free, and no keystroke has ever been a request. */
const DATASTREAMS = new Map<string, ReadonlyArray<{ id: string; name: string }>>();

/** Test seam — the cache outlives a `render()`. */
export function resetPaletteCache(): void {
  DATASTREAMS.clear();
}

/** A list long enough to scroll past is a list nobody reads. Everything is
 *  still reachable — by narrowing, which is what the box is for — and the
 *  count says plainly how much is not on screen. */
const VISIBLE_CAP = 40;

export default function CommandPalette({
  open,
  onOpenChange,
  org,
  project,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Resolved by the top bar, which already answers "which scope is this bar
   *  acting on" for a surface whose address carries none. Passed in rather than
   *  re-derived: two answers to that question would drift. */
  org: OrgRef | null;
  project: ProjectRef | null;
}) {
  const { route, navigate } = useRoute();
  const { orgs } = useScope();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [streams, setStreams] = useState<ReadonlyArray<{ id: string; name: string }>>([]);
  const listId = useId();
  const activeRef = useRef<HTMLButtonElement | null>(null);

  /* Ctrl+K / Cmd+K. Bound on the document rather than on the bar, because the
     whole point is that it works while the focus is wherever the person left
     it — inside a table, inside a workbench, anywhere. */
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      if (event.key.toLowerCase() !== "k") return;
      event.preventDefault();
      onOpenChange(!open);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onOpenChange]);

  // A palette reopened is a new question: it starts empty, at the top.
  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
    }
  }, [open]);

  const projectId = project?.id ?? null;
  useEffect(() => {
    if (!open || !projectId) return;
    const cached = DATASTREAMS.get(projectId);
    if (cached) {
      setStreams(cached);
      return;
    }
    let alive = true;
    void getDataSurface(projectId, "datastreams")
      .then((envelope) => {
        const items = envelope.items.map((item) => ({
          id: item.object_ref.id,
          name: item.name ?? item.label ?? item.object_ref.id,
        }));
        DATASTREAMS.set(projectId, items);
        if (alive) setStreams(items);
      })
      .catch(() => {
        // The palette keeps every other destination. A failed read is not
        // cached, so the next opening asks again — reopening is a deliberate
        // gesture, and retrying it is what the person is asking for.
        if (alive) setStreams([]);
      });
    return () => { alive = false; };
  }, [open, projectId]);

  const close = () => onOpenChange(false);

  const entries = useMemo<PaletteEntry[]>(() => {
    const list: PaletteEntry[] = [];
    for (const organization of orgs) {
      for (const candidate of organization.projects) {
        list.push({
          id: `project:${organization.id}:${candidate.id}`,
          group: "Projects",
          label: candidate.name,
          hint: organization.name,
          keywords: `${candidate.name} ${organization.name}`,
          go: () => {
            close();
            navigate({
              organizationId: organization.id, projectId: candidate.id,
              workspace: "overview", section: "project-overview",
              objectType: null, objectId: null, tab: null, versionId: null, action: null,
              globalSurface: null, globalSection: null,
            });
          },
        });
      }
    }
    if (org && project) {
      for (const stream of streams) {
        list.push({
          id: `datastream:${stream.id}`,
          group: "Datastreams",
          label: stream.name,
          hint: project.name,
          keywords: `${stream.name} datastream`,
          go: () => {
            close();
            navigate({
              organizationId: org.id, projectId: project.id,
              workspace: "data", section: "datastreams",
              objectType: "datastream", objectId: stream.id, tab: "overview",
              versionId: null, action: null, globalSurface: null, globalSection: null,
            });
          },
        });
      }
      for (const workspace of WORKSPACES) {
        for (const item of workspace.subnav) {
          list.push({
            id: `section:${workspace.key}:${item.slug}`,
            group: workspace.label,
            label: item.label,
            keywords: `${item.label} ${workspace.label} ${workspace.question}`,
            go: () => {
              close();
              navigate({
                organizationId: org.id, projectId: project.id,
                workspace: workspace.key, section: item.slug,
                objectType: null, objectId: null, tab: null, versionId: null, action: null,
                globalSurface: null, globalSection: null,
              });
            },
          });
        }
      }
    }
    for (const surface of SURFACES) {
      if (surface.needs === "project" && !project) continue;
      if (surface.needs === "organization" && !org) continue;
      list.push({
        id: `surface:${surface.surface}`,
        group: "Settings",
        label: surface.label,
        hint: surface.needs === "project" ? project?.name : surface.needs === "organization" ? org?.name : undefined,
        keywords: `${surface.label} settings`,
        go: () => {
          close();
          navigate({
            organizationId: surface.surface === "account" ? null : org?.id ?? route.organizationId,
            projectId: surface.surface === "organization-settings" || surface.surface === "account"
              ? null
              : project?.id ?? route.projectId,
            globalSurface: surface.surface,
            globalSection: surface.section,
            objectType: null, objectId: null, tab: null, versionId: null, action: null,
          } as Partial<CanonicalRoute>);
        },
      });
    }
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgs, org, project, streams, navigate, route.organizationId, route.projectId]);

  /** The address of the page being looked at — never offered as somewhere to go. */
  const here = useMemo(() => {
    try {
      return buildPath(route);
    } catch {
      return null;
    }
  }, [route]);

  const recents = useMemo<PaletteEntry[]>(() => {
    if (!open) return [];
    return recentRoutes()
      .filter((entry) => entry.path !== here)
      .map((entry) => ({
        id: `recent:${entry.path}`,
        group: "Recent",
        label: entry.label,
        keywords: entry.label,
        go: () => {
          const index = entry.path.indexOf("?");
          const result = index === -1
            ? parsePath(entry.path)
            : parsePath(entry.path.slice(0, index), entry.path.slice(index));
          // Refused rather than repaired: the store already dropped what the
          // router will not resolve, and guessing an adjacent address from
          // inside a menu is exactly what `routeHref.ts` forbids.
          if (result.kind !== "resolved") return;
          close();
          navigate(result.route);
        },
      }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, here, navigate]);

  const normalized = query.trim().toLowerCase();
  const matched = useMemo(
    () => (normalized
      // The history answers "take me back". Once a question is typed, the
      // answer is the thing itself, wherever it lives.
      ? entries.filter((entry) => entry.keywords.toLowerCase().includes(normalized))
      : [...recents, ...entries]),
    [normalized, entries, recents],
  );
  const visible = matched.slice(0, VISIBLE_CAP);

  useEffect(() => setActive(0), [normalized]);
  useEffect(() => {
    if (active >= visible.length) setActive(visible.length > 0 ? visible.length - 1 : 0);
  }, [active, visible.length]);
  useEffect(() => {
    // jsdom implements no scrolling, and a palette that threw here would fail
    // its own tests rather than the product's.
    activeRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [active, open]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (visible.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (index + 1) % visible.length);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index - 1 + visible.length) % visible.length);
      return;
    }
    if (event.key === "Home") { event.preventDefault(); setActive(0); return; }
    if (event.key === "End") { event.preventDefault(); setActive(visible.length - 1); return; }
    if (event.key === "Enter") {
      event.preventDefault();
      visible[active]?.go();
    }
  };

  let renderedGroup = "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="top-[10vh] max-w-xl translate-y-0 gap-0 overflow-hidden p-0 sm:max-w-xl"
      >
        <DialogTitle className="sr-only">Search and jump to</DialogTitle>
        <DialogDescription className="sr-only">
          Type a workspace, a Project, a Datastream or a settings page. Arrows move, Enter opens, Escape closes.
        </DialogDescription>
        <div className="flex items-center gap-3 border-b border-divider-base px-4 dark:border-divider-medium">
          <Search className="size-4 shrink-0 text-text-secondary" aria-hidden="true" />
          <input
            autoFocus
            type="text"
            role="combobox"
            aria-expanded
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={visible[active] ? `${listId}-${active}` : undefined}
            aria-label="Search workspaces, Projects, Datastreams and settings"
            placeholder="Search workspaces, Projects, Datastreams…"
            className="min-w-0 flex-1 bg-transparent py-4 text-body text-text outline-none placeholder:text-text-secondary"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
          />
        </div>
        <div
          id={listId}
          role="listbox"
          aria-label="Destinations"
          className="max-h-[22rem] overflow-y-auto p-2"
        >
          {visible.length === 0 ? (
            <p className="px-3 py-8 text-center text-ui text-text-secondary">
              {normalized
                ? `Nothing here is called “${query.trim()}”. Try a workspace, a Project or a Datastream name.`
                : "Open a Project to search its workspaces and Datastreams."}
            </p>
          ) : null}
          {visible.map((entry, index) => {
            const header = entry.group === renderedGroup ? null : entry.group;
            renderedGroup = entry.group;
            const selected = index === active;
            return (
              <div key={entry.id}>
                {header ? (
                  <p className={`px-3 pb-1 text-caption font-semibold uppercase tracking-wide text-text-secondary ${index === 0 ? "pt-1" : "pt-3"}`}>
                    {header}
                  </p>
                ) : null}
                <button
                  type="button"
                  role="option"
                  id={`${listId}-${index}`}
                  aria-selected={selected}
                  tabIndex={-1}
                  ref={selected ? activeRef : undefined}
                  className={`flex min-h-9 w-full items-center gap-3 rounded-small px-3 text-left text-ui outline-none ${
                    selected
                      ? "bg-surface-subtle font-semibold text-text dark:bg-divider-medium dark:text-text-on-dark"
                      : "text-text-secondary"
                  }`}
                  onMouseEnter={() => setActive(index)}
                  onClick={entry.go}
                >
                  <span className="min-w-0 flex-1 truncate">{entry.label}</span>
                  {entry.hint ? (
                    <span className="min-w-0 shrink-0 truncate text-caption text-text-secondary">{entry.hint}</span>
                  ) : null}
                  {selected ? <CornerDownLeft className="size-3.5 shrink-0 text-text-secondary" aria-hidden="true" /> : null}
                </button>
              </div>
            );
          })}
          {matched.length > visible.length ? (
            <p className="px-3 pb-1 pt-3 text-caption text-text-secondary">
              Showing {visible.length} of {matched.length}. Keep typing to narrow.
            </p>
          ) : null}
        </div>
        <p className="border-t border-divider-base px-4 py-2 text-caption text-text-secondary dark:border-divider-medium">
          Arrows to move, Enter to open, Escape to close.
        </p>
      </DialogContent>
    </Dialog>
  );
}
