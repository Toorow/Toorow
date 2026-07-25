/**
 * Navigation model — Epic 42 story 42.1 (IA v3 shell).
 *
 * Single data-driven source for the six project workspaces and their subnav,
 * consumed by BOTH the StableSidebar and the deep-linkable router. Matches the
 * validated v4 render (key-project-overview-v4.png) and navigation-architecture-v3.
 *
 * Design doctrine baked in:
 *  - The permanent left nav carries exactly the SIX project workspaces. No flat
 *    20-item list, no duplicate horizontal tab bar.
 *  - Object-detail tabs (Datastream, Report, Notebook, …) are NOT here — they
 *    appear only after opening an object and live with the object shell.
 *  - Recovery is an exception drawer, never a workspace.
 *  - Organization administration and the user account are OUT of project nav
 *    (they live in the TopBar scope control).
 *  - All labels English (2026-07-24 directive). No hardcoded colors here; the
 *    sidebar consumes theme tokens.
 */

export type WorkspaceKey =
  | "overview"
  | "analyze"
  | "test"
  | "data"
  | "governance"
  | "context";

export interface SubnavItem {
  /** URL segment under the workspace, e.g. /p/:project/analyze/reports */
  slug: string;
  label: string;
}

export interface Workspace {
  key: WorkspaceKey;
  /** URL segment, e.g. /p/:project/data */
  slug: string;
  label: string;
  /** One-line purpose (the user question the workspace answers). */
  question: string;
  /** Ordered subnav; empty for leaf workspaces (Overview). */
  subnav: SubnavItem[];
}

/**
 * The six workspaces, in render order. `Data` intentionally has no flat subnav
 * of Datastreams: its navigation expands as `Sources / Datastreams > type >
 * Datastream / Imports` via the DatastreamTree, handled by the Data workspace
 * itself. The subnav entries below are the stable section anchors.
 */
export const WORKSPACES: Workspace[] = [
  {
    key: "overview",
    slug: "overview",
    label: "Overview",
    question: "What changed and what needs my attention?",
    subnav: [],
  },
  {
    key: "analyze",
    slug: "analyze",
    label: "Analyze",
    question: "What do the matched data say?",
    subnav: [
      { slug: "reports", label: "Reports" },
      { slug: "widgets", label: "Widgets" },
      { slug: "notebooks", label: "Notebooks" },
      { slug: "renders", label: "Renders" },
    ],
  },
  {
    key: "test",
    slug: "test",
    label: "Test",
    question: "Can I trust the generated answer and widget?",
    subnav: [
      { slug: "golden-questions", label: "Golden questions" },
      { slug: "regression-runs", label: "Regression runs" },
      { slug: "widget-feedback", label: "Widget feedback" },
    ],
  },
  {
    key: "data",
    slug: "data",
    label: "Data",
    question: "What data enters this project and is it current?",
    subnav: [
      { slug: "data-overview", label: "Data overview" },
      { slug: "datastreams", label: "Datastreams" },
      { slug: "sources", label: "Sources" },
      { slug: "imports", label: "Imports" },
      { slug: "modules", label: "Modules" },
    ],
  },
  {
    key: "governance",
    slug: "governance",
    label: "Governance",
    question: "How are the data defined, matched and verified?",
    subnav: [
      { slug: "semantic-model", label: "Semantic model" },
      { slug: "mapping", label: "Mapping" },
      { slug: "competitors", label: "Competitors" },
      { slug: "data-quality", label: "Data quality" },
      { slug: "provenance", label: "Provenance" },
      { slug: "activity", label: "Activity" },
    ],
  },
  {
    key: "context",
    slug: "context",
    label: "Context",
    question: "What business knowledge should analysis use?",
    subnav: [
      { slug: "knowledge", label: "Knowledge" },
      { slug: "procedures", label: "Procedures" },
      { slug: "events", label: "Events" },
    ],
  },
];

export const WORKSPACE_BY_KEY: Record<WorkspaceKey, Workspace> = Object.fromEntries(
  WORKSPACES.map((w) => [w.key, w]),
) as Record<WorkspaceKey, Workspace>;

/** Default landing workspace. */
export const DEFAULT_WORKSPACE: WorkspaceKey = "overview";
