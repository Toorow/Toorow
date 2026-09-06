/**
 * Display vocabulary for the Governance route contracts.
 *
 * The route SHAPE lives in `shell/navigation.ts` and nowhere else; this file
 * only says how each registered slug reads in English, and — for a tab the
 * route contracts but no story has filled — which story owns it.
 *
 * `PENDING_TAB_OWNER` is deliberately data, not a `default:` branch. A tab
 * swallowed by a fallback looks delivered; a tab that names the story it is
 * waiting for is the inventory of what is left to build.
 */
import { findObjectContract, objectTypeLabel, sectionOwnsLens } from "../shell/navigation";
import { wireWord } from "../ui/glossary";

/** What a type nobody declared reads as. Never the token on its own: a raw
 *  `value-mapping-tabel` looks like a word somebody chose, and the person
 *  reading it cannot tell a typo from an object. */
export const UNKNOWN_OBJECT_TYPE = "Unknown object type";

/** The noun a person reads for an object type.
 *
 *  ONE ANSWER, ONE STORE. This function used to read the registry first and its
 *  OWN 14-entry map second — "ONE ANSWER, TWO STORES, AND THE REGISTRY WINS" is
 *  what the header said, and a store that wins is still a store: the crumb read
 *  `sentenceCase` (`Semantic view`) while this read the map (`Semantic View`),
 *  which is two spellings of one answer on two halves of one screen. Story 76-3
 *  moved all 14 onto their route contracts (`shell/navigation/governance.ts`)
 *  and deleted the map; `console-presentation.md` §4 ratifies the deletion.
 *
 *  A type declared nowhere returns `Unknown object type` rather than its token,
 *  so the missing declaration is VISIBLE and attributable. The token itself is
 *  not thrown away — `ObjectTypeName` (`shell/ObjectTypeName.tsx`) prints it in
 *  monospace beside the words, through the console's one identifier rendering. */
export function objectLabel(objectType: string): string {
  return objectTypeLabel(objectType) ?? UNKNOWN_OBJECT_TYPE;
}

/** The SCOPE a governed object lives in, as a person reads it.
 *
 *  One declared place, because three screens print the same field: the
 *  Governance collection's Scope column, the object workbench's Scope block and
 *  the mapping panel's identity-candidate badge. Each printed the stored token.
 *
 *  The words are the server's own: `governance_read_model.py:1117` defaults the
 *  field to `project`, and the emitted values across `server/core` are
 *  `organization | project | platform | datastream | connector | principal |
 *  session`. Every one of them is already a ratified product noun, so this map
 *  only takes the base's lower case off — it decides nothing. A value outside
 *  the set keeps its own spelling rather than being renamed by a fallback
 *  nobody chose. */
const SCOPE_LABEL: Record<string, string> = {
  organization: "Organization",
  project: "Project",
  platform: "Platform",
  datastream: "Datastream",
  connector: "Connector",
  principal: "Principal",
  session: "Session",
  node: "Node",
};

export function scopeLabel(scope: string | null | undefined): string {
  if (!scope) return "";
  return SCOPE_LABEL[scope] ?? wireWord(scope);
}

export const TAB_LABEL: Record<string, string> = {
  overview: "Overview",
  definition: "Definition",
  evidence: "Evidence",
  hierarchy: "Hierarchy",
  "mappings-aliases": "Mappings & Aliases",
  "used-by": "Used by",
  versions: "Versions",
  semantics: "Semantics",
  "source-bindings": "Source Bindings",
  "metrics-dimensions": "Metrics & Dimensions",
  "candidate-change": "Candidate Change",
  impact: "Impact",
  "decision-history": "Decision History",
  coverage: "Coverage",
  history: "History",
  issues: "Issues",
  rules: "Rules",
  "effective-dates": "Effective Dates",
  "approvals-exceptions": "Approvals & Exceptions",
  lineage: "Lineage",
  provenance: "Provenance",
  diff: "Diff",
  approvals: "Approvals",
};

export interface PendingOwner {
  story: string;
  what: string;
}

/** `${objectType}:${tab}` → the story that owns that tab's domain content.
 *  A tab absent from this map is one Story 49.1 itself fills. */
export const PENDING_TAB_OWNER: Record<string, PendingOwner> = {
  // EMPTY, and that is a result rather than an oversight. Every contracted
  // Governance tab now has an owner that ships it:
  //
  //   Semantic Model  -> Story 49.3   (SemanticModelTabs.tsx)
  //   Controls & Quality -> Story 49.4 (ControlsQualityTabs.tsx)
  //   Evidence        -> Story 49.5   (EvidenceTabs.tsx)
  //   Master Data     -> Story 49.2   (MasterDataTabs.tsx) -- the last six
  //
  // The map stays because the mechanism must survive its own success: the next
  // contracted tab without an owner belongs here, named, rather than falling
  // into a `default:` branch that would make it look delivered.
  //
  // Removing an entry is only legitimate when the tab actually ships. What a
  // shipped tab still CANNOT answer is said by the tab itself -- a Business
  // Domain has no alias store, and its Mappings & Aliases tab reports that
  // missing store instead of rendering an empty table.
};

export function pendingOwner(objectType: string, tab: string): PendingOwner | null {
  return PENDING_TAB_OWNER[`${objectType}:${tab}`] ?? null;
}

/** What each collection hands off, and to whom. Governance projects these; it
 *  never becomes their second writer. */
export interface OwnershipNote {
  label: string;
  workspace: "data" | "context-hub" | "test" | "analyze";
  section: string;
  /** The LENS of the destination collection, when the destination declares one.
   *
   *  A reference that stops at the section lands on that section's declared
   *  default lens — a different collection from the one the note is about. That
   *  is the dead end story 58.9 paid for with `Add a check` and story 60.3 paid
   *  for again with the cleanup rules step, which is why
   *  `WorkbenchProcessingPage.tsx:23-31` carries a lens in its owner reference.
   *  Validated against the registry before it enters an address, never trusted. */
  lens?: string;
  /** The object TAB that owns the handed-off family, when the family is edited
   *  inside one object's workbench rather than on the destination collection. */
  objectTab?: { type: string; tab: string };
  detail: string;
}

/** What a note may honestly address, judged by the route registry.
 *
 *  TWO ANSWERS, AND THE SECOND ONE IS NOT AN ADDRESS. A lens is put into the
 *  address when the destination declares it, and dropped otherwise. A tab is
 *  NAMED and never put into the address: `ContentRouter.tsx:174-184` refuses a
 *  tab without an object identifier, and this panel holds none — the Governance
 *  semantic-model envelope carries counts and no Datastream reference
 *  (`governance_read_model.py:1710-1723`). Writing `tab` into the address would
 *  be dropped by the router and land on the collection anyway: the same place,
 *  reached by a lie. So the link opens the deepest address that EXISTS, the
 *  collection that lists the objects carrying that tab, and the tab is named
 *  from the registry — a tab this repository does not declare produces no
 *  sentence at all rather than a name nothing answers to.
 *
 *  The label is title-cased from the registered slug, the same rule the
 *  Workbench band applies (`shell/pages/datastreamTabs.ts:73`), so one tab has
 *  one spelling. */
export function ownershipTarget(note: OwnershipNote): {
  lens: string | null;
  tabLabel: string | null;
} {
  const lens =
    note.lens && sectionOwnsLens(note.workspace, note.section, note.lens) ? note.lens : null;
  const declared =
    note.objectTab
      ? findObjectContract(note.workspace, note.section, note.objectTab.type)?.tabs?.includes(
          note.objectTab.tab,
        )
      : false;
  const slug = note.objectTab?.tab ?? "";
  return {
    lens,
    tabLabel: declared && slug ? slug[0].toUpperCase() + slug.slice(1) : null,
  };
}

export const SECTION_OWNERSHIP: Record<string, readonly OwnershipNote[]> = {
  "master-data": [
    {
      label: "Knowledge, Skills and AI Paths",
      workspace: "context-hub",
      section: "knowledge-library",
      detail: "Context Hub authors business knowledge against these object IDs. Governance defines the objects; it does not edit the knowledge.",
    },
  ],
  "semantic-model": [
    {
      label: "Physical field mapping",
      workspace: "data",
      section: "datastreams",
      detail: "Data owns profiling, field proposals and the published mapping version of each Datastream. Governance reads those versions.",
    },
    // STORY 60.4 — the FOURTH transformation family, named where the other
    // three are listed. Value Tables, Cleanup Rules and the calculated fields
    // of Concepts are three lenses of this screen; excluding, joining and
    // splitting a collected column is the fourth, it is delivered by story 60.6
    // and it is NOT a Governance object at all. Naming it here is what stops a
    // person from searching this screen for something that was never going to
    // be on it; saying where it is edited is what stops Governance from
    // becoming its second writer (`datastream-workbench-and-wizard.md:987`).
    {
      label: "Excluded, joined and split columns",
      workspace: "data",
      section: "datastreams",
      objectTab: { type: "datastream", tab: "mapping" },
      detail:
        "Which collected columns are excluded, joined into one Concept or split into several is decided per Datastream and is not edited in Governance. Governance reads the published mapping version that results.",
    },
  ],
  "controls-quality": [
    {
      label: "Source health and Runs",
      workspace: "data",
      section: "sources",
      detail: "Operational health and individual Runs stay in Data. Governance owns the consolidated policy, case and decision.",
    },
  ],
  evidence: [
    {
      label: "Evaluation runs and annotations",
      workspace: "test",
      section: "regression-runs",
      detail: "Test owns evaluation suites and their results. Evidence links them by identifier and copies none of them.",
    },
    {
      label: "Render snapshots",
      workspace: "analyze",
      section: "renders",
      detail: "Analyze owns rendered outputs. Evidence references them; it is not a second ledger.",
    },
  ],
};
