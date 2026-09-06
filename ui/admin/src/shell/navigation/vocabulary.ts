/**
 * The vocabulary every workspace declares itself in -- AD-42, 2026-08-12.
 *
 * `navigation.ts` held ONE `WORKSPACES` declaration of 313 lines that six
 * workspaces shared, `governance` taking 125 of them alone. Every new section,
 * lens or object contract -- whoever owned it -- was appended to that one array:
 * 83 edits from 27 distinct subjects since June.
 *
 * The table is now declared one workspace per file, and this holds what they all
 * speak: the types, the `section()` constructor and the query contracts. It
 * imports nothing from them, so the graph stays acyclic.
 */

export type WorkspaceKey =
  | "overview"
  | "analyze"
  | "test"
  | "data"
  | "governance"
  | "context-hub";

export interface ObjectRouteContract {
  type: string;
  /** The noun a PERSON reads for this type — breadcrumb, screen title, the type
   *  column of a collection. `type` above stays the wire token: the route slug,
   *  the envelope's `object_type` and every stored reference.
   *
   *  Declared here and nowhere else, because the console had two spellings of
   *  the same answer — `governance/contracts.ts#OBJECT_TYPE_LABEL` for the
   *  workbench and `TopBar.tsx#sentenceCase` for the crumb — and a type absent
   *  from the first was printed raw by one and sentence-cased by the other. That
   *  is how a person read `tracked-entity` on a screen whose lens is called
   *  Competitor Registry (`governance.md`, *Amendment, 2026-09-01*).
   *
   *  A DISPLAYED rename is exactly this field and nothing else. Renaming the
   *  token is a migration with a backfill, and it stays deferred. */
  label?: string;
  tabs?: readonly string[];
  /** The tab a bare `/object/:type/:id` address canonicalizes to.
   *  Declared, never inferred: `tabs[0]` looks like a default but asserting it
   *  for a contract whose story never chose one invents a design decision. A
   *  contract without this field keeps a bare object route bare. */
  defaultTab?: string;
  actions?: readonly string[];
  /** Tabs that may carry `/version/{id}`. Absent keeps the historical rule:
   *  `versions` is the only version-bearing tab. */
  versionTabs?: readonly string[];
  /** Tabs that may carry `/evidence/{id}` -- an exact datum inside one lens. */
  evidenceTabs?: readonly string[];
}

export interface LensContract {
  slug: string;
  label: string;
}

export interface SubnavItem {
  slug: string;
  label: string;
  objects: readonly ObjectRouteContract[];
  /** Contextual actions owned by the COLLECTION rather than by one object.
   *  "Add Datastream" is the case that forced this: it operates on the section,
   *  and encoding it as an action on an object id of `"new"` asserted that an
   *  object exists when none does. */
  actions?: readonly string[];
  /** URL-stable collection lenses. The FIRST entry is the declared default: a
   *  base collection route canonicalizes to it, and an unknown lens stays
   *  Unknown rather than falling back to it. A lens is not an action and never
   *  lives in component state — a two-button local toggle is not shareable. */
  lenses?: readonly LensContract[];
  /** URL-stable query state a section OWNS (Story 49.3). Declared here and
   *  nowhere else, so a section cannot start honouring a parameter the router
   *  does not know how to canonicalize — and a parameter nobody declared is
   *  dropped rather than carried into a shareable address that lies. */
  query?: QueryContract;
}

export interface QueryParameter {
  name: string;
  /** Parameters that are meaningless alone. `semantic_view_id` without its
   *  version pins nothing, so the pair is required together or neither is
   *  accepted. */
  requiredWith?: readonly string[];
  /** Values that parse but must never be honoured. `latest` is the whole point:
   *  a shared link that follows `latest` silently changes what it shows. */
  forbiddenValues?: readonly string[];
}

export interface QueryContract {
  parameters: readonly QueryParameter[];
}

export interface Workspace {
  key: WorkspaceKey;
  slug: WorkspaceKey;
  label: string;
  question: string;
  subnav: readonly SubnavItem[];
}

export const section = (
  slug: string,
  label: string,
  objects: readonly ObjectRouteContract[] = [],
  actions: readonly string[] = [],
  lenses: readonly LensContract[] = [],
  query?: QueryContract,
): SubnavItem => ({ slug, label, objects, actions, lenses, query });

/** The exact published Semantic View an Explore address pins (Story 49.3 AC12).
 *  Both parameters are required together, `latest` is invalid on either, and
 *  the Business Domain is an optional narrowing that means nothing on its own. */
export const EXPLORE_QUERY: QueryContract = {
  parameters: [
    {
      name: "semantic_view_id",
      requiredWith: ["semantic_view_version_id"],
      forbiddenValues: ["latest", "current"],
    },
    {
      name: "semantic_view_version_id",
      requiredWith: ["semantic_view_id"],
      forbiddenValues: ["latest", "current"],
    },
    { name: "business_domain_id", requiredWith: ["semantic_view_id"] },
    // Story 50.4: the Result a Visualization Builder session is bound to. An
    // address that said `latest` would silently change which answer the
    // presentation describes.
    { name: "result_id", forbiddenValues: ["latest", "current"] },
    {
      name: "left_datastream_id",
      requiredWith: ["right_datastream_id", "common_key_version_id"],
    },
    {
      name: "right_datastream_id",
      requiredWith: ["left_datastream_id", "common_key_version_id"],
    },
    {
      name: "common_key_version_id",
      requiredWith: ["left_datastream_id", "right_datastream_id"],
      forbiddenValues: ["latest", "current"],
    },
    // THE PINS `Explore together` HANDS OVER (data.md, *Which Datastreams can
    // usefully be crossed*): "the exact Datastream and Output versions that were
    // on the screen, not their current heads". The server composes them; until
    // they were declared here the address dropped them as undeclared, and the
    // Explorer re-selected the pair from a catalog it fetched itself -- the
    // re-resolution that amendment forbids by name.
    //
    // ALL SIX TRAVEL TOGETHER, WITH THE PAIR. A mapping version pinned without
    // its Output version pins half a reading, and either one without the pair it
    // belongs to pins nothing at all.
    ...(
      [
        "left_mapping_version_id",
        "left_output_version_id",
        "left_published_execution_id",
        "right_mapping_version_id",
        "right_output_version_id",
        "right_published_execution_id",
      ] as const
    ).map((name, _index, all) => ({
      name,
      requiredWith: [
        ...all.filter((companion) => companion !== name),
        "left_datastream_id",
        "right_datastream_id",
        "common_key_version_id",
      ],
      forbiddenValues: ["latest", "current"],
    })),
  ],
};

/** The bounded Evidence filter and cursor state (Story 49.5 AC9).
 *
 *  Declared here so a filtered Evidence view is a real ADDRESS: the router
 *  validates it, `buildPath` reproduces it, and someone who is sent the link
 *  opens what the sender was looking at. The previous Evidence screen kept its
 *  state in the component, which is why it could not be shared at all.
 *
 *  The SERVER applies every one of these. They are declared here to be carried
 *  and validated, never to be honoured by the browser: a page filtered in the
 *  client would show a count the server never computed.
 *
 *  `cursor` is meaningless without the filter set it was minted against, so the
 *  server refuses a cursor whose filter fingerprint does not match. It is
 *  declared alone here because it is legitimate on its own — page two of an
 *  unfiltered lens. */
export const EVIDENCE_QUERY: QueryContract = {
  parameters: [
    // The instant search of the collection shell, and the ONE parameter here
    // the server never sees: `normalize_filters` (`evidence_index.py:1823-1831`)
    // refuses every key outside `ALLOWED_FILTERS`, so sending it would turn a
    // typed word into a 400. It is applied in the browser over the page the
    // server already returned, and declared here for the same reason `scope` is
    // declared below — a narrowing kept out of this registry is a narrowing
    // `buildPath` refuses to build, which is what made pressing Enter in the
    // search box throw "This section declares no query state".
    { name: "q" },
    { name: "cursor" },
    { name: "limit" },
    { name: "from" },
    { name: "to" },
    { name: "owner_workspace" },
    { name: "record_kind" },
    { name: "correlation_kind", requiredWith: ["correlation_id"] },
    { name: "correlation_id" },
    { name: "outcome" },
  ],
};

/** The scope narrowing of a Semantic Model lens (Story 60.4).
 *
 *  IT IS APPLIED IN THE BROWSER, AND DECLARED HERE ANYWAY. The router is the
 *  only place a parameter may be declared (`sectionQueryContract`), and a
 *  parameter nobody declares is dropped on parse and refused by `buildPath` —
 *  so a filter kept out of this registry is a filter kept out of the address,
 *  which is the defect `GovernanceCollection.tsx:4-7` was written against: "A
 *  lens in `useState` is not a view; it is a mood."
 *
 *  The SERVER is not widened by this line and must not be:
 *  `_COLLECTION_QUERY_KEYS` (`governance_surface_api.py:85-95`) knows nine
 *  Evidence keys, `governance_read_model.py:2274-2275` raises for any parameter
 *  outside Evidence, and the collection request carries filters on Evidence
 *  only. `scope` never reaches a query string this browser sends.
 *
 *  No lens, no object type and no route is added with it: this section still
 *  declares five lenses and four object types, which
 *  `Router.test.tsx:157-234` asserts by exact list. */
export const SEMANTIC_MODEL_QUERY: QueryContract = {
  parameters: [{ name: "q" }, { name: "scope" }],
};

/** The instant search of a Controls & Quality lens.
 *
 *  Same shape and same reason as `q` above, and no `scope`: this section draws
 *  no scope selector (`GovernanceCollection.tsx`, `SCOPE_FILTER_SECTION`), and a
 *  parameter declared here that no control writes and no reader honours would be
 *  carried into a shared address that promises a narrowing nothing applies.
 *
 *  The SERVER is not widened: `governance_read_model.py:2940-2943` raises
 *  "accepts no query parameters" for every section outside Evidence and Master
 *  Data, and this browser sends none of it — `filters` is `{}` here. */
export const CONTROLS_QUALITY_QUERY: QueryContract = {
  parameters: [{ name: "q" }],
};

/** The search, scope and page of a Master Data lens.
 *
 *  MASTER DATA IS THE ONE SECTION THE SERVER PAGES, so these four are the only
 *  ones that reach it and the list is exact:
 *  `governance_read_model.py:2889` allows `{cursor, limit, q, scope}` and raises
 *  "unsupported Master Data filter" for anything else.
 *
 *  It was the only Governance section declaring NO contract at all while its
 *  screen drew the whole apparatus — search box, Search button, scope selector,
 *  First page and Next page. Every one of those five controls called
 *  `buildPath`, and every one threw "This section declares no query state"
 *  (`router.tsx:246`): the screen could be read and could not be filtered. */
export const MASTER_DATA_QUERY: QueryContract = {
  parameters: [{ name: "q" }, { name: "scope" }, { name: "cursor" }, { name: "limit" }],
};

/** The five tabs every Master Data governed object carries (governance.md). */
export const MASTER_DATA_TABS = ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"] as const;

/** Ordered and exact: consumers must render this registry, never a copied list. */
