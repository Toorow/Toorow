/**
 * The one Level-2 shell behind all four Governance collections.
 *
 * Four screens, fifteen lenses, one layout. The lens is a ROUTE — a real anchor
 * built from the canonical registry — because the previous Evidence screen kept
 * its lens in component state, which meant a person could not send anyone the
 * view they were looking at. A lens in `useState` is not a view; it is a mood.
 *
 * Rows open the exact object route through an anchor in the name cell, so the
 * keyboard reaches them the way it reaches every other link. The old pattern —
 * a `tabIndex` on `<tr>` with an `onKeyDown` handler — reimplemented half of
 * what an `<a>` already does and left the other half missing.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  EmptyState,
  Input,
  PageHeader,
  Pager,
  Panel,
  PanelHeader,
  PanelBody,
  SortableHead,
  SortScopeNote,
  sortRows,
  Stack,
  Status,
  stateLabel,
  stateTone,
  StatusLegend,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  useTableSort,
  NativeSelect,
  NavTabs,
  EntityMatrix,
  type NavTab,
  type SortValue,
  type MatrixDatastream,
  type MatrixEntity,
  formatTimestamp,
  type StatusLegendEntry,
  type Tone,
} from "../ui";
import RouteState from "../shell/RouteState";
import { buildPath, type CanonicalRoute, useRoute } from "../shell/router";
import { sectionLenses } from "../shell/navigation";
import { objectLabel, ownershipTarget, scopeLabel, SECTION_OWNERSHIP } from "./contracts";
import { ObjectTypeName } from "../shell/ObjectTypeName";
import {
  EVIDENCE_COLUMNS,
  EvidenceCoverage,
  EvidenceFilters,
  EvidenceRow,
} from "./EvidenceCollection";
import { type GovernanceObject, useGovernanceCollection } from "./governanceSurface";
import NewConceptDialog from "./NewConceptDialog";
import NewSemanticViewDialog from "./NewSemanticViewDialog";
import NewBusinessDomainDialog from "./NewBusinessDomainDialog";
import ProjectMapping from "../shell/pages/ProjectMapping";
import MdmConflictsPanel from "./MdmConflictsPanel";
import MasterDataConvergencePanel, {
  planIsConverged,
  useMasterDataConvergence,
} from "./MasterDataConvergence";
import AlertDestinations from "./AlertDestinations";
import TransformationsLibrary from "./TransformationsLibrary";
import UnresolvedValuesPanel from "./UnresolvedValuesPanel";
import CleanupRules from "./CleanupRules";
import CanonicalFields from "./CanonicalFields";
import CalculatedFieldProposalsQueue from "./CalculatedFieldProposalsQueue";

interface SectionGuide {
  title: string;
  shortTitle: string;
  description: string;
  purposeTitle: string;
  purposeText: string;
  highlights: Array<{ label: string; detail: string }>;
}

const SECTION_COPY: Record<string, SectionGuide> = {
  "master-data": {
    title: "Master Data",
    shortTitle: "Master Data",
    description:
      "Versioned business domains, classifications and the registries an enabled Project capability contributes.",
    purposeTitle: "What is Master Data for?",
    purposeText:
      "Master Data is the single registry of reference objects across your organization. It prevents definition fragmentation between teams and guarantees a stable identity for all ingested data.",
    highlights: [
      { label: "Business Domains & Classifications", detail: "Structure Business Domain trees (Sales, Finance, Marketing) and category hierarchies." },
      { label: "Products & Activities", detail: "Define master entities processed by Datastreams and analytical algorithms." },
      { label: "Registries & Taxonomies", detail: "Manage cross-cutting reference data (Countries, Currencies, Markets, Competitors)." },
    ],
  },
  "semantic-model": {
    title: "Semantic Model",
    shortTitle: "Semantic Model",
    // NOT « Canonical concepts »: that borrows the Canonical Field's adjective
    // for the other object. This lens serves Semantic Concepts (glossary,
    // arbitration 2026-08-14).
    description:
      "Semantic Concepts, published Semantic Views and the coverage of each Concept across this Project's Datastreams.",
    purposeTitle: "What is the Semantic Model for?",
    purposeText:
      "The Semantic Model abstracts physical table complexity into governed Semantic Concepts. It guarantees every metric (e.g. Gross Revenue, Margin) is calculated identically across all reports and AI skills.",
    highlights: [
      { label: "Metrics & Dimensions", detail: "Define calculation formulas, aggregation functions (SUM, AVG) and value types." },
      { label: "Semantic Views", detail: "Publish sets of metrics and dimensions ready to be queried by Analyze and AI Skills." },
      { label: "Mapping Coverage", detail: "Visualize which Datastreams feed each Concept." },
    ],
  },
  "controls-quality": {
    title: "Controls & Quality",
    shortTitle: "Controls & Quality",
    description:
      "Conflicts and approval cases, reconciliation rule sets, Data Quality monitors and capability rule ladders.",
    purposeTitle: "What is Controls & Quality for?",
    purposeText:
      "This control center unifies the resolution of mapping ambiguities and data anomalies, supervising data reliability before publishing into reports and virtual assistants.",
    highlights: [
      { label: "Control Cases & Conflicts", detail: "Validate, resolve or escalate mapping divergences and alignment between Datastreams." },
      { label: "Data Quality Monitors", detail: "Monitor completeness, freshness and compliance of ingested data." },
      { label: "Rule Sets & Exceptions", detail: "Set policies for currency conversion (FX), timezones and taxes." },
    ],
  },
  evidence: {
    title: "Evidence",
    shortTitle: "Evidence",
    description:
      "A reference-only index over lineage and provenance, object versions and approvals, and audited Project activity.",
    purposeTitle: "What is Evidence for?",
    purposeText:
      "The Evidence index provides an unalterable audit and governance guarantee. It retains exact proof of every transformation, approval and execution performed on the project.",
    highlights: [
      { label: "Lineage & Provenance", detail: "Trace the exact path of a value from the raw source field to its final metric." },
      { label: "Versions & Approbations", detail: "Inspect locked object versions, modification authors and validation statuses." },
      { label: "Activity Audit Log", detail: "Follow the chronological history of all governance operations." },
    ],
  },
};

/*
 * THE OVERRIDE THAT USED TO LIVE HERE IS WITHDRAWN (story 76-2,
 * `console-presentation.md` §3). It drew `blocked` as an ERROR where the five
 * Data collections draw it as a warning, on the reading that a governed object
 * whose lifecycle is blocked "cannot be used at all". The arbitration went the
 * other way: a blocked lifecycle is still repairable, which is exactly what
 * `warning` means in this scale, and the word that means "cannot be used at all"
 * is `archived` — already `error`, and drawn as one on this very screen. This
 * file reads the union like everybody else now, and there is no `overrides`
 * parameter left to pass one.
 */

/**
 * The three lenses that carry a transformation family (Story 60.4).
 *
 * There are FOUR families and they live at four addresses: value tables
 * (`value-tables`), cleanup rules (`cleanup-rules`), calculated fields — a
 * `semantic-concept` carrying a formula, inside `concepts` — and the excluded,
 * joined or split columns of a Datastream, which are not a Governance object at
 * all and are named by the `Owned elsewhere` panel below.
 *
 * NO SIXTH LENS GROUPS THEM, and that refusal is measured rather than assumed:
 * `governance.md:37-38` ratifies "exactly four stable Level 2 screens … never as
 * additional permanent navigation", `governance.md:43,84,200` names five
 * Semantic Model lenses and none of them `Transformations`, and a grouping lens
 * would give one object two addresses — breaking the exact lists of
 * `Router.test.tsx:157-234`, `GovernanceScreens.test.tsx:516-529` and
 * `test_governance_read_model.py:300-301,750,937`, and making
 * `_object_detail` replay a grouping adapter on every object opened.
 *
 * What this screen adds instead is the third of the three things
 * `epic-60:109-110` asks for: search was already here, `Used by` is the
 * `Assigned to` of the plan under the name `facetLabel` gives all three facets,
 * and the scope narrowing was missing.
 */
const SCOPE_FILTER_SECTION = "semantic-model";
const SCOPE_FILTER_LENSES = new Set(["concepts", "value-tables", "cleanup-rules"]);

/**
 * Lenses whose own panel already says the empty case, in full (lot A1).
 *
 * The generic empty state below says "Nothing governed here yet / This Project
 * has no object under this lens" — true, and it names no gesture. A lens that
 * mounts a panel stating WHY the list is empty and WHAT fills it would then show
 * two empty states, one of which is worse than the other, and a person reads the
 * last one they see.
 *
 * SIX LENSES, not one: `canonical-fields` was the only entry while five other
 * lenses mounted a panel that already says its own empty case with the gesture
 * that fills it — "No mapping table on this Project yet."
 * (`TransformationsLibrary.tsx:328-329`), "No cleanup rule on this Project yet."
 * (`CleanupRules.tsx:237-238`), "Nothing to arbitrate"
 * (`MdmConflictsPanel.tsx:101-102`), "No destination configured"
 * (`AlertDestinations.tsx:264-265`) and "No mapped concepts yet"
 * (`ProjectMapping.tsx:259`). All five rendered with the generic state stacked
 * underneath.
 *
 * IT SUPPRESSES THE DUPLICATE, NEVER THE ESCAPE. Only ONE of the branches below
 * is a duplicate — "Nothing governed here yet". The others each say something no
 * panel can: which narrowing hid the objects and how to undo it (no panel is
 * filtered by this screen's search or scope), that no owner answered the lens at
 * all, or that the index has not finished reading this Project. Those are the
 * difference between "nobody has declared one" and "I could not read it", which
 * is the whole meaning of an empty Governance screen — so they still render.
 */
const LENSES_OWNING_THEIR_EMPTY_STATE = new Set([
  "canonical-fields",
  "value-tables",
  "cleanup-rules",
  "mapping-coverage",
  "conflicts",
  "data-quality",
]);

/**
 * Whether the explainer above the work is folded away, per section.
 *
 * In `localStorage` and not in the address: it is a reading preference of ONE
 * person on ONE browser, and putting it in the URL would make a shared link
 * carry somebody else's furniture. That is the mirror of this file's rule about
 * filters — a NARROWING is a view others must be able to receive, a disclosure
 * is not.
 *
 * A blocked or full storage is never a reason for the screen to fail: the panel
 * still opens and closes, it just forgets between visits.
 */
const PURPOSE_STORAGE_PREFIX = "toorow.governance.purpose.";

function purposeIsCollapsed(section: string): boolean {
  try {
    return localStorage.getItem(`${PURPOSE_STORAGE_PREFIX}${section}`) === "collapsed";
  } catch {
    return false;
  }
}

function rememberPurposeState(section: string, open: boolean): void {
  try {
    if (open) localStorage.removeItem(`${PURPOSE_STORAGE_PREFIX}${section}`);
    else localStorage.setItem(`${PURPOSE_STORAGE_PREFIX}${section}`, "collapsed");
  } catch {
    // Nothing to remember if nothing can be written.
  }
}

function facetLabel(state: string, count: number): string {
  if (state === "unavailable") return "Unavailable";
  if (state === "empty" || count === 0) return "None";
  return String(count);
}

/**
 * A facet ordered by its NUMBER, never by the word `facetLabel` prints.
 *
 * "Unavailable", "None", "12" sorted as text puts 12 between the two, which is
 * an order nobody asked for. An unavailable facet has no number at all, so it
 * sorts as an absence — last in both directions, by `compareSortValues`.
 */
function facetSortValue(facet: { state: string; count: number }): SortValue {
  return facet.state === "unavailable" ? null : facet.count;
}

/**
 * The seven columns of the generic object list, and what each one orders by.
 *
 * The labels were a bare string array until 2026-08-17 and the cells were seven
 * hand-written `TableCell`s underneath, so a column and its value agreed only by
 * position. They are one declaration now because a sortable header needs a KEY,
 * and a key that lives beside the label is one a cell cannot drift from.
 *
 * The EVIDENCE lenses are not here and do not sort. Their columns come from
 * `EVIDENCE_COLUMNS[lens]` and their cells from `EvidenceRow`, so this screen
 * holds no value to order them by — offering an arrow that read a cell's text
 * would be a promise about `summary` fields it cannot keep. It is a gap, named
 * rather than faked.
 */
const COLLECTION_COLUMNS: ReadonlyArray<{
  key: string;
  label: string;
  sortValue: (item: GovernanceObject) => SortValue;
}> = [
  { key: "object", label: "Object", sortValue: (item) => item.object_ref.label },
  {
    key: "type",
    label: "Type",
    sortValue: (item) => objectLabel(item.object_ref.type),
  },
  { key: "scope", label: "Scope", sortValue: (item) => item.scope },
  { key: "status", label: "Status", sortValue: (item) => stateLabel(item.lifecycle_status) },
  { key: "used-by", label: "Used by", sortValue: (item) => facetSortValue(item.used_by) },
  { key: "versions", label: "Versions", sortValue: (item) => facetSortValue(item.versions) },
  { key: "evidence", label: "Evidence", sortValue: (item) => facetSortValue(item.evidence) },
];

/**
 * The Competitor Registry lens (Story 48.5): the same objects the table below
 * lists, arranged the way the ratified contract asks for -- entities against the
 * Datastreams proven compatible.
 *
 * The Datastream columns come from the bindings the server composed, not from
 * every Datastream in the Project. A column for a Datastream nothing can bind to
 * would read as a gap to fill rather than as a source that cannot see entities
 * at all.
 *
 * THE COLUMN HEADER IS AN IDENTIFIER, AND THAT IS A SERVER GAP, NOT A CHOICE.
 * `governance_read_model.py:3086-3098` composes each cell's `datastream_ref` as
 * `{"object_type": "datastream", "id": …}` and nothing else — no name is on the
 * wire — so a reader of this matrix has to leave for Data to decode `ds_01KYJ…`.
 * The Fed-by panel of the same section has the answer beside it
 * (`datastream_label`, `MasterDataTabs.tsx:441-448`), which is the shape this
 * one owes too. Until the read model carries it, the id is shown AS the id
 * rather than dressed as a name: `ref.label` is read here so the day it is
 * served the column reads it, and nothing else in this file changes.
 */
function competitorMatrix(items: GovernanceObject[]): {
  entities: MatrixEntity[];
  datastreams: MatrixDatastream[];
} {
  const datastreams = new Map<string, MatrixDatastream>();
  const entities: MatrixEntity[] = items.map((item) => {
    const summary = (item.summary ?? {}) as Record<string, unknown>;
    const matrix = Array.isArray(summary.matrix) ? summary.matrix : [];
    const cells = matrix.map((raw) => {
      const cell = raw as Record<string, unknown>;
      const ref = (cell.datastream_ref ?? {}) as Record<string, unknown>;
      const id = String(ref.id ?? "");
      if (id && !datastreams.has(id)) {
        const served = typeof ref.label === "string" ? ref.label.trim() : "";
        datastreams.set(id, { datastream_id: id, label: served || id });
      }
      return {
        datastream_id: id,
        state: String(cell.state ?? "none") as MatrixEntity["cells"][number]["state"],
        direction: (cell.direction as string | null) ?? null,
        report_id: (cell.report_id as string | null) ?? null,
        exception_reason_code: (cell.exception_reason_code as string | null) ?? null,
        exception_reason: (cell.exception_reason as string | null) ?? null,
      };
    });
    const representations = Array.isArray(summary.representations) ? summary.representations : [];
    return {
      entity_id: item.object_ref.id,
      label: item.object_ref.label,
      entity_kind: (summary.entity_kind as string | null) ?? null,
      role: String(summary.project_role ?? "reference"),
      cells,
      representation_count: representations.length,
    };
  });
  return { entities, datastreams: Array.from(datastreams.values()) };
}

export default function GovernanceCollection({
  projectId,
  section,
  lens,
}: {
  projectId: string;
  section: string;
  lens: string;
}) {
  const { route, navigate } = useRoute();
  const isEvidence = section === "evidence";
  const [newConceptOpen, setNewConceptOpen] = useState(false);
  const [newViewOpen, setNewViewOpen] = useState(false);
  const [newDomainOpen, setNewDomainOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  /** Open unless this browser folded THIS section away before. Read once per
   *  section, and written back on every change — a person who folds Master Data
   *  finds Evidence still explaining itself. */
  const [purposeOpen, setPurposeOpenState] = useState(() => !purposeIsCollapsed(section));
  const setPurposeOpen = useCallback(
    (open: boolean) => {
      setPurposeOpenState(open);
      rememberPurposeState(section, open);
    },
    [section],
  );
  useEffect(() => {
    // The four sections share this component instance, so switching screen must
    // re-read the preference rather than carry the previous section's.
    setPurposeOpenState(!purposeIsCollapsed(section));
  }, [section]);
  const serverPaged = section === "master-data";

  /**
   * WHETHER THIS ORGANIZATION'S BUSINESS TAXONOMY HAS CONVERGED (governance.md,
   * amendment of 2026-08-25). It is read for the whole Master Data section and
   * not per lens, because it is a fact about the ORGANIZATION and it decides two
   * different things on this screen: whether the convergence gesture is offered,
   * and whether the create action below can succeed at all.
   */
  const convergence = useMasterDataConvergence(projectId, section === "master-data");
  const notConverged =
    convergence.read.status === "ready" && !planIsConverged(convergence.read.plan);
  /** The two lenses whose objects the convergence moves. The gesture is named
   *  where a person is looking at the identities it is about — and
   *  `business-domains` is this section's default lens, so arriving on Master
   *  Data is enough to meet it. */
  const convergenceLens = section === "master-data" && (lens === "business-domains" || lens === "classifications");

  // The filters live in the ROUTE, not in component state. A lens filtered in
  // `useState` is not a view someone can send you — which is exactly why the
  // previous Evidence screen could not be shared.
  const filters = isEvidence
    ? (route.query ?? {})
    : serverPaged
      ? { ...(route.query ?? {}), limit: "50" }
      : {};

  // MASTER DATA IS THE ONLY SECTION WHOSE SEARCH IS THE SERVER'S. Everywhere
  // else `q` narrows the page already returned, so it is carried by the address
  // and never put in the request: `normalize_filters`
  // (`evidence_index.py:1823-1831`) refuses every key outside `ALLOWED_FILTERS`,
  // so an Evidence request carrying `q` is a 400, not a filtered list.
  const { q: _browserSearch, ...evidenceRequest } = filters;
  const { state, reload } = useGovernanceCollection(
    projectId,
    section,
    lens,
    isEvidence ? evidenceRequest : filters,
  );
  const copy = SECTION_COPY[section] ?? { title: section, description: "" };
  const lenses = sectionLenses("governance", section);

  const envelope = state.status === "ready" ? state.envelope : null;
  const reasons = envelope?.unavailable_reasons ?? [];
  const items = envelope?.items ?? [];

  // THE KEY LISTS THE LIFECYCLES THESE ROWS CARRY, and nothing else. The fixed
  // four-entry list it replaces explained a « Withdrawn » mark to a lens whose
  // every object was current; `StatusLegend` renders nothing below two entries,
  // so a homogeneous collection shows no key at all, which is right.
  const lifecycleLegend = (rows: readonly { lifecycle_status?: string | null }[]): StatusLegendEntry[] => {
    const meaning: Record<Tone, { label: string; meaning: string }> = {
      success: { label: "Current", meaning: "published and bindable as it stands" },
      warning: { label: "Held up", meaning: "draft, blocked, or a lifecycle this console does not know — a person can still move it" },
      error: { label: "Withdrawn", meaning: "archived, refused or revoked — nothing downstream can bind it" },
      neutral: { label: "Retired", meaning: "superseded or switched off on purpose" },
      info: { label: "Not offered here", meaning: "this deployment does not carry the object" },
    };
    const shown = new Set<Tone>();
    for (const row of rows) shown.add(stateTone(row.lifecycle_status));
    return [...shown].map((tone) => ({ tone, ...meaning[tone] }));
  };

  // THE SCOPE NARROWING IS AN ADDRESS, and it is applied here rather than
  // asked of the server. Both halves are measured: the server refuses it
  // (`_COLLECTION_QUERY_KEYS`, `governance_surface_api.py:85-95`, and
  // `governance_read_model.py:2274-2275` raises for any parameter outside
  // Evidence), and honouring it in `useState` would produce a narrowing nobody
  // can send anyone — the mood this file's own header refuses.
  // The SEARCH is a narrowing too, and it used to live in `useState` on every
  // section but Master Data -- so a filtered Concept list, a filtered Rule Set
  // list and a filtered Evidence list were views nobody could send anyone, the
  // very defect the comment above claims this file refuses. The address carries
  // it everywhere now; the filtering below still happens in the browser for the
  // lenses the server does not page.
  useEffect(() => {
    setSearchQuery(route.query?.q ?? "");
  }, [route.query?.q]);

  const scopeAware = serverPaged || (section === SCOPE_FILTER_SECTION && SCOPE_FILTER_LENSES.has(lens));
  const scopeFilter = scopeAware ? (route.query?.scope ?? "") : "";

  /** The scopes the lens ACTUALLY loaded, never a list written here.
   *
   *  A value table is `project` or `organization`
   *  (`governance_read_model.py:1687`), a cleanup rule is `project` and nothing
   *  else (`:1777`), a Concept is `project` or `platform` (`:748`). A hardcoded
   *  vocabulary would offer a choice that narrows to an empty table on two of
   *  the three lenses. The scope carried by the ADDRESS is always kept in the
   *  list, even when no item has it: a shared link may arrive on a lens that
   *  holds none, and hiding the control there would leave a person in front of
   *  an empty table with nothing to undo. */
  const scopeOptions = useMemo(() => {
    if (!scopeAware) return [] as string[];
    const present = new Set(
      serverPaged
        ? (envelope?.filter_options?.scopes ?? [])
        : items.map((item) => item.scope).filter(Boolean),
    );
    if (scopeFilter) present.add(scopeFilter);
    return Array.from(present).sort();
  }, [envelope?.filter_options?.scopes, items, scopeAware, scopeFilter, serverPaged]);

  // One option is not a choice -- EXCEPT when the address is the one carrying
  // it. A shared link can land on a lens that holds no object of its scope:
  // `scopeOptions` is then exactly `[scopeFilter]`, the selector disappeared,
  // and the empty state below kept telling the person to "set the scope back to
  // Any" with nothing left to set it with. A control that undoes the address is
  // an escape, not a choice, and it is offered whenever there is something to
  // escape from.
  const showScopeFilter = scopeOptions.length > 1 || Boolean(scopeFilter);

  const filteredItems = useMemo(() => {
    if (serverPaged) return items;
    const scoped = scopeFilter ? items.filter((item) => item.scope === scopeFilter) : items;
    if (!searchQuery.trim()) return scoped;
    const q = searchQuery.toLowerCase();
    return scoped.filter(
      (item) =>
        item.object_ref.label.toLowerCase().includes(q) ||
        item.object_ref.type.toLowerCase().includes(q) ||
        item.object_ref.id.toLowerCase().includes(q) ||
        item.scope.toLowerCase().includes(q) ||
        item.lifecycle_status.toLowerCase().includes(q)
    );
  }, [items, searchQuery, scopeFilter, serverPaged]);

  /**
   * The reader's order over the rows THIS PAGE holds.
   *
   * Every lens but Master Data is paged in the browser, so a sort there covers
   * the whole lens. Master Data is paged by the SERVER (`limit: 50` above, a
   * cursor below), so it does not — and the sort is offered there anyway, with
   * `SortScopeNote` under the table saying which rows moved. The alternative was
   * to take the arrows away on the one lens whose lists are long enough to need
   * them, which is the wrong half of the trade; what makes it honest is the
   * sentence, not the absence of the control.
   */
  const { sort, toggleSort } = useTableSort();
  const sortedItems = useMemo(
    () =>
      sortRows(filteredItems, sort, (item, key) => {
        const column = COLLECTION_COLUMNS.find((candidate) => candidate.key === key);
        return column ? column.sortValue(item) : null;
      }),
    [filteredItems, sort],
  );
  /** True when the rows on screen are a page of a longer answer. */
  const orderIsPageWide = !isEvidence && (serverPaged || Boolean(envelope?.next_cursor));

  /** True when the generic empty state would repeat, worse, what the lens's own
   *  panel already says. Every other reason for an empty list survives it. */
  const genericEmptyStateIsDuplicate =
    LENSES_OWNING_THEIR_EMPTY_STATE.has(lens)
    && !searchQuery
    && !scopeFilter
    && envelope?.coverage.state !== "unavailable"
    && envelope?.coverage.index_state !== "backfilling";

  const lensTabs = useMemo<NavTab[]>(
    () =>
      lenses.map((candidate) => ({
        key: candidate.slug,
        label: candidate.label,
        href: buildPath({
          ...route,
          section,
          lens: candidate.slug,
          objectType: null,
          objectId: null,
          tab: null,
          versionId: null,
          action: null,
          // Switching lens drops the filter set: a cursor and a correlation
          // filter minted for one lens mean nothing in another, and carrying
          // them would produce an address the server refuses.
          query: {},
        } as CanonicalRoute),
      })),
    [lenses, route, section],
  );

  const openObject = (item: GovernanceObject) =>
    navigate({
      workspace: "governance",
      section,
      lens: null,
      objectType: item.object_ref.type,
      objectId: item.object_ref.id,
      tab: null,
      versionId: null,
      action: null,
      query: {},
    });

  const objectHref = (item: GovernanceObject) =>
    buildPath({
      ...route,
      section,
      lens: null,
      objectType: item.object_ref.type,
      objectId: item.object_ref.id,
      tab: item.default_tab,
      versionId: null,
      action: null,
      query: {},
    } as CanonicalRoute);

  /** A filter change is a NAVIGATION. It replaces the address, so the browser's
   *  back button undoes a filter the way a person expects it to. */
  const applyFilters = (next: Record<string, string>) =>
    navigate({ section, lens, objectType: null, objectId: null, tab: null, versionId: null, action: null, query: next });

  /** Same rule for the scope narrowing, and for the same reason: it is a
   *  navigation, so the back button undoes it and the address bar carries it. */
  const applyScope = (value: string) =>
    navigate({
      section,
      lens,
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
      query: {
        // EVERY section, since the search moved into the address: changing the
        // scope must not silently drop the narrowing the person typed.
        ...(route.query?.q ? { q: route.query.q } : {}),
        ...(value ? { scope: value } : {}),
      },
    });

  if (state.status === "denied") {
    return <RouteState kind="denied" onBackToOverview={() => navigate({ workspace: "overview", section: "project-overview", lens: null, objectType: null, objectId: null, tab: null, versionId: null, action: null, query: {} })} />;
  }
  if (state.status === "unknown") {
    return <RouteState kind="unknown" onBackToOverview={() => navigate({ workspace: "overview", section: "project-overview", lens: null, objectType: null, objectId: null, tab: null, versionId: null, action: null, query: {} })} />;
  }

  const renderHeaderActions = () => {
    if (section === "semantic-model") {
      return (
        <div className="flex gap-2">
          <Button variant="default" onClick={() => setNewConceptOpen(true)}>
            + New Concept
          </Button>
          <Button variant="secondary" onClick={() => setNewViewOpen(true)}>
            + New View
          </Button>
        </div>
      );
    }
    if (section === "master-data") {
      // AN ORGANIZATION THAT HAS NOT CONVERGED CANNOT BE WRITTEN, so the door is
      // not drawn. `governance.md`, ratified 2026-08-25: *"The legacy writers
      // refuse from now on (409 naming the convergence gesture); an unconverged
      // organization converges first, then writes through the Master Data
      // authority."* This dialog is a door onto exactly those writers
      // (`POST /api/context/business-domains`), so on an unconverged
      // organization it opens onto a refusal. The one gesture that works is the
      // convergence, and the panel below carries it.
      //
      // It is withheld only on a MEASURED state: a plan that could not be read
      // leaves the button where it is, because removing a gesture on a failure
      // to read would take away a control on no evidence.
      if (notConverged) return null;
      return (
        <div className="flex gap-2">
          <Button variant="default" onClick={() => setNewDomainOpen(true)}>
            + New Domain / Taxonomy
          </Button>
        </div>
      );
    }
    return null;
  };

  return (
    <Stack className="gap-6" data-owner={`governance/${section}`}>
      <div className="flex items-start justify-between">
        <PageHeader
          title={copy.title}
          description={copy.description}
/* Governance's own words for the lifecycle scale, over the lifecycle
             words THESE rows carry. `blocked` sits under « Held up » since 76-2
             withdrew this screen's `error` override: a blocked lifecycle is
             repairable, and `archived` is the word that is not. */
          legend={<StatusLegend label="What a lifecycle mark means" entries={lifecycleLegend(items)} />}
        />
        {renderHeaderActions()}
      </div>

      {/* WHAT THE SCREEN IS FOR, ONCE — and then only when it is wanted.
          The explainer is right on a first visit and is furniture on the
          fiftieth: it pushed the lens tabs, the search bar and the table below
          the fold every single time, on the four screens a governance operator
          lives in. It is collapsible, its state is remembered per SECTION (the
          four say different things, so hiding one is not hiding the others), and
          it is OPEN by default — a person who has never met this screen must not
          have to find the disclosure to be told what it does. Nothing is
          removed: every word is still one click away. */}
      {copy.purposeTitle && (
        <Collapsible open={purposeOpen} onOpenChange={setPurposeOpen}>
          <Panel flush>
            <PanelHeader
              title={copy.purposeTitle}
              description={purposeOpen ? copy.purposeText : undefined}
              actions={
                <CollapsibleTrigger asChild>
                  <Button variant="ghost" size="sm" data-testid="purpose-toggle">
                    {purposeOpen ? "Hide" : "Show"}
                  </Button>
                </CollapsibleTrigger>
              }
            />
            <CollapsibleContent>
              <PanelBody>
                <div className="grid gap-4 md:grid-cols-3">
                  {copy.highlights.map((h, i) => (
                    <div key={i} className="rounded-lg border border-divider-base bg-surface-subtle/40 p-4">
                      <strong className="block text-ui font-semibold text-text">{h.label}</strong>
                      <p className="mt-1 mb-0 text-caption text-text-secondary">{h.detail}</p>
                    </div>
                  ))}
                </div>
              </PanelBody>
            </CollapsibleContent>
          </Panel>
        </Collapsible>
      )}

      {/* `query: {}` on both paths, the anchor and the handler: a scope or a
          cursor minted for one lens means nothing in another, and the two ways
          of switching lens must not disagree about that. */}
      <NavTabs label={`${copy.title} lenses`} tabs={lensTabs} current={lens} onNavigate={(next) => navigate({ section, lens: next, objectType: null, objectId: null, tab: null, versionId: null, action: null, query: {} })} />

      {/* THE RATIFIED OPERATOR GESTURE, above the objects it is about
          (governance.md, 2026-08-25: "an operator runs the convergence per
          organization from the Master Data screen"). It sits above the list and
          not inside it: it is a statement about the ORGANIZATION, and the rows
          below are the identities it would move. It draws nothing at all for an
          organization that never held a legacy taxonomy.

          It is drawn on the two lenses it is about in every state — and on
          EVERY Master Data lens while the organization has not converged,
          because that is where the section's create action disappears, and a
          control that vanishes without a sentence is a gesture a person
          hunts for. */}
      {(convergenceLens || notConverged) && (
        <MasterDataConvergencePanel
          projectId={projectId}
          read={convergence.read}
          onReload={convergence.reload}
          onConverged={reload}
        />
      )}

      {/* Dialogues de création */}
      <NewConceptDialog
        open={newConceptOpen}
        projectId={projectId}
        onClose={() => setNewConceptOpen(false)}
        onCreated={reload}
      />
      <NewSemanticViewDialog
        open={newViewOpen}
        projectId={projectId}
        onClose={() => setNewViewOpen(false)}
        onCreated={reload}
      />
      <NewBusinessDomainDialog
        open={newDomainOpen}
        projectId={projectId}
        onClose={() => setNewDomainOpen(false)}
        onCreated={reload}
      />

      {state.status === "loading" && (
        <p role="status" className="text-body text-text-secondary">Loading {copy.title}…</p>
      )}
      {state.status === "error" && (
        <Status as="block" tone="error" title={`${copy.title} is unavailable`} action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          {state.message} No substitute content has been shown.
        </Status>
      )}

      {envelope && (
        <>
          {/* A REASON IS A BANNER ONLY WHEN THE LENS COULD NOT BE READ. A lens
              that WAS read and holds nothing may carry a reason too — the
              sentence that says why it is empty and names the gesture that fills
              it — and that sentence belongs in the empty state below, beside the
              list it is about, not in a warning above a table that is fine.
              Drawing both would say the same thing twice, in two tones.

              The title is no longer "Not delivered yet": that is a release
              state, and none of the reasons that reach here is one — a store
              that could not be read and a capability the Project disabled are
              both about THIS Project, today, and a person can act on both. */}
          {envelope.coverage.state === "unavailable" &&
            reasons.map((reason) => (
              <Status key={reason.code} as="block" tone="warning" title="Why this lens shows nothing" data-testid={`reason-${reason.code}`}>
                {reason.message}
              </Status>
            ))}

          <Panel flush>
            <PanelHeader
              title={`${lenses.find((candidate) => candidate.slug === lens)?.label ?? lens}`}
              description={
                envelope.evidence_as_of
                  ? `Evidence as of ${formatTimestamp(envelope.evidence_as_of)}`
                  : "No evidence timestamp is available for this lens."
              }
            />

            {/* Instant Search Bar. The scope selector sits beside it, and only
                ever inside this bar: an envelope that could not be read renders
                no bar at all — that case never reaches here, it takes the
                `Status tone="error"` branch above with no envelope, and a filter
                drawn over it would read as "no object matches" when the truth is
                "this lens could not be read".

                It IS drawn when a narrowing arrived in the address and matched
                nothing. That is not a filter over a nothing, it is the only way
                back: the empty state below tells the person to set the scope
                back to Any, and until 2026-08-17 the control that does it was
                removed from under that sentence exactly when it was needed. */}
            {(items.length > 0 || serverPaged || Boolean(scopeFilter) || Boolean(searchQuery)) && (
              <div className="flex items-center justify-between border-b border-divider-base bg-surface-subtle/30 px-5 py-2.5">
                <div className="flex items-center gap-3">
                  <div className="w-full max-w-sm">
                    <Input
                      type="text"
                      placeholder="🔍 Instant Search (concepts, views, scope...)"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      className="h-8 text-caption"
                      onKeyDown={(event) => {
                        // Every section, not only the server-paged one: pressing
                        // Enter is what turns a narrowing into an address someone
                        // can send.
                        if (event.key === "Enter") {
                          applyFilters({
                            ...(searchQuery.trim() ? { q: searchQuery.trim() } : {}),
                            ...(scopeFilter ? { scope: scopeFilter } : {}),
                          });
                        }
                      }}
                    />
                  </div>
                  {serverPaged ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      onClick={() => applyFilters({
                        ...(searchQuery.trim() ? { q: searchQuery.trim() } : {}),
                        ...(scopeFilter ? { scope: scopeFilter } : {}),
                      })}
                    >
                      Search
                    </Button>
                  ) : null}
                  {showScopeFilter && (
                    <label className="flex items-center gap-2 text-caption text-text-secondary">
                      <span>Scope</span>
                      <NativeSelect
                        aria-label="Scope"
                        data-testid="scope-filter"
                        className="h-8 text-caption"
                        value={scopeFilter}
                        onChange={(event) => applyScope(event.target.value)}
                      >
                        <option value="">Any scope</option>
                        {scopeOptions.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </NativeSelect>
                    </label>
                  )}
                </div>
                <span className="text-caption font-mono text-text-secondary">
                  {serverPaged && envelope.coverage.total !== null
                    ? `Showing ${envelope.coverage.returned} of ${envelope.coverage.total} matching items`
                    : `Showing ${filteredItems.length} of ${items.length} items`}
                </span>
              </div>
            )}

            {/* MOUNTED ON THE ROWS IT DRAWS, not on the rows that were loaded.
                The condition tested `items` while the matrix was built from
                `filteredItems`, so a narrowing that matched nothing rendered an
                empty grid frame with its three selectors above the empty state —
                two answers to one question, and the emptier one on top. */}
            {lens === "competitor-registry" && filteredItems.length > 0 && (
              <div className="p-4">
                <EntityMatrix {...competitorMatrix(filteredItems)} />
              </div>
            )}
            {/*
              Story 48.2 AC1 — Country has ONE editor. The `country-market` lens
              rendered here was never declared in `navigation.ts`, so no lens tab
              could reach it, and the screen it mounted read the
              `project_preferences` geography — the read-layer authority this
              same story retired (Task 2). The governed editor is the Country
              Registry: Governance › Master Data › Registries, object type
              `registry`, tab `hierarchy` (`CountryWorkspace`).
            */}
            {/* STORY 75-2: what is waiting to be decided, above the Concepts it
                would become. Governance has exactly four Level 2 screens and
                `governance.md` keeps optional capabilities INSIDE them, so the
                promotion queue is a panel of this lens rather than a fifth
                screen -- the shape `UnresolvedValuesPanel` takes in Value
                Tables, and for the same reason: the work sits above the
                objects. */}
            {lens === "concepts" && (
              <div className="p-4">
                <CalculatedFieldProposalsQueue projectId={projectId} />
              </div>
            )}
            {lens === "mapping-coverage" && (
              <div className="p-4">
                <ProjectMapping projectId={projectId} />
              </div>
            )}
            {/* STORY 59.6: where the alerts these monitors write actually go.
                It renders INSIDE the Data Quality lens rather than as a fifth
                lens, because Governance has exactly four Level 2 screens and
                "object types and optional capabilities appear inside them, never
                as additional permanent navigation" (`governance.md`). Its
                surface is Governance and not Project Settings because
                `alignment-register.md:98` is ratified and says "alert rules in
                Governance" — adding a fourth Project Settings section would have
                amended a ratified document AND two separate declarations of the
                same union (`ProjectSettings.tsx:26`, `router.tsx:7`). */}
            {/* STORY 60.1: the client's OWN value mapping tables, inside the
                lens that carries them. The generic row list above states what
                each table IS; this editor is where a pair, an import and an
                assignment are written, and where a deletion names the number of
                Datastreams BEFORE it happens. */}
            {lens === "value-tables" && (
              <div className="p-4">
                {/* S2 of `unresolved-values.md` — the SAME panel as the Workbench
                    `Map` tab, one projection wider, and it sits ABOVE the table
                    list (:379, :420-428): here a person is looking at the tables,
                    and the panel tells them which table is short of which rows.
                    The work sits above the objects.

                    Every row deep-links to S1 on the Datastream that emitted the
                    value — one address per value, never two lists disagreeing
                    about a count. The address is built by the router and never
                    composed inside the panel. */}
                <UnresolvedValuesPanel
                  projectId={projectId}
                  scope="project"
                  onOpenDatastream={(datastreamId) =>
                    navigate({
                      workspace: "data",
                      section: "datastreams",
                      objectType: "datastream",
                      objectId: datastreamId,
                      tab: "mapping",
                      lens: null,
                      versionId: null,
                      action: null,
                    })
                  }
                />
                <TransformationsLibrary projectId={projectId} />
              </div>
            )}
            {/* STORY 60.3: the cleanup rules, and the ONLY place one is written.
                The `Processing` tab of the Workbench shows the same rules with
                no control at all — `datastream-workbench-and-wizard.md:989`
                ("Governed rules are referenced, not edited") and `data.md:82-85`
                ("it does not redefine it"). Two editing authorities over one
                rule is how a Datastream ends up disagreeing with Governance
                about what it removes. */}
            {lens === "cleanup-rules" && (
              <div className="p-4">
                <CleanupRules projectId={projectId} />
              </div>
            )}
            {/* LOT A1 (issue #68): the vocabulary every mapping is validated
                against, listed for the first time. It reads its own route rather
                than this envelope because the two answer different questions —
                the generic row above says what an object IS and where it opens,
                and this one says the kind, the value type and the aggregation,
                which are what decide whether a field may be bound and summed.
                Same pattern as the two lenses above it. */}
            {lens === "canonical-fields" && (
              <div className="p-4">
                <CanonicalFields projectId={projectId} />
              </div>
            )}
            {/* AUDIT MDM & GOUVERNANCE, 2026-08-14. `governance.md:528` donne a
                cet ecran « mapping conflicts and approval cases » -- la
                DECISION -- et la lentille `conflicts` etait declaree dans
                `navigation.ts` sans rien rendre : elle tombait sur la liste
                generique, qui ne porte aucun conflit MDM. Le seul endroit qui
                les montrait etait Mapping Coverage, a qui `governance.md:75`
                donne la PROJECTION. Meme route, meme lecture, meme geste : deux
                surfaces, un seul ecrivain. */}
            {lens === "conflicts" && (
              <div className="p-4">
                <MdmConflictsPanel projectId={projectId} />
              </div>
            )}
            {lens === "data-quality" && (
              <div className="p-4">
                <AlertDestinations projectId={projectId} />
              </div>
            )}
            {/* STORY 59.1, ARBITRAGE 5: `DqIncidentPanel` is gone from here.
                It called three routes story 49.4 unmounted (`/api/dq/summary`,
                `/api/dq/evaluate`, `/api/dq/issues/{id}/acknowledge`) — three
                404s on a ratified lens — and the one that carried the word
                acknowledged `app.alert_firings`, a different table under the
                same word. Keeping it while the same gesture ships inside the run
                would have given two acknowledgement authorities, one of them
                broken. The acknowledgement now lives where the anomaly is: on
                the run that found it. */}
            {isEvidence && (
              <div className="border-b border-divider-base p-4">
                {/* The bar reads the vocabulary the ENVELOPE declares, rather
                    than a copy of the server's lists kept in the browser. */}
                <EvidenceFilters
                  lens={lens}
                  filters={filters}
                  items={filteredItems}
                  options={envelope?.filter_options}
                  onChange={applyFilters}
                />
              </div>
            )}
            {filteredItems.length === 0 && genericEmptyStateIsDuplicate ? null : filteredItems.length === 0 ? (
              <EmptyState
                title={
                  searchQuery
                    ? `No items matching "${searchQuery}"`
                    : // THREE SENTENCES, AND NONE OF THEM IS THE OTHER. A lens
                      // with no object says "Nothing governed here yet"; a
                      // narrowing that matched nothing says which narrowing,
                      // because the objects are there and the person put them
                      // out of sight themselves; an unreadable envelope never
                      // reaches this branch at all.
                      scopeFilter
                    ? `No object of this lens has scope ${scopeFilter}`
                    : envelope.coverage.state === "unavailable"
                    ? "No owner answers this lens yet"
                    : envelope.coverage.index_state === "backfilling"
                      ? "The Evidence index has not finished registering this Project"
                      : isEvidence && Object.keys(filters).length > 0
                        ? "No Evidence Record matches these filters"
                        : "Nothing governed here yet"
                }
                description={
                  searchQuery
                    ? "Try adjusting your search query or clear the filter."
                    : scopeFilter
                    ? serverPaged
                      ? "The server applied this scope and found no matching object. Set the scope back to Any to see the whole lens."
                      : "Every object this lens loaded carries another scope. Set the scope back to Any to see the whole lens — this narrowing happens in the browser and was never asked of the server, so nothing was hidden by an owner."
                    : // The owner's own sentence wins whenever it exists, and it
                      // exists for an empty lens as well as an unreadable one:
                      // an empty list that says why it is empty and names the
                      // gesture that fills it is the whole point of sending one.
                      reasons[0]?.message
                    ? reasons[0].message
                    : envelope.coverage.index_state === "backfilling"
                      ? "Records already indexed are exact. This is not a Project with no evidence — it is a Project the index has not finished reading."
                      : isEvidence && Object.keys(filters).length > 0
                        ? "The server applied every filter in this address and found nothing. Clear them to see the whole lens."
                        : convergenceLens && notConverged
                          ? // THE GESTURE THAT FILLS THIS LIST IS NOT THE ONE
                            // THAT USED TO. While this organization has not
                            // converged, writing a Business Domain or a
                            // Classification is refused (governance.md,
                            // 2026-08-25) and the create action above is not
                            // drawn — so an empty list whose sentence still
                            // pointed at it would name a gesture that answers
                            // 409. It names the convergence instead, which is
                            // the one act that unblocks the rest.
                            "This organization has not converged into Master Data yet, so nothing can be declared here until it does. Run the convergence above. Nothing has been hidden or substituted."
                        : lens === "registries"
                          ? // TWO gestures fill this lens, and naming only one
                            // sent a reader to Capabilities for something no
                            // capability can give them (AI-232). `governance.md`
                            // — "a registry is listed whether a project
                            // capability pins it or a client declared it".
                            // Declaring a kind has no screen; it is asked of the
                            // assistant, and the sentence says so rather than
                            // implying a control that does not exist.
                            "Registries appear here in two ways: enable a Project capability that owns one, in Project Settings › Capabilities — or declare your own object kind, such as a video, a venue or a product, by asking the assistant for it."
                          : "This Project has no object under this lens. Nothing has been hidden or substituted."
                }
              />
            ) : (
              <TableScroll label={`${copy.title} ${lens} objects`}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      {isEvidence
                        ? (EVIDENCE_COLUMNS[lens] ?? []).map((column) => (
                            <TableHead key={column}>{column}</TableHead>
                          ))
                        : COLLECTION_COLUMNS.map((column) => (
                            <SortableHead
                              key={column.key}
                              sortKey={column.key}
                              sort={sort}
                              onSort={toggleSort}
                            >
                              {column.label}
                            </SortableHead>
                          ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sortedItems.map((item) => {
                      const nameCell = (
                        <a
                          className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                          href={objectHref(item)}
                          onClick={(event) => {
                            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                            event.preventDefault();
                            openObject(item);
                          }}
                        >
                          {item.object_ref.label}
                        </a>
                      );
                      const key = `${item.object_ref.type}:${item.object_ref.id}`;
                      if (isEvidence) {
                        return <EvidenceRow key={key} item={item} lens={lens} nameCell={nameCell} />;
                      }
                      return (
                        <TableRow key={key}>
                          <TableCell>{nameCell}</TableCell>
                          <TableCell className="text-text-secondary"><ObjectTypeName objectType={item.object_ref.type} /></TableCell>
                          <TableCell className="text-text-secondary">{scopeLabel(item.scope)}</TableCell>
                          <TableCell>
                            {/* The console's sentence for the state, not the
                                column value in lower case. `not_current` read
                                as "not current" here and as "Not current" on
                                every Data screen — one fact under two
                                spellings. */}
                            <Status tone={stateTone(item.lifecycle_status)}>
                              {stateLabel(item.lifecycle_status)}
                            </Status>
                          </TableCell>
                          <TableCell className="font-numeric text-text-secondary">{facetLabel(item.used_by.state, item.used_by.count)}</TableCell>
                          <TableCell className="font-numeric text-text-secondary">{facetLabel(item.versions.state, item.versions.count)}</TableCell>
                          <TableCell className="font-numeric text-text-secondary">{facetLabel(item.evidence.state, item.evidence.count)}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
            {/* WHAT THE ARROWS ACTUALLY PROMISE, said where they are used. On
                Master Data the server chose these fifty rows and holds the rest
                behind a cursor, so an order composed here covers the page and
                not the lens. Without this sentence a reader sorting by Status
                would conclude the lens has no blocked object. */}
            {orderIsPageWide && sortedItems.length > 0 ? (
              <div className="border-t border-divider-base px-5 py-2.5">
                <SortScopeNote />
              </div>
            ) : null}
          </Panel>

          {/* Announced, not merely rendered: a filter that changes the result
              count silently leaves a screen-reader user on the old number. */}
          <p role="status" aria-live="polite" className="text-caption text-text-secondary">
            {envelope.coverage.total !== null ? (
              <>
                {envelope.coverage.returned} of {envelope.coverage.total} objects shown
                {envelope.coverage.total > envelope.coverage.returned ? " — the remainder is bounded, not absent." : "."}
              </>
            ) : (
              <>
                {envelope.coverage.returned} objects shown. The total could not be proven for this
                caller, so none is stated — an undercount is not a count.
              </>
            )}
          </p>

          {/* THE SAME CONTROL AS THE DATA COLLECTIONS, and it says the same
              thing about what it cannot do. There is no Previous here and there
              never was: a cursor is the server's place-marker and this screen
              keeps no stack of the ones it has spent, so `onPrevious` is left
              undefined and the control is not drawn — a disabled Previous would
              promise a gesture that does not exist. `First` is the way back,
              and it is drawn from the first page on, dead until there is a
              cursor to abandon. The request is unchanged: both handlers
              navigate, exactly as before, because the address is what carries a
              page here. */}
          {(isEvidence || serverPaged) && (envelope.next_cursor || route.query?.cursor) && (
            <Pager
              label={`${copy.title} pages`}
              onFirst={
                serverPaged
                  ? route.query?.cursor
                    ? () => applyFilters({
                        ...(route.query?.q ? { q: route.query.q } : {}),
                        ...(route.query?.scope ? { scope: route.query.scope } : {}),
                      })
                    : null
                  : undefined
              }
              onNext={
                envelope.next_cursor
                  ? () => applyFilters({ ...filters, cursor: envelope.next_cursor as string })
                  : null
              }
            />
          )}

          {isEvidence && (envelope.coverage.adapters?.length ?? 0) > 0 && (
            <Panel flush>
              <PanelHeader
                title="What each Evidence adapter proved"
                description={
                  envelope.coverage.evidence_horizon
                    ? `Evidence on this page reaches back to ${formatTimestamp(envelope.coverage.evidence_horizon)}. An owner named unavailable here has not been indexed — that is not a count of zero.`
                    : "An owner named unavailable here has not been indexed. That is not a count of zero."
                }
              />
              <EvidenceCoverage adapters={envelope.coverage.adapters ?? []} />
            </Panel>
          )}
        </>
      )}

      <Panel flush>
        <PanelHeader title="Owned elsewhere" description="Governance projects these; it never becomes their writer." />
        <PanelBody className="flex flex-col gap-3">
          {(SECTION_OWNERSHIP[section] ?? []).map((note) => {
          const target = ownershipTarget(note);
          const noteHref = buildPath({ ...route, workspace: note.workspace, section: note.section, lens: target.lens, objectType: null, objectId: null, tab: null, versionId: null, action: null, query: {} } as CanonicalRoute);
          const openNote = () => navigate({ workspace: note.workspace, section: note.section, lens: target.lens, objectType: null, objectId: null, tab: null, versionId: null, action: null, query: {} });
          return (
            <div
              key={note.label}
              data-testid={`owned-elsewhere-${note.label.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}`}
              className="flex flex-col gap-2 rounded-lg border border-divider-base bg-surface-subtle/30 p-4 transition-colors hover:bg-surface-subtle/70 md:flex-row md:items-center md:justify-between"
            >
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="rounded bg-surface-light border border-divider-base px-2 py-0.5 text-caption font-semibold text-text-secondary">
                    External Ownership
                  </span>
                  <a
                    className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                    href={noteHref}
                    onClick={(event) => {
                      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                      event.preventDefault();
                      openNote();
                    }}
                  >
                    {note.label}
                  </a>
                </div>
                <p className="m-0 text-caption text-text-secondary">{note.detail}</p>
                {/* The tab is NAMED from the route registry and deliberately not
                    put in the address: a tab without an object identifier is
                    refused (`ContentRouter.tsx:174-184`), and this envelope
                    carries no Datastream reference. So the link opens the list
                    of the objects that own the tab, and the sentence says what
                    continues from there rather than promising a door that would
                    silently open the collection anyway. */}
                {target.tabLabel && note.objectTab && (
                  <p className="m-0 text-caption text-text-secondary">
                    Edited in the {target.tabLabel} tab of one{" "}
                    {note.objectTab.type[0].toUpperCase() + note.objectTab.type.slice(1)} at a time.
                    This link opens the list, because Governance holds no identifier for one of them
                    here.
                  </p>
                )}
              </div>

              <a
                className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-control border border-divider-base bg-surface-light px-3 py-1.5 text-label font-semibold text-text transition-colors hover:border-primary hover:text-primary focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                href={noteHref}
                onClick={(event) => {
                  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  openNote();
                }}
              >
                Open in {note.workspace} &rarr;
              </a>
            </div>
          );
          })}
        </PanelBody>
      </Panel>
    </Stack>
  );
}
