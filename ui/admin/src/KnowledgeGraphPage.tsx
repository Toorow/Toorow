/**
 * KnowledgeGraphPage — Story 44.4, "the mindmap".
 *
 * A spatial, read-only projection of everything the agent knows about this
 * project: governed topics, procedures and generated schema docs (nodes) plus
 * the `app.context_graph` links between them (edges). Mounted at
 * Context ▸ Graph by shell/ContentRouter.tsx; the shell owns the frame,
 * sidebar, topbar and <main>, so this component renders only page content.
 *
 * ── Scope (v1 + 44.5 + 44.6) ──────────────────────────────────────────────
 * READ + drawer (44.4). Story 44.5 adds edge creation/deletion and a topic
 * editor (create from the canvas or the empty state, edit from the drawer) —
 * that editor is side-by-side title + Markdown + live preview, the same shape
 * as Story 44.1's editor on Context ▸ Knowledge (see `RawMarkdown`). Story
 * 44.6 adds the view switcher (Domains/Procedures/Schema/Orphans — see
 * `projectGraph`), the MiniMap, the Tidy button and Up/Down search-hit
 * cycling; all four views are pure client-side re-shapes of the SAME fetched
 * payload — switching never refetches. Nodes are draggable purely for reading
 * comfort: positions are NEVER persisted (R44-UX02 — auto-layout is the
 * source of truth, the operator never hand-positions a corpus). Nothing is
 * deletable locally: React Flow's own Delete/Backspace shortcut is disabled
 * (`deleteKeyCode={null}`) so the canvas can never diverge from the server.
 *
 * ── Data ──────────────────────────────────────────────────────────────────
 * ONE call per load: `GET /api/context/graph?project_id=<id>` (Story 44.3,
 * server/core/context_api.py::_get_graph) through `apiFetch`. Filters, search
 * and re-layout are pure client work on that single payload — changing a filter
 * must never refetch. The endpoint already drops dangling edges and returns
 * verbatim 280-char excerpts, so the UI truncates visually (3-line clamp) but
 * never paraphrases (R44-UX03).
 *
 * Opening the drawer needs the FULL body, which the bundle deliberately does
 * not carry, so the drawer fetches `GET /api/context/topics/{id}` or
 * `GET /api/context/procedures/{id}` on open. There is no single-doc endpoint
 * for schema docs (checked against CONTEXT_ROUTES: only topics/procedures have
 * a `/{id}` GET), so a schema_doc drawer shows the verbatim excerpt and says so
 * — it does not pretend to hold the whole document.
 *
 * ── Layout ────────────────────────────────────────────────────────────────
 * elkjs `layered`, direction RIGHT, recomputed on load and on every structural
 * filter change. We import the BUNDLED build (`elkjs/lib/elk.bundled.js`), not
 * the worker build: it runs in-process, which is what lets vitest/jsdom execute
 * the real layout instead of a stub.
 *
 * ── Styling ───────────────────────────────────────────────────────────────
 * shell/application.css for the shell classes + knowledge-graph.css for this
 * page. Every colour is an application.css CSS variable (dark-safe): topic =
 * `--rose`, procedure = `--info` (the violet-blue accent — application.css has
 * no other blue token and inventing a hex is forbidden), schema_doc = neutral
 * `--muted`, target_field = `--success` (the only remaining accent token that
 * is not already taken by another node type; `--warning` is spoken for by the
 * "auto" tag). React Flow ships hardcoded light colours in its base
 * stylesheet, so knowledge-graph.css re-points its `--xy-*` variables at our
 * tokens.
 *
 * ── Story 44.10 ───────────────────────────────────────────────────────────
 * Dictionary fields (`app.target_fields`) are the FOURTH node type. They are
 * read-only here: the drawer summarises the field, links out to the Semantic
 * model page (`onOpenSemanticModel`, wired the same way as `onOpenProcedure`)
 * and lists what actually FEEDS the field via
 * `GET /api/datamodel/mappings?target_field=…`. Editing a field stays on the
 * Semantic model page — this page never writes to the dictionary.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
  type ReactFlowInstance,
} from "@xyflow/react";
import ELK from "elkjs/lib/elk.bundled.js";
import "@xyflow/react/dist/style.css";
import "./shell/application.css";
import "./knowledge-graph.css";
import { formatDate } from "./ui/Timestamp";
import { apiFetch } from "./lib/apiFetch";
import NodeRemarks from "./connaissances/NodeRemarks";
import { parseSkillSteps } from "./connaissances/SkillStepList";
import { endpointLabelOf, graphWord } from "./connaissances/relationshipVocabulary";
import RawMarkdown from "./lib/RawMarkdown";
import type { OwnerReference } from "./governance/governanceSurface";
import { EmptyState, ObjectId, stateLabel } from "./ui";

// ---------------------------------------------------------------------------
// Payload contract — GET /api/context/graph (Story 44.3)
// ---------------------------------------------------------------------------

/**
 * The node types the governed bundle can contain.
 *
 * Story 45.1 added `business_domain`, `business_classification` and
 * `report_view` server-side. They arrived in the payload and rendered as cards
 * with no kind badge and no filter toggle, because this union stopped at four —
 * so the graph could not be "rooted in Business Domains" (context-hub.md).
 */
/**
 * The GRAPH NODE vocabulary. It overlaps heavily with `BusinessTargetType` and is
 * deliberately NOT the same list: it carries `business_domain` and
 * `business_classification`, which are the taxonomy SIDE of a link and never the
 * target of one. Two vocabularies that share most of their values are still two.
 *
 * Story 49.6 adds the three the mindmap could not draw. `context-hub.md` says the
 * graph "expands through classification layers to products, activities, metrics,
 * views, Datastreams, knowledge and Skills" -- Datastreams and views were absent,
 * so a business link to one produced an edge whose endpoint had no node, and the
 * bundle dropped it as dangling.
 */
export type NodeTypeKey =
  | "topic"
  | "procedure"
  | "schema_doc"
  | "target_field"
  | "canonical_field"
  | "business_domain"
  | "business_classification"
  | "report_view"
  | "datastream"
  | "semantic_view"
  | "semantic_concept";

/** The three types Context Hub owns: read-only on the canvas, edited in its own
 *  surface. Their edges are derived from the authoritative taxonomy. */
export const BUSINESS_NODE_TYPES: ReadonlySet<NodeTypeKey> = new Set([
  "business_domain",
  "business_classification",
  "report_view",
]);
export type ScopeKey = "platform" | "project";

export interface GraphNodeRow {
  id: string;
  node_type: NodeTypeKey;
  title: string;
  /** Verbatim first 280 chars of body_md, computed server-side (R44-UX03). */
  excerpt: string;
  /** Resolved for display (Story 44.11): explicit owner, else created_by, else "auto". */
  owner: string | null;
  /**
   * The RAW explicit `owner` column value (string or null) -- topic/procedure
   * nodes only. Editors (the drawer edit form, the Knowledge/Procedures page
   * editors) read/write this field, never the resolved `owner` above, so an
   * unset owner does not get baked in as a fake explicit value on save.
   */
  owner_raw?: string | null;
  version_number: number;
  scope: ScopeKey;
  status: string;
  /** schema_doc nodes only ("columns", "sample_values", …); absent elsewhere. */
  doc_kind?: string | null;
  /**
   * target_field nodes only (Story 44.10): "metric" | "dimension". Absent on
   * every other node type — a missing value renders no badge rather than a
   * guessed one.
   */
  field_kind?: string | null;
}

export interface GraphEdgeRow {
  id: string;
  from_id: string;
  to_id: string;
  from_type: string;
  to_type: string;
  edge_type: string;
  project_id?: string | null;
  created_by: string;
  created_at: string;
}

export interface GraphBundle {
  nodes: GraphNodeRow[];
  edges: GraphEdgeRow[];
}

// `procedure` is the DELIVERED TOKEN; the product noun is `Skill`
// (`glossary.md`: "Procedure *(not a product noun)* — A Skill"). The key stays
// what the wire and `mdm_business_links.target_type` carry until the rename of
// `alignment-register.md` item 4 lands with its backfill; only the word a
// person reads changes here.
// The six types this canvas SHARES with the relationship authority come from
// the one vocabulary (`connaissances/relationshipVocabulary.ts`), which is a
// leaf module precisely so a panel can read these words without pulling React
// Flow behind them — the reason the Usage facet had a private copy of three of
// them, and rendered every other type as a wire token.

export const NODE_TYPE_ORDER: NodeTypeKey[] = [
  "business_domain",
  "business_classification",
  "topic",
  "procedure",
  "schema_doc",
  "target_field",
  "canonical_field",
  "report_view",
  // After the report view: these are governed objects the graph REACHES, not
  // business keys it is rooted in. The order is the reading order of the legend.
  "semantic_view",
  "semantic_concept",
  "datastream",
];

/** The five node types the mindmap draws beyond a relationship endpoint.
 *
 *  Three of them ARE object types of the navigation registry, so their word
 *  comes from `endpointLabelOf` like every other — `Canonical Field`,
 *  `Business Domain`, `Semantic Concept`, in the registry's spelling. This map
 *  holds only the two the registry cannot answer for, because no route opens
 *  them. Writing all five here is what let `Semantic concept` sit one screen
 *  away from the registry's `Semantic Concept` (`console-presentation.md` §4). */
const MINDMAP_ONLY_LABELS: Record<string, string> = {
  business_classification: "Business layer",
  report_view: "Report view",
};

export const NODE_TYPE_LABELS: Record<NodeTypeKey, string> = Object.fromEntries(
  NODE_TYPE_ORDER.map((key) => [
    key,
    endpointLabelOf(key) ?? MINDMAP_ONLY_LABELS[key] ?? key,
  ]),
) as Record<NodeTypeKey, string>;

/** Above this many nodes the page warns and suggests filters — but still renders (R44-UX04). */
export const LARGE_GRAPH_THRESHOLD = 500;

/** Card geometry handed to elk; must match the CSS box in knowledge-graph.css. */
export const NODE_WIDTH = 268;
export const NODE_HEIGHT = 156;

// ---------------------------------------------------------------------------
// Pure filter / search logic (exported so it is testable without a canvas)
// ---------------------------------------------------------------------------

export interface GraphFilterState {
  /** Node-type toggles; a type switched off hides its nodes and their edges. */
  types: Record<NodeTypeKey, boolean>;
  /** "all" or one concrete edge_type present in the payload. */
  edgeType: string;
  scope: "all" | ScopeKey;
}

export const DEFAULT_FILTERS: GraphFilterState = {
  types: {
    topic: true,
    procedure: true,
    schema_doc: true,
    target_field: true,
    canonical_field: true,
    business_domain: true,
    business_classification: true,
    report_view: true,
    // Visible by default like every other type. Hiding the three newest behind
    // an off switch would reproduce the bug they fix: the link exists and the
    // mindmap does not show it.
    datastream: true,
    semantic_view: true,
    semantic_concept: true,
  },
  edgeType: "all",
  scope: "all",
};

/**
 * Structural filtering. Search is deliberately NOT part of it: a search
 * highlights and centres matches (R44-FR03) instead of amputating the graph,
 * because hiding a node's neighbours would misrepresent what the agent knows.
 */
export function applyGraphFilters(
  nodes: GraphNodeRow[],
  edges: GraphEdgeRow[],
  filters: GraphFilterState,
): GraphBundle {
  const keptNodes = nodes.filter(
    (n) =>
      filters.types[n.node_type] !== false &&
      (filters.scope === "all" || n.scope === filters.scope),
  );
  const keptIds = new Set(keptNodes.map((n) => n.id));
  const keptEdges = edges.filter(
    (e) =>
      keptIds.has(e.from_id) &&
      keptIds.has(e.to_id) &&
      (filters.edgeType === "all" || e.edge_type === filters.edgeType),
  );
  return { nodes: keptNodes, edges: keptEdges };
}

// ---------------------------------------------------------------------------
// Story 45.3 — business scope
//
// The Context Hub selection scopes the canvas WITHOUT a refetch: everything
// below reshapes the bundle already in memory, exactly like the 44.6 view
// projections. Selecting a domain or a layer keeps its ancestor path, its whole
// subtree, and the governed resources linked to any node of that subtree.
// Clearing the selection ("All business") restores the complete bundle.
// ---------------------------------------------------------------------------

export interface BusinessScopeInput {
  selected: { type: "business_domain" | "business_classification"; id: string } | null;
  domains: Array<{ id: string }>;
  classifications: Array<{ id: string; domain_id: string; parent_id: string | null }>;
  links: Array<{ taxonomy_type: string; taxonomy_id: string; target_id: string }>;
  onSelect?: (
    selection: { type: "business_domain" | "business_classification"; id: string } | null,
  ) => void;
}

/**
 * The taxonomy ids in scope: the selected node, its ancestors up to its domain,
 * and its descendants. Cycles and missing parents are bounded by a visited set —
 * the authoritative store rejects cycles, and a projection must not hang if one
 * ever reaches it.
 */
export function businessScopeIds(scope: BusinessScopeInput): Set<string> | null {
  const selected = scope.selected;
  if (!selected) return null;
  const byId = new Map(scope.classifications.map((item) => [item.id, item]));

  // Descendants are seeded from the SELECTED node only. Seeding them from the
  // ancestors too would pull a selected layer's siblings back in through their
  // shared domain, which is the opposite of scoping.
  const descendants = new Set<string>([selected.id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const item of scope.classifications) {
      if (descendants.has(item.id)) continue;
      const parentInScope =
        item.parent_id !== null
          ? descendants.has(item.parent_id)
          : descendants.has(item.domain_id);
      if (parentInScope) {
        descendants.add(item.id);
        grew = true;
      }
    }
  }

  // Ancestors: the traceable path up to the domain. Bounded by a visited set --
  // the authoritative store rejects cycles, and a projection must not hang if
  // one ever reaches it.
  const inScope = new Set<string>(descendants);
  if (selected.type === "business_classification") {
    let cursor = byId.get(selected.id);
    const seen = new Set<string>();
    while (cursor && !seen.has(cursor.id)) {
      seen.add(cursor.id);
      inScope.add(cursor.id);
      inScope.add(cursor.domain_id);
      cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined;
    }
  }
  return inScope;
}

/**
 * Keep the taxonomy path/subtree in scope plus every governed resource linked to
 * it. A resource linked to nothing in scope drops out; an empty scope yields an
 * empty graph rather than unrelated nodes, which is what the story requires the
 * empty state to say.
 */
export function applyBusinessScope(
  nodes: GraphNodeRow[],
  edges: GraphEdgeRow[],
  scope: BusinessScopeInput,
): GraphBundle {
  const inScope = businessScopeIds(scope);
  if (inScope === null) return { nodes, edges };
  const linkedTargets = new Set(
    scope.links.filter((link) => inScope.has(link.taxonomy_id)).map((link) => link.target_id),
  );
  const keptNodes = nodes.filter((node) => inScope.has(node.id) || linkedTargets.has(node.id));
  const keptIds = new Set(keptNodes.map((node) => node.id));
  const keptEdges = edges.filter((edge) => keptIds.has(edge.from_id) && keptIds.has(edge.to_id));
  return { nodes: keptNodes, edges: keptEdges };
}

/** Case-insensitive match on the two fields the operator can actually see. */
export function nodeMatchesQuery(node: GraphNodeRow, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return false;
  return (
    node.title.toLowerCase().includes(q) || (node.excerpt ?? "").toLowerCase().includes(q)
  );
}

/** The distinct edge_types present in a payload, for the filter select. */
export function edgeTypeOptions(edges: GraphEdgeRow[]): string[] {
  return Array.from(new Set(edges.map((e) => e.edge_type))).sort();
}

/** The drift aggregate the picker line renders (`/context/ai-paths/stats`). */
export interface AiPathStats {
  window_days: number;
  outcomes: Record<string, number>;
  verdicts: Record<string, number>;
  verdict_window: number;
  verdicts_assessed: number;
}

/** One honest line: outcomes over the stated window, verdicts over their
 * stated sample -- never an all-time rate read off a bounded one. */
export function formatAiPathStats(stats: AiPathStats): string {
  const outcomes =
    ["succeeded", "failed", "refused", "unavailable"]
      .filter((k) => stats.outcomes[k])
      .map((k) => `${stats.outcomes[k]} ${k}`)
      .join(" · ") || "no finalized walk";
  const verdicts =
    ["pass", "fail", "unverifiable"]
      .filter((k) => stats.verdicts[k])
      .map((k) => `${stats.verdicts[k]} ${k}`)
      .join(" · ") || "none";
  return (
    `Last ${stats.window_days} days: ${outcomes} — verdicts over the ` +
    `${stats.verdicts_assessed} most recent: ${verdicts}`
  );
}

/** The evidence-inspection aggregate (`/context/ai-paths/inspections/summary`).
 *
 * `inspections_total` is `null` — never 0 — when the server's bounded scan hit
 * its cap: the size of the window is then something the query did not prove, and
 * a ceiling shown as a total is a number that reads as complete. */
export interface AiPathInspectionSummary {
  window_days: number;
  inspections_scanned: number;
  inspections_total: number | null;
  window_truncated: boolean;
  displayed_states: Record<string, number>;
  top_steps: Array<{ ai_path_id: string; step_ordinal: number; inspections: number }>;
}

/** One honest line about who opened what, in the words a reader speaks.
 *
 * THE THREE EMPTINESSES STAY THREE, because they are three different facts and
 * the person reading this line acts differently on each:
 *   - nobody opened anything → the line names the gesture that fills it;
 *   - openings happened and the walk had judged no branch → an honest zero;
 *   - openings happened and what was judged is unknowable — not kept at the time,
 *     or unreadable now. Folding that into "none" would say "nothing was
 *     considered" about something nobody can know, which is the confusion the
 *     four recorded states exist to prevent. */
export function formatInspectionSummary(summary: AiPathInspectionSummary): string {
  const states = summary.displayed_states ?? {};
  const listed = states.branches_listed ?? 0;
  const judgedNone = states.no_branch_judged ?? 0;
  const notKept = states.branches_not_recorded ?? 0;
  const unreadable = states.unavailable ?? 0;
  const opened = summary.inspections_scanned ?? 0;

  if (opened === 0) {
    // Names the gesture, never a deployment state: this is what fills it.
    return "No evidence branch opened yet — open an AI Path and expand a step to start counting.";
  }

  // A truncated window knows how many it READ, not how many there were.
  const total = summary.inspections_total === null ? `at least ${opened}` : `${opened}`;
  const parts = [
    listed ? `${listed} showed branches` : "",
    judgedNone ? `${judgedNone} had none to show` : "",
    notKept ? `${notKept} not kept` : "",
    unreadable ? `${unreadable} could not be read` : "",
  ].filter(Boolean);
  const top = summary.top_steps?.[0];
  const most = top
    ? ` · most opened: step ${top.step_ordinal} of ${top.ai_path_id} (${top.inspections}×)`
    : "";
  return (
    `Last ${summary.window_days} days: ${total} branch openings — ` +
    // A recorded state this build does not know is reported as unknown rather
    // than dropped: a bucket silently omitted would make the parts sum to less
    // than the total with nothing saying why.
    `${parts.length ? parts.join(" · ") : "in states this screen cannot name"}${most}`
  );
}

// ---------------------------------------------------------------------------
// elk layout
// ---------------------------------------------------------------------------

/**
 * Minimal structural typing over the elk instance. elkjs' own generic
 * `layout<T extends ElkNode>` signature indexes `T['children'][number]`, which
 * does not instantiate cleanly under `strict`; we only need id/x/y back.
 */
interface ElkLayoutResult {
  children?: Array<{ id: string; x?: number; y?: number }>;
}
interface ElkLike {
  layout(graph: unknown): Promise<ElkLayoutResult>;
}

/**
 * The ONE elk instance the page uses. 44.4 shipped a `toElkGraph`/`layoutGraph`
 * pair for the single (Domains) layout; 44.6 generalised both into
 * `toElkGraphForProjection`/`layoutProjection`, of which Domains is now just
 * one case, so the 44.4 pair was deleted rather than left as a second,
 * unreachable code path that only tests could still reach.
 */
const elk = new ELK() as unknown as ElkLike;

// ---------------------------------------------------------------------------
// React Flow edge mapping
// ---------------------------------------------------------------------------

/**
 * The exact edge objects handed to <ReactFlow>. Extracted as a pure function so
 * the contract (notably `label: edge_type` — React Flow renders edge labels into
 * SVG that jsdom cannot introspect, so nothing else in a DOM test would notice
 * it disappearing) can be asserted directly.
 */
export function toFlowEdges(edges: GraphEdgeRow[]): Edge[] {
  return edges.map((e) => ({
    id: e.id,
    source: e.from_id,
    target: e.to_id,
    label: e.edge_type,
    type: "smoothstep",
    labelShowBg: false,
    markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
  }));
}

// ---------------------------------------------------------------------------
// Graph editing — Story 44.5. Pure request/response shapes kept out of the
// component so the exact POST body and the modal's derived open-state can be
// asserted directly, without driving a React Flow drag gesture in jsdom.
// ---------------------------------------------------------------------------

/** Suggested edge_type buttons in the connect modal; the API accepts any non-empty string. */
export const EDGE_TYPE_SUGGESTIONS = ["defines", "depends_on", "explains", "relates_to"] as const;

export interface CreateEdgePayload {
  project_id: string;
  from_id: string;
  from_type: NodeTypeKey;
  to_id: string;
  to_type: NodeTypeKey;
  edge_type: string;
}

/** The exact body POSTed to /api/context/graph/edges (Story 44.5, AC1). */
export function buildEdgeCreatePayload(
  projectId: string,
  fromNode: GraphNodeRow,
  toNode: GraphNodeRow,
  edgeType: string,
): CreateEdgePayload {
  return {
    project_id: projectId,
    from_id: fromNode.id,
    from_type: fromNode.node_type,
    to_id: toNode.id,
    to_type: toNode.node_type,
    edge_type: edgeType.trim(),
  };
}

/** The two fields of a React Flow `Connection` this page actually needs. */
export interface ConnectionEndpoints {
  source: string | null;
  target: string | null;
}

/**
 * Resolves a React Flow `onConnect` gesture against the known node set. Returns
 * null for a connection that cannot be resolved (dangling id) rather than ever
 * opening a modal for a phantom edge.
 */
export function resolveConnection(
  connection: ConnectionEndpoints,
  nodeById: Map<string, GraphNodeRow>,
): { fromNode: GraphNodeRow; toNode: GraphNodeRow } | null {
  if (!connection.source || !connection.target) return null;
  const fromNode = nodeById.get(connection.source);
  const toNode = nodeById.get(connection.target);
  if (!fromNode || !toNode) return null;
  return { fromNode, toNode };
}

/** A 404 on any graph-editing write means "not permitted", never "crash" (CONTEXT_PLATFORM_WRITERS). */
export function isPermissionDenied(status: number): boolean {
  return status === 404 || status === 403;
}

// ---------------------------------------------------------------------------
// View projections — Story 44.6. Pure, client-side re-shapes of whatever
// `applyGraphFilters` already produced ("visible"): switching views NEVER
// refetches and never re-derives the filter — it only reshapes the SAME
// node/edge set that is already in memory. Domains is exactly the 44.4
// layered-RIGHT view; the other three are additive.
// ---------------------------------------------------------------------------

export type GraphView = "domains" | "procedures" | "schema" | "orphans";

export const GRAPH_VIEW_ORDER: GraphView[] = ["domains", "procedures", "schema", "orphans"];

export const GRAPH_VIEW_LABELS: Record<GraphView, string> = {
  domains: "Domains",
  procedures: "Skills",
  schema: "Schema",
  orphans: "Orphans",
};

export interface GraphProjection {
  nodes: GraphNodeRow[];
  edges: GraphEdgeRow[];
  elkOptions: Record<string, string>;
  /** Node id -> group name, laid out as its own column (elk partitioning).
   * Schema view: the module/table-prefix group. Domains view: the owning
   * Business Domain, or "Unlinked" — a knowledge card floating across every
   * trade was the defect this grouping repairs. */
  groups?: Record<string, string>;
  /**
   * Procedures view only. elk has no boolean "roots first" flag: the effect is
   * obtained by ORDERING procedure nodes first (see `projectGraph`) under
   * `considerModelOrder`. This sibling field records that the projection made
   * that choice deliberately — it is OUR marker, never an elk option, so it
   * stays out of `elkOptions` (elk warns on unknown keys) and is what the
   * projection test asserts.
   */
  proceduresFirst?: boolean;
}

const DOMAINS_ELK_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.layered.spacing.nodeNodeBetweenLayers": "120",
  "elk.spacing.nodeNode": "40",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
};

/**
 * Procedures first: elk's layered algorithm seeds layer placement from
 * `considerModelOrder.strategy: NODES_AND_EDGES`, which respects the order
 * nodes are handed in. Listing procedure nodes first in `children` (below)
 * combined with a DOWN direction is what pulls them into the top layer,
 * roots-first, instead of wherever topics/schema docs happen to land them.
 * elk itself has no boolean "roots first" flag; the fact that this projection
 * chose the ordering on purpose is recorded on the projection object itself
 * (`proceduresFirst`), NOT as a fake `x-…` entry in the options elk receives.
 */
const PROCEDURES_ELK_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "DOWN",
  "elk.layered.spacing.nodeNodeBetweenLayers": "100",
  "elk.spacing.nodeNode": "40",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
};

/** `elk.partitioning.activate` + a per-node partition index (assigned from
 * `groups`) keeps each schema module laid out as its own column. */
const SCHEMA_ELK_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "DOWN",
  "elk.layered.spacing.nodeNodeBetweenLayers": "90",
  "elk.spacing.nodeNode": "36",
  "elk.partitioning.activate": "true",
};

/** Orphans: no edges by construction, so a grid needs no elk options at all;
 * kept only so every projection has a well-typed elkOptions value. */
const ORPHANS_ELK_OPTIONS: Record<string, string> = {};

/**
 * Grouping key for the Schema view: the module/table prefix before the first
 * ".", e.g. `"marts.fct_campaign_daily"` -> `"marts"`. Falls back to the
 * whole title when there is no ".".
 */
export function schemaGroupOf(title: string): string {
  const idx = title.indexOf(".");
  return idx === -1 ? title : title.slice(0, idx);
}

/**
 * Pure re-projection of an already-filtered node/edge set (exported so tests
 * assert the subset and elk options directly, without a canvas).
 */
export function projectGraph(
  view: GraphView,
  nodes: GraphNodeRow[],
  edges: GraphEdgeRow[],
): GraphProjection {
  if (view === "domains") {
    // Group every node under its OWNING Business Domain. The contains chain
    // (domain -> classification -> classification) gives each classification
    // its root; a governed object inherits the root of the key that links to
    // it. What nobody claimed lands in "Unlinked" — visible, not scattered:
    // the previous pass-through let every connector card float across every
    // trade's band.
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const parentOf = new Map<string, string>();
    for (const e of edges) {
      if (e.edge_type !== "contains") continue;
      const parent = byId.get(e.from_id);
      if (
        parent &&
        (parent.node_type === "business_domain" ||
          parent.node_type === "business_classification")
      ) {
        parentOf.set(e.to_id, e.from_id);
      }
    }
    const domainTitleOf = (id: string, seen = new Set<string>()): string | null => {
      const n = byId.get(id);
      if (!n || seen.has(id)) return null;
      if (n.node_type === "business_domain") return n.title;
      if (n.node_type !== "business_classification") return null;
      seen.add(id);
      const parent = parentOf.get(id);
      return parent ? domainTitleOf(parent, seen) : null;
    };
    const groups: Record<string, string> = {};
    for (const n of nodes) {
      if (n.node_type === "business_domain") {
        groups[n.id] = n.title;
        continue;
      }
      if (n.node_type === "business_classification") {
        groups[n.id] = domainTitleOf(n.id) ?? "Unlinked";
        continue;
      }
      let owner: string | null = null;
      for (const e of edges) {
        if (e.to_id !== n.id || e.edge_type === "contains") continue;
        const from = byId.get(e.from_id);
        if (!from) continue;
        if (from.node_type === "business_domain") {
          owner = from.title;
          break;
        }
        if (from.node_type === "business_classification") {
          owner = domainTitleOf(e.from_id);
          if (owner) break;
        }
      }
      groups[n.id] = owner ?? "Unlinked";
    }
    const ordered = [...nodes].sort(
      (a, b) => groups[a.id].localeCompare(groups[b.id]) || a.title.localeCompare(b.title),
    );
    return {
      nodes: ordered,
      edges,
      elkOptions: { ...DOMAINS_ELK_OPTIONS, "elk.partitioning.activate": "true" },
      groups,
    };
  }

  if (view === "procedures") {
    const rank = (n: GraphNodeRow) => (n.node_type === "procedure" ? 0 : 1);
    const ordered = [...nodes].sort((a, b) => rank(a) - rank(b));
    return {
      nodes: ordered,
      edges,
      elkOptions: PROCEDURES_ELK_OPTIONS,
      proceduresFirst: true,
    };
  }

  if (view === "schema") {
    const schemaNodes = nodes.filter((n) => n.node_type === "schema_doc");
    const ids = new Set(schemaNodes.map((n) => n.id));
    // Only edges strictly between two visible schema docs survive — an edge
    // to a hidden topic/procedure would dangle (same rule as applyGraphFilters).
    const schemaEdges = edges.filter((e) => ids.has(e.from_id) && ids.has(e.to_id));
    const groups: Record<string, string> = {};
    for (const n of schemaNodes) groups[n.id] = schemaGroupOf(n.title);
    const ordered = [...schemaNodes].sort((a, b) => {
      const byGroup = groups[a.id].localeCompare(groups[b.id]);
      return byGroup !== 0 ? byGroup : a.title.localeCompare(b.title);
    });
    return { nodes: ordered, edges: schemaEdges, elkOptions: SCHEMA_ELK_OPTIONS, groups };
  }

  // Orphans: nodes with no edge touching either end, from either side.
  const touched = new Set<string>();
  for (const e of edges) {
    touched.add(e.from_id);
    touched.add(e.to_id);
  }
  const orphanNodes = nodes.filter((n) => !touched.has(n.id));
  return { nodes: orphanNodes, edges: [], elkOptions: ORPHANS_ELK_OPTIONS };
}

/** Stable group -> partition-index map (alphabetical by group name). */
function groupPartitionIndex(groups: Record<string, string>): Record<string, number> {
  const names = Array.from(new Set(Object.values(groups))).sort();
  const order: Record<string, number> = {};
  names.forEach((name, i) => (order[name] = i));
  return order;
}

/**
 * Defensive: elk rejects/warns on layout options it does not know, so no
 * private `x-…` marker may ever reach it. Projections carry their own markers
 * as sibling fields (`proceduresFirst`), but this strip keeps that a property
 * of the boundary rather than of every projection author remembering it.
 */
export function stripPrivateOptions(options: Record<string, string>): Record<string, string> {
  const clean: Record<string, string> = {};
  for (const [key, value] of Object.entries(options)) {
    if (key.startsWith("x-")) continue;
    clean[key] = value;
  }
  return clean;
}

/** elk graph description for a projection: the projection's own elk options
 * (private markers stripped) plus a per-node partition when it supplies
 * `groups` — the mechanism the Schema and Domains views share. */
export function toElkGraphForProjection(projection: GraphProjection) {
  const { nodes, edges, elkOptions, groups } = projection;
  const partitionOf = groups ? groupPartitionIndex(groups) : null;
  return {
    id: "root",
    layoutOptions: stripPrivateOptions(elkOptions),
    children: nodes.map((n) => {
      const base = { id: n.id, width: NODE_WIDTH, height: NODE_HEIGHT };
      if (!partitionOf) return base;
      return {
        ...base,
        layoutOptions: { "elk.partitioning.partition": String(partitionOf[groups![n.id]]) },
      };
    }),
    edges: edges.map((e) => ({ id: e.id, sources: [e.from_id], targets: [e.to_id] })),
  };
}

/** Grid layout for Orphans: edge-less by construction, so no elk is run. */
export function gridPositions(
  nodes: GraphNodeRow[],
  columns = 4,
): Record<string, { x: number; y: number }> {
  const gapX = 48;
  const gapY = 40;
  const positions: Record<string, { x: number; y: number }> = {};
  nodes.forEach((n, i) => {
    const col = i % columns;
    const row = Math.floor(i / columns);
    positions[n.id] = { x: col * (NODE_WIDTH + gapX), y: row * (NODE_HEIGHT + gapY) };
  });
  return positions;
}

/** Run elk (or the Orphans grid) for a projection. Async by nature — never
 * called in render. Uses the single module-level `elk` instance. */
export async function layoutProjection(
  view: GraphView,
  projection: GraphProjection,
): Promise<Record<string, { x: number; y: number }>> {
  if (projection.nodes.length === 0) return {};
  if (view === "orphans") return gridPositions(projection.nodes);
  const result = await elk.layout(toElkGraphForProjection(projection));
  const positions: Record<string, { x: number; y: number }> = {};
  for (const child of result.children ?? []) {
    positions[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
  }
  return positions;
}

// ---------------------------------------------------------------------------
// The AI Path overlay — Story 49.6 AC7
// ---------------------------------------------------------------------------
//
// `context-hub.md` says a path can be "aggregated into Knowledge Graph
// overlays". AC7 says how: the overlay "decorates the current canonical graph
// and does not mutate it", and "the React client does not join AI Path, graph,
// Feedback and owner APIs into an authority".
//
// So this file fetches a DECORATION and draws it. It does not read steps, does
// not compare versions and does not decide what `used` means — `core.ai_paths`
// derives every state from the snapshot pinned before the run, and a second
// derivation here would be a second opinion on evidence.
//
// The canonical bundle is untouched: overlay states land on nodes already laid
// out, exactly like search highlighting, so selecting a path never re-runs the
// layout. A decoration that moved the graph would be a mutation arrived at
// through the viewport.

/** The five states the owner distinguishes. Mirrors `OVERLAY_STATE_*`. */
type OverlayState = "used" | "missing" | "version_mismatch" | "forbidden" | "unverifiable";

interface OverlayNode {
  node_type: NodeTypeKey;
  node_id: string;
  state: OverlayState;
  ordinals: number[];
  expected_version?: string | null;
  observed_version?: string | null;
}

/**
 * An Event the path went through — Story 49.6 AC9.
 *
 * NOT a node, and that is the design, not a shortcut. The graph vocabulary is
 * closed at ten types and carries no Event; `data.md` keeps the Event owned by
 * its Datastream, and AC9 says a graph "may DISPLAY an Event", never draw one.
 * So it appears as a reference beside the canvas, resolved server-side by its
 * own owner and reachable through a link — or as `unavailable`, which is the
 * exact word migration 133 put on the column.
 */
interface OverlayEventReference {
  event_id: string;
  ordinals: number[];
  binding_state: "linked" | "unavailable";
  event_type?: string | null;
  event_date?: string | null;
  datastream_id?: string | null;
  version_number?: number | null;
  /** The semantic route to the Data-owned Event Configuration. */
  owner_route?: OwnerReference | null;
}

interface AiPathOverlay {
  path_id: string;
  lifecycle?: string | null;
  outcome?: string | null;
  verdict?: string | null;
  nodes: OverlayNode[];
  event_references: OverlayEventReference[];
  /** Steps that reached no drawable node. Reported, never invented as one. */
  unrepresented_steps: number;
}

/** One entry of the path picker. The list route already returns these. */
interface AiPathSummary {
  id: string;
  lifecycle?: string | null;
  outcome?: string | null;
  started_at?: string | null;
  /** 2026-09-05: the list route says how many steps a path has and which tools it walked. */
  steps?: number | null;
  tools?: string[] | null;
}

/** « aip_… · 7 steps · get_procedure, compose_analyze_pivot, … » -- an id alone named nothing. */
export function aiPathOptionLabel(p: AiPathSummary): string {
  const parts = [p.id];
  if (p.lifecycle === "recording") parts.push("recording");
  if (p.outcome) parts.push(p.outcome);
  if (typeof p.steps === "number" && p.steps > 0) parts.push(`${p.steps} step${p.steps > 1 ? "s" : ""}`);
  if (p.tools && p.tools.length > 0) {
    const shown = p.tools.slice(0, 3).join(", ");
    parts.push(p.tools.length > 3 ? `${shown}, …` : shown);
  }
  return parts.join(" · ");
}

/** The ONE key a decoration and a canvas node are matched on.
 *
 * Neither half is unique alone: `target_field` nodes are keyed by field NAME
 * and `topic` by an id, so two vocabularies can collide on a bare id. Written
 * inline in four places it was already two different spellings. */
const overlayKey = (nodeType: string, nodeId: string) => `${nodeType}::${nodeId}`;

const OVERLAY_STATE_LABELS: Record<OverlayState, string> = {
  used: "Used",
  missing: "Required, not reached",
  version_mismatch: "Wrong version",
  forbidden: "Forbidden, reached",
  unverifiable: "Unproven",
};

// ---------------------------------------------------------------------------
// Custom node card
// ---------------------------------------------------------------------------

type KgNodeData = {
  node: GraphNodeRow;
  /** True when the current search query matches this node. */
  match: boolean;
  /** True when a search is active and this node is NOT a match. */
  dim: boolean;
  query: string;
  /** What the selected AI Path did with this node, or null when none is on. */
  overlay: OverlayNode | null;
};
type KgFlowNode = Node<KgNodeData, "kg">;

/** Split text around the query so matches can be <mark>ed without a dependency. */
function highlight(text: string, query: string) {
  const q = query.trim();
  if (!q) return text;
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  const parts: Array<string | { hit: string; key: number }> = [];
  let cursor = 0;
  let found = lower.indexOf(needle, cursor);
  let key = 0;
  while (found !== -1) {
    if (found > cursor) parts.push(text.slice(cursor, found));
    parts.push({ hit: text.slice(found, found + needle.length), key: key++ });
    cursor = found + needle.length;
    found = lower.indexOf(needle, cursor);
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts.map((p) =>
    typeof p === "string" ? (
      p
    ) : (
      <mark className="kg-mark" key={p.key}>
        {p.hit}
      </mark>
    ),
  );
}

function KnowledgeNode({ data, selected }: NodeProps<KgFlowNode>) {
  const { node, match, dim, query, overlay } = data;
  const classes = [
    "kg-node",
    `kg-node--${node.node_type}`,
    selected ? "is-selected" : "",
    match ? "is-match" : "",
    dim ? "is-dim" : "",
    overlay ? `kg-node--path-${overlay.state}` : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={classes}
      data-testid={`kg-node-${node.id}`}
      data-node-type={node.node_type}
      data-match={match ? "true" : "false"}
      data-path-state={overlay ? overlay.state : undefined}
    >
      <Handle type="target" position={Position.Left} className="kg-handle" />
      <div className="kg-node-head">
        <span className="kg-node-kind">{NODE_TYPE_LABELS[node.node_type]}</span>
        {node.scope === "platform" && <span className="kg-tag kg-tag--platform">Platform</span>}
        {node.node_type === "schema_doc" && <span className="kg-tag kg-tag--auto">auto</span>}
        {node.doc_kind ? <span className="kg-tag kg-tag--kind">{graphWord(node.doc_kind)}</span> : null}
        {/* metric / dimension — dictionary fields only (44.10). Absent
            field_kind renders nothing rather than a guessed badge. */}
        {node.node_type === "target_field" && node.field_kind ? (
          <span className="kg-tag kg-tag--field-kind" data-testid={`kg-field-kind-${node.id}`}>
            {graphWord(node.field_kind)}
          </span>
        ) : null}
        {/* AC7: used, missing and version-mismatched must be DISTINGUISHABLE.
            Colour alone would not be — the state is named on the card, and the
            walk order comes with it so a reader can follow the path without
            leaving the canvas. A `missing` node carries no ordinal because
            nothing walked it, and printing one would invent the visit. */}
        {overlay ? (
          <span
            className={`kg-tag kg-tag--path kg-tag--path-${overlay.state}`}
            data-testid={`kg-path-state-${node.id}`}
            title={
              overlay.state === "version_mismatch"
                ? `Pinned ${overlay.expected_version ?? "?"}, observed ${overlay.observed_version ?? "?"}`
                : undefined
            }
          >
            {OVERLAY_STATE_LABELS[overlay.state]}
            {overlay.ordinals.length > 0 ? ` · step ${overlay.ordinals.join(", ")}` : ""}
          </span>
        ) : null}
      </div>
      <h3 className="kg-node-title">{highlight(node.title, query)}</h3>
      <p className="kg-node-excerpt">{highlight(node.excerpt, query)}</p>
      <div className="kg-node-foot">
        <span className="kg-node-owner">{node.owner ?? "Generated"}</span>
        <span className="kg-node-version">v{node.version_number}</span>
      </div>
      <Handle type="source" position={Position.Right} className="kg-handle" />
    </div>
  );
}

/** Must be module-level and stable: React Flow re-mounts nodes otherwise. */
const NODE_TYPES: NodeTypes = { kg: KnowledgeNode };

// ---------------------------------------------------------------------------
// View switcher — Story 44.6. A segmented control, deliberately its own row
// rather than folded into the "Graph filters" toolbar above: it re-projects
// the SAME visible set, it does not filter it.
// ---------------------------------------------------------------------------

function GraphViewSwitcher({
  view,
  onChange,
}: {
  view: GraphView;
  onChange: (next: GraphView) => void;
}) {
  return (
    <div className="kg-view-switcher" role="group" aria-label="Graph view">
      {GRAPH_VIEW_ORDER.map((v) => (
        <button
          key={v}
          type="button"
          className={`kg-view-option${v === view ? " is-active" : ""}`}
          aria-pressed={v === view}
          data-testid={`kg-view-${v}`}
          onClick={() => onChange(v)}
        >
          {GRAPH_VIEW_LABELS[v]}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page state
// ---------------------------------------------------------------------------

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; bundle: GraphBundle };

type DetailState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; body_md: string; updated_at: string | null; step_count: number | null }
  /** schema_doc: no single-doc route exists, the excerpt is all we honestly have. */
  | { status: "excerpt_only" }
  /**
   * target_field (44.10): the bundle already carries everything the graph
   * shows about a dictionary field (display name, description, kind, owner,
   * version). There is no richer "body" to fetch — the full record lives on
   * the Semantic model page — so the drawer renders a read-only summary and
   * fires NO detail request at all.
   */
  | { status: "field_summary" }
  | { status: "error"; message: string };

/** One row of the "Fed by" panel — a datastream mapping that lands in the field. */
export interface FedByRow {
  datastream_id: string;
  datastream_name: string | null;
  module_name: string | null;
  project_id: string | null;
  enabled?: boolean | null;
  source_field: string;
  is_key_column?: boolean | null;
}

type FedByState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; rows: FedByRow[] }
  | { status: "error"; message: string };

async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { message?: string; code?: string };
    return body.message ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

function formatUpdated(iso: string | null): string | null {
  return iso ? formatDate(iso) : null;
}

/** Suggestion buttons + free text -> POST /api/context/graph/edges (Story 44.5). */
type EdgeModalState =
  | { status: "closed" }
  | {
      status: "open";
      fromId: string;
      toId: string;
      fromType: NodeTypeKey;
      toType: NodeTypeKey;
      edgeType: string;
      saving: boolean;
      error: string | null;
    };

/** Shared editor for both "New topic here" (canvas context menu) and the
 * drawer's topic Edit button — same title/body_md/preview shape as
 * KnowledgeBasePage's editor (Story 44.1), kept local here since that page's
 * editor is a full-page component (not a reusable sub-component) and this
 * page owns its own modal chrome (kg-* tokens, not knowledge-editor-*). */
type TopicEditorState =
  | { status: "closed" }
  | {
      status: "create";
      title: string;
      body_md: string;
      /** Raw explicit owner text (Story 44.11), e.g. an email. Empty = unset. */
      owner: string;
      saving: boolean;
      error: string | null;
    }
  | {
      status: "edit";
      nodeId: string;
      title: string;
      body_md: string;
      owner: string;
      saving: boolean;
      error: string | null;
    };

type ContextMenuState = { x: number; y: number } | null;

/** "Request review" modal state (Story 44.11) -- shared by topic and
 * procedure drawers. Audit-only: no notification is ever sent. */
type ReviewModalState =
  | { status: "closed" }
  | { status: "open"; nodeId: string; nodeType: "topic" | "procedure"; note: string; saving: boolean; error: string | null }
  | { status: "success" };

export default function KnowledgeGraphPage({
  // Aucun projet par defaut (2026-07-27) : un "default" implicite faisait
  // lire le projet d'un autre. Le routeur fournit toujours l'id.
  projectId,
  onOpenKnowledge,
  onOpenProcedure,
  onAdjustNode,
  onOpenSemanticModel,
  onOpenEvent,
  onOpenAiPath,
  onFlowInit,
  businessScope,
  initialBundle,
}: {
  projectId: string;
  /** Navigates to Context ▸ Knowledge — wired by ContentRouter for the empty-state CTA. */
  onOpenKnowledge?: () => void;
  /**
   * Opens the Skill's own page (Context ▸ Skills Registry ▸ object) — Story
   * 44.5's drawer action for a procedure node. A Skill is worked on its
   * workbench, where its standardized steps are read and edited; navigating
   * to the bare collection left the operator to find the card by hand.
   */
  onOpenProcedure?: (procedureId: string) => void;
  /**
   * ANSWER a remark, from the drawer that shows it.
   *
   * `context-hub.md` is explicit that "one does not work a Skill inside a
   * mindmap": the adjustment a remark asks for is a new version, and a new
   * version is written on the node's own workbench. So the mindmap does not
   * grow an editor — it hands the reader over to the surface that owns the
   * write, exactly as `onOpenProcedure` and `onOpenSemanticModel` already do.
   *
   * Without this the drawer offered Accept and Decline and nothing else: the
   * queue could be emptied here and the corpus never changed, which is the
   * defect named in "a remark can only be closed and not acted on".
   *
   * Absent prop => no button, rather than a button that goes nowhere.
   */
  onAdjustNode?: (nodeType: "topic" | "procedure", nodeId: string) => void;
  /**
   * Navigates to Governance ▸ Semantic model — Story 44.10's drawer action for
   * a dictionary-field node. Mounted exactly like `onOpenProcedures`: the
   * mindmap never edits the dictionary, it hands the operator over to the page
   * that owns it. Absent prop => an honest note instead of a dead button.
   */
  onOpenSemanticModel?: () => void;
  /**
   * Navigates to the Data-owned Event Configuration an AI Path went through
   * (Story 49.6 AC9). The reference is COMPOSED BY THE SERVER — this page never
   * builds a route to Data from parts, because a route assembled here would be
   * Context Hub deciding where an Event lives.
   *
   * Absent prop => the reference is still SHOWN, without a link. AC9 forbids
   * Context Hub from owning the Event, not from naming it; hiding the row
   * because the shell wired no handler would hide evidence to avoid a dead
   * button.
   */
  onOpenEvent?: (reference: OwnerReference) => void;
  /**
   * Opens one observed AI Path on its own screen — `AiPathPage`, served by
   * `objectSurfaces.tsx` for `{ workspace: "context-hub", objectType:
   * "ai-path" }` and declared under this very section in
   * `navigation/contextHub.ts`.
   *
   * IT HAD NO DOOR. The picker below listed every path the project recorded and
   * navigated nowhere: the only way to that screen was to type its address. A
   * lens that names a thing and cannot open it is where the reading stops —
   * the overlay decorates the canvas, it does not carry the steps, the branch
   * evidence or the assessment.
   *
   * Absent prop => the path is still listed, without the control. The same
   * decision `onOpenEvent` states above: a shell that wired no handler must not
   * grow a button that does nothing.
   */
  onOpenAiPath?: (pathId: string) => void;
  /**
   * Handed the React Flow instance the moment it initialises. The page keeps
   * its own reference regardless; this is the seam that lets a caller (today:
   * the projections test, which asserts a view switch really re-fits the
   * viewport) observe the same instance the page calls `fitView` on, without
   * mocking the library out from under the layout.
   */
  onFlowInit?: (instance: ReactFlowInstance<KgFlowNode, Edge>) => void;
  /**
   * Story 45.3: the Context Hub selection, its taxonomy and its authoritative
   * links. Absent prop => the complete bundle, which is what a caller mounting
   * the mindmap outside Context Hub gets. Filtering is client-side: changing the
   * scope never issues a new graph request.
   */
  businessScope?: BusinessScopeInput;
  /**
   * The bundle the Context Hub layout ALREADY read for its taxonomy tree.
   * Provided => this page adopts it instead of paying a second identical
   * `GET /api/context/graph` on mount; a later layout refresh (new identity)
   * is adopted the same way, and it carries this page's own writes since they
   * were POSTed to the server first. Absent => the page fetches, as before.
   */
  initialBundle?: GraphBundle | null;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [filters, setFilters] = useState<GraphFilterState>(DEFAULT_FILTERS);
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DetailState>({ status: "idle" });
  const [layoutError, setLayoutError] = useState<string | null>(null);

  // ── Story 44.5: edges + quick actions ────────────────────────────────────
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [confirmDeleteEdgeId, setConfirmDeleteEdgeId] = useState<string | null>(null);
  const [deletingEdgeId, setDeletingEdgeId] = useState<string | null>(null);
  const [edgeActionError, setEdgeActionError] = useState<{ id: string; message: string } | null>(
    null,
  );
  const [edgeModal, setEdgeModal] = useState<EdgeModalState>({ status: "closed" });
  const [topicEditor, setTopicEditor] = useState<TopicEditorState>({ status: "closed" });
  const [contextMenu, setContextMenu] = useState<ContextMenuState>(null);
  const [reviewModal, setReviewModal] = useState<ReviewModalState>({ status: "closed" });

  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<KgFlowNode>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [rf, setRf] = useState<ReactFlowInstance<KgFlowNode, Edge> | null>(null);
  const handleFlowInit = useCallback(
    (instance: ReactFlowInstance<KgFlowNode, Edge>) => {
      setRf(instance);
      onFlowInit?.(instance);
    },
    [onFlowInit],
  );

// ── Story 49.6 AC7: the AI Path overlay ──────────────────────────────────
  //
  // The path list loads ON DEMAND, not with the page. This file's own contract
  // is "ONE call per load" (see the header), and a lens nobody opened must not
  // spend a request proving it has nothing to offer. `KnowledgeGraphPage.test`
  // asserts the count, and it caught this.
  const [paths, setPaths] = useState<AiPathSummary[]>([]);
  const [pathsStats, setPathsStats] = useState<AiPathStats | null>(null);
  /** The usage reading beside the drift one. `null` means it could not be read —
   *  and an unreadable measurement renders NOTHING, never a zero. */
  const [pathsInspections, setPathsInspections] = useState<AiPathInspectionSummary | null>(null);
  const [pathsState, setPathsState] = useState<"idle" | "loading" | "loaded" | "unavailable">(
    "idle",
  );
  /** Observations the server swallowed. An empty list plus this > 0 is not
   *  "nothing happened" — it is "we could not write down what happened". */
  const [pathsFailures, setPathsFailures] = useState(0);
  const [overlayPathId, setOverlayPathId] = useState<string>("");
  const [overlay, setOverlay] = useState<AiPathOverlay | null>(null);
  const [overlayError, setOverlayError] = useState<string | null>(null);

  const searchInputRef = useRef<HTMLInputElement>(null);
  // Read inside the layout effect so a relayout does not wipe the highlight.
  const searchRef = useRef(search);
  searchRef.current = search;
  // Same reason, same mechanism: a relayout must not drop the decoration.
  const overlayRef = useRef(overlay);
  overlayRef.current = overlay;

  // ── One fetch per load — and zero when the layout already paid for it. ───
  const load = useCallback(async () => {
    if (initialBundle) {
      setState({ status: "ok", bundle: initialBundle });
      return;
    }
    setState({ status: "loading" });
    try {
      const res = await apiFetch(
        `/api/context/graph?project_id=${encodeURIComponent(projectId)}`,
      );
      if (!res.ok) {
        setState({ status: "error", message: await readErrorMessage(res) });
        return;
      }
      const body = (await res.json()) as Partial<GraphBundle>;
      setState({
        status: "ok",
        bundle: { nodes: body.nodes ?? [], edges: body.edges ?? [] },
      });
    } catch (err) {
      setState({
        status: "error",
        message: err instanceof Error ? err.message : "Network error",
      });
    }
  }, [projectId, initialBundle]);

  useEffect(() => {
    void load();
  }, [load]);

  const bundle = state.status === "ok" ? state.bundle : null;
  const allNodes = useMemo(() => bundle?.nodes ?? [], [bundle]);
  const allEdges = useMemo(() => bundle?.edges ?? [], [bundle]);

  // Business scope first, then the node/edge/scope filters, then the view
  // projection. All three are pure reshapes of the payload already in memory.
  const scoped = useMemo(
    () => (businessScope ? applyBusinessScope(allNodes, allEdges, businessScope) : { nodes: allNodes, edges: allEdges }),
    [allNodes, allEdges, businessScope],
  );
  const visible = useMemo(
    () => applyGraphFilters(scoped.nodes, scoped.edges, filters),
    [scoped, filters],
  );

  const nodeById = useMemo(() => {
    const map = new Map<string, GraphNodeRow>();
    for (const n of allNodes) map.set(n.id, n);
    return map;
  }, [allNodes]);

  /** Ids the FILTERS keep. Not the same thing as "on the canvas": a view can
   * legitimately drop a node the filters kept (Schema hides topics, Orphans
   * hides everything linked), which is why the two labels below differ. */
  const visibleIds = useMemo(() => new Set(visible.nodes.map((n) => n.id)), [visible]);

  // ── View projection (44.6): a pure client-side re-shape of `visible` — no
  // refetch ever happens here, only reshaping the payload already in memory.
  const [view, setView] = useState<GraphView>("domains");
  const projected = useMemo(
    () => projectGraph(view, visible.nodes, visible.edges),
    [view, visible],
  );
  const projectedRef = useRef(projected);
  projectedRef.current = projected;
  const layoutGenRef = useRef(0);
  /** Incremented every time a layout is actually committed to the canvas. */
  const [layoutTick, setLayoutTick] = useState(0);

  /** Ids ACTUALLY drawn on the canvas — filters AND the current view. The
   * drawer must agree with the map, so every "can the operator still see it?"
   * question below is asked of this set, never of `visibleIds` (44.6 review:
   * projections were bypassing the 44.4 invariant). */
  const projectedIds = useMemo(() => new Set(projected.nodes.map((n) => n.id)), [projected]);
  const projectedEdgeIds = useMemo(() => new Set(projected.edges.map((e) => e.id)), [projected]);

  // A node the operator can no longer see cannot keep its drawer open: the
  // drawer would be describing something absent from the canvas.
  useEffect(() => {
    setSelectedId((current) => (current && !projectedIds.has(current) ? null : current));
  }, [projectedIds]);

  // Same invariant for the CANVAS edge selection. Without it a filter (or a
  // view switch) could hide the selected edge while the Delete key still
  // deleted it — a blind destructive action on something the operator cannot
  // see. The armed confirm and the error banner belong to that selection, so
  // they go with it (drawer-row confirms, which are keyed on a row the
  // operator IS looking at, are deliberately left alone).
  useEffect(() => {
    if (!selectedEdgeId || projectedEdgeIds.has(selectedEdgeId)) return;
    setSelectedEdgeId(null);
    setConfirmDeleteEdgeId((current) => (current === selectedEdgeId ? null : current));
    setEdgeActionError((current) => (current?.id === selectedEdgeId ? null : current));
  }, [projectedEdgeIds, selectedEdgeId]);

  /**
   * Re-lays out whatever `projectedRef` currently holds. Extracted so the
   * debounced auto-relayout effect below and the "Tidy" button run the exact
   * same code path — Tidy is not a different layout, just an on-demand rerun
   * of the current projection's layout (44.6 scope item 4). `layoutGenRef`
   * discards a stale response instead of a boolean `cancelled` flag, because
   * Tidy can fire this concurrently with the debounced effect.
   */
  const relayout = useCallback(async () => {
    const gen = ++layoutGenRef.current;
    const current = projectedRef.current;
    try {
      const positions = await layoutProjection(view, current);
      if (layoutGenRef.current !== gen) return;
      const query = searchRef.current;
      // Read from the ref, not from the closure: a relayout (Tidy, a filter
      // toggle, a view switch) rebuilds every node's data from scratch, and
      // omitting the decoration here would silently clear the lens the person
      // had selected — the same reason `searchRef` exists two lines up.
      const decoration = new Map(
        (overlayRef.current?.nodes ?? []).map((o) => [overlayKey(o.node_type, o.node_id), o]),
      );
      setRfNodes(
        current.nodes.map((n) => ({
          id: n.id,
          type: "kg" as const,
          position: positions[n.id] ?? { x: 0, y: 0 },
          data: {
            node: n,
            query,
            match: nodeMatchesQuery(n, query),
            dim: query.trim() !== "" && !nodeMatchesQuery(n, query),
            overlay: decoration.get(overlayKey(n.node_type, n.id)) ?? null,
          },
        })),
      );
      setRfEdges(toFlowEdges(current.edges));
      setLayoutError(null);
      // Record WHICH generation is being committed before bumping the tick:
      // the refit effect below compares it against the generation it is
      // waiting for (44.4 re-review: a boolean flag could be consumed by a
      // stale filter-toggle commit racing a view switch inside the debounce
      // window, fitting the pre-switch layout and starving the real one).
      committedGenRef.current = gen;
      // Bumped in the SAME batch as the node commit: an effect on this counter
      // therefore runs once the new positions are on screen (see below).
      setLayoutTick((t) => t + 1);
    } catch (err) {
      if (layoutGenRef.current !== gen) return;
      setLayoutError(err instanceof Error ? err.message : "Layout failed");
    }
  }, [view, setRfNodes, setRfEdges]);

  // Auto-layout: on load and on every structural filter or view change,
  // debounced ~120ms so rapid clicking (toggling filters, switching views)
  // triggers one elk run instead of one per click (44.6 perf requirement).
  useEffect(() => {
    const timer = setTimeout(() => {
      void relayout();
    }, 120);
    return () => clearTimeout(timer);
  }, [projected, relayout]);

  /**
   * Refit-after-commit. `relayout()` resolving only means setRfNodes was
   * CALLED: React has not re-rendered yet, so fitting inside that `.then()`
   * fits the OLD layout. The flag is consumed by an effect on the LAYOUT
   * counter — not on `rfNodes`, which React Flow also mutates for its own
   * measurement passes and would consume the flag before the new positions
   * exist — and the fit is deferred one more frame so React Flow has measured
   * them.
   *
   * Only a VIEW change and Tidy arm it — a filter toggle deliberately leaves
   * the viewport where the operator put it (the same anti-yank rule the search
   * centring follows).
   */
  /** The layout GENERATION the pending refit is waiting for (null = none).
   * Generation-based rather than a boolean so a stale commit that races the
   * view switch cannot consume the refit (44.4 re-review LOW). */
  const refitForGenRef = useRef<number | null>(null);
  const committedGenRef = useRef(0);
  useEffect(() => {
    if (layoutTick === 0 || refitForGenRef.current === null || !rf) return;
    if (committedGenRef.current < refitForGenRef.current) return; // stale commit
    refitForGenRef.current = null;
    // Nothing on the canvas: fitView would have no bounds to work with.
    if (projectedRef.current.nodes.length === 0) return;
    const frame = requestAnimationFrame(() => {
      rf.fitView({ duration: 300 });
    });
    return () => cancelAnimationFrame(frame);
  }, [layoutTick, rf]);

  /** AC1: switching view re-centres on the new shape, it does not leave the
   * operator staring at the old viewport over a freshly re-laid-out graph.
   * The refit waits for the NEXT layout generation — the one this change
   * will trigger — never an in-flight one. */
  const handleViewChange = useCallback((next: GraphView) => {
    refitForGenRef.current = layoutGenRef.current + 1;
    setView(next);
  }, []);

  /** "Tidy": re-run the current projection's layout right now, then refit
   * once the new positions have actually been committed. */
  const handleTidy = useCallback(() => {
    refitForGenRef.current = layoutGenRef.current + 1;
    void relayout();
  }, [relayout]);

  // ── Search: highlight in place (positions kept), then centre the first hit.
  useEffect(() => {
    setRfNodes((prev) =>
      prev.map((n) => {
        const match = nodeMatchesQuery(n.data.node, search);
        const dim = search.trim() !== "" && !match;
        if (n.data.match === match && n.data.dim === dim && n.data.query === search) return n;
        return { ...n, data: { ...n.data, match, dim, query: search } };
      }),
    );
  }, [search, setRfNodes]);

  // ── AI Path overlay: the picker's contents, fetched the first time the
  // picker is actually reached for. ───────────────────────────────────────
  //
  // A failure here NEVER takes the graph down: an unreadable path history is a
  // missing lens, not a missing mindmap. It is also not an EMPTY one --
  // `unavailable` and `loaded with none` stay apart, the same distinction
  // `AiPathPage` makes ("This is not an empty history").
  const ensurePaths = useCallback(() => {
    if (pathsState !== "idle") return;
    setPathsState("loading");
    apiFetch(`/api/projects/${encodeURIComponent(projectId)}/context/ai-paths?limit=25`, {
      cache: "no-store",
    })
      .then(async (res) => {
        if (!res.ok) {
          setPaths([]);
          setPathsState("unavailable");
          return;
        }
        const body = (await res.json()) as {
          paths?: AiPathSummary[];
          recording_failures_this_instance?: number;
        };
        setPaths(body.paths ?? []);
        // An empty list is only "nothing happened" when nothing FAILED. The
        // server counts what it swallowed, and the picker says so instead of
        // reporting silence as absence — the distinction `context-hub.md`
        // requires of the context store, and the one that let a recorder
        // record nothing for a day while every test was green.
        setPathsFailures(body.recording_failures_this_instance ?? 0);
        setPathsState("loaded");
        // The drift line, fetched with the list. A missing aggregate must
        // never read as "no drift": on failure the line simply does not
        // render, and the list keeps its own honest states.
        apiFetch(
          `/api/projects/${encodeURIComponent(projectId)}/context/ai-paths/stats`,
          { cache: "no-store" },
        )
          .then(async (res) => {
            if (!res.ok) return;
            const body = (await res.json()) as Partial<AiPathStats>;
            // Shape check: a payload without the two aggregates is not the
            // stats answer, and rendering it would invent a drift reading.
            if (body && body.outcomes && body.verdicts) {
              setPathsStats(body as AiPathStats);
            }
          })
          .catch(() => setPathsStats(null));
        // The usage line, on the same trigger and with the same failure rule:
        // an aggregate that could not be read must never render as "nobody
        // opened anything" — the table was written by two surfaces for weeks
        // and read by nothing, and a zero here would have looked identical.
        apiFetch(
          `/api/projects/${encodeURIComponent(projectId)}/context/ai-paths/inspections/summary`,
          { cache: "no-store" },
        )
          .then(async (res) => {
            if (!res.ok) return;
            const body = (await res.json()) as Partial<AiPathInspectionSummary>;
            // Shape check, like the drift line: a payload without the state
            // split is not the summary answer, and rendering it would invent a
            // usage reading out of whatever answered.
            if (
              body &&
              body.displayed_states &&
              typeof body.inspections_scanned === "number"
            ) {
              setPathsInspections(body as AiPathInspectionSummary);
            }
          })
          .catch(() => setPathsInspections(null));
      })
      .catch(() => {
        setPaths([]);
        setPathsState("unavailable");
      });
  }, [projectId, pathsState]);

  // A project switch invalidates the list: the paths of the previous one must
  // not remain selectable under the new project's graph.
  useEffect(() => {
    setPaths([]);
    setPathsStats(null);
    setPathsInspections(null);
    setPathsState("idle");
    setOverlayPathId("");
  }, [projectId]);

  // ── AI Path overlay: the decoration for the selected path. ───────────────
  useEffect(() => {
    if (!overlayPathId) {
      setOverlay(null);
      setOverlayError(null);
      return;
    }
    let live = true;
    setOverlayError(null);
    apiFetch(
      `/api/projects/${encodeURIComponent(projectId)}/context/ai-paths/` +
        `${encodeURIComponent(overlayPathId)}/graph-overlay`,
      { cache: "no-store" },
    )
      .then(async (res) => {
        if (!live) return;
        if (!res.ok) {
          // Nothing is drawn in its place. A canvas that quietly stayed
          // undecorated would read as "this path deviated from nothing".
          setOverlay(null);
          setOverlayError(
            res.status === 404
              ? "This AI Path is not available in this project."
              : "This overlay could not be read. The graph below is undecorated.",
          );
          return;
        }
        setOverlay((await res.json()) as AiPathOverlay);
      })
      .catch(() => {
        if (!live) return;
        setOverlay(null);
        setOverlayError("This overlay could not be reached. The graph below is undecorated.");
      });
    return () => {
      live = false;
    };
  }, [projectId, overlayPathId]);

  // ── Apply the decoration in place. Positions are kept, exactly like the
  // search highlight above: AC7 says the overlay decorates the canonical
  // graph, and a lens that re-laid the graph out would have moved it.
  useEffect(() => {
    const byKey = new Map(
      (overlay?.nodes ?? []).map((n) => [overlayKey(n.node_type, n.node_id), n]),
    );
    setRfNodes((prev) =>
      prev.map((n) => {
        const next = byKey.get(overlayKey(n.data.node.node_type, n.data.node.id)) ?? null;
        if (n.data.overlay === next) return n;
        return { ...n, data: { ...n.data, overlay: next } };
      }),
    );
  }, [overlay, setRfNodes]);

  const fittedQueryRef = useRef<string | null>(null);
  /** Index of the hit the viewport is currently on, for Up/Down cycling.
   * Declared (and reset) BEFORE the centring effect below so that effect can
   * seed it — effects run in declaration order, and the other way round the
   * reset would immediately undo the seed. */
  const searchHitIndexRef = useRef(-1);
  useEffect(() => {
    searchHitIndexRef.current = -1;
  }, [search, view]);

  // Centring is a response to the QUERY changing, not to the node set changing:
  // with `projected` in the deps every filter toggle yanked the viewport back
  // to the first hit while the operator was trying to narrow the map. The
  // current nodes are therefore read through a ref, not through the closure.
  //
  // `projected` stays in the deps so a first-time fit can still happen when a
  // hit only APPEARS after the operator widens the filters (44.4 re-review);
  // the fittedQueryRef guard is what prevents the anti-yank regression -- once
  // a query has been fitted, later filter toggles are no-ops for the viewport.
  // It resolves hits against the PROJECTION, not the filter set: in Schema or
  // Orphans the first filter-level hit may not be on the canvas at all, and
  // fitting an absent node silently does nothing (44.6 review).
  useEffect(() => {
    if (!rf) return;
    const query = search.trim();
    if (query === "") {
      fittedQueryRef.current = null;
      return;
    }
    if (fittedQueryRef.current === query) return;
    const hits = projectedRef.current.nodes.filter((n) => nodeMatchesQuery(n, search));
    if (hits.length === 0) return;
    fittedQueryRef.current = query;
    searchHitIndexRef.current = 0;
    rf.fitView({ nodes: [{ id: hits[0].id }], duration: 300, maxZoom: 1.2 });
  }, [rf, search, projected]);

  // ── Keyboard: "/" focuses search; Escape is owned here (see below). ──────
  //
  // ONE Escape owner. Several document-level listeners all fire for the same
  // key, so an Escape meant for the edge modal used to ALSO close the drawer
  // underneath it. The surfaces are therefore backed out in strict priority
  // order, innermost first, and exactly one of them handles the press.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null;
      const typing =
        !!target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchInputRef.current?.focus();
        return;
      }
      if (e.key !== "Escape") return;
      // 44.11 re-review: the review modal joins the ONE-Escape-owner chain --
      // without this branch, Escape fell through and closed the drawer UNDER
      // the still-open modal.
      if (reviewModal.status !== "closed") {
        setReviewModal({ status: "closed" });
        return;
      }
      if (edgeModal.status === "open") {
        setEdgeModal({ status: "closed" });
        return;
      }
      if (topicEditor.status !== "closed") {
        setTopicEditor({ status: "closed" });
        return;
      }
      if (contextMenu) {
        setContextMenu(null);
        return;
      }
      if (confirmDeleteEdgeId) {
        setConfirmDeleteEdgeId(null);
        return;
      }
      setSelectedId(null);
      setSelectedEdgeId(null);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [reviewModal.status, edgeModal.status, topicEditor.status, contextMenu, confirmDeleteEdgeId]);

  // ── Keyboard: Up/Down cycles search hits (44.6). Available whenever a query
  // is active and the operator is not typing in some OTHER input — being
  // limited to "search box focused" made it unreachable the moment focus moved
  // to the canvas, which is exactly when cycling is wanted.
  useEffect(() => {
    function onArrowKeyDown(e: KeyboardEvent) {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const target = e.target as HTMLElement | null;
      const typingElsewhere =
        !!target &&
        target !== searchInputRef.current &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          // SELECT too (44.4 re-review): hijacking Up/Down while the Link
          // type / Scope dropdowns have focus would make them unusable by
          // keyboard exactly when a query is active.
          target.tagName === "SELECT" ||
          target.isContentEditable);
      if (typingElsewhere) return;
      const query = search.trim();
      if (!query) return;
      const hits = projected.nodes.filter((n) => nodeMatchesQuery(n, query));
      if (hits.length === 0) return;
      e.preventDefault();
      const dir = e.key === "ArrowDown" ? 1 : -1;
      searchHitIndexRef.current = (searchHitIndexRef.current + dir + hits.length) % hits.length;
      const hit = hits[searchHitIndexRef.current];
      // Keep the anti-yank guard (above) in sync so a later filter toggle does
      // not immediately re-fit back to the first hit and undo the cycle.
      fittedQueryRef.current = query;
      rf?.fitView({ nodes: [{ id: hit.id }], duration: 300, maxZoom: 1.2 });
    }
    document.addEventListener("keydown", onArrowKeyDown);
    return () => document.removeEventListener("keydown", onArrowKeyDown);
  }, [search, projected, rf]);

  // ── Drawer: fetch the FULL body the bundle intentionally does not carry. ─
  const selectedNode = selectedId ? (nodeById.get(selectedId) ?? null) : null;

  useEffect(() => {
    if (!selectedNode) {
      setDetail({ status: "idle" });
      return;
    }
    if (
      selectedNode.node_type === "schema_doc" ||
      selectedNode.node_type === "business_domain" ||
      selectedNode.node_type === "business_classification" ||
      selectedNode.node_type === "report_view" ||
      selectedNode.node_type === "datastream" ||
      selectedNode.node_type === "semantic_view" ||
      selectedNode.node_type === "semantic_concept"
    ) {
      setDetail({ status: "excerpt_only" });
      return;
    }
    if (selectedNode.node_type === "target_field") {
      // Nothing more to fetch: the bundle already carries the whole summary.
      setDetail({ status: "field_summary" });
      return;
    }
    const path =
      selectedNode.node_type === "topic"
        ? `/api/context/topics/${encodeURIComponent(selectedNode.id)}?project_id=${encodeURIComponent(projectId)}`
        : `/api/context/procedures/${encodeURIComponent(selectedNode.id)}?project_id=${encodeURIComponent(projectId)}`;
    let cancelled = false;
    setDetail({ status: "loading" });
    void (async () => {
      try {
        const res = await apiFetch(path);
        if (cancelled) return;
        if (!res.ok) {
          setDetail({ status: "error", message: await readErrorMessage(res) });
          return;
        }
        const body = (await res.json()) as {
          body_md?: string;
          updated_at?: string | null;
          frontmatter_yaml?: string;
        };
        if (cancelled) return;
        setDetail({
          status: "ok",
          body_md: body.body_md ?? "",
          updated_at: body.updated_at ?? null,
          // Une Skill annonce sa sequence standardisee dans le drawer -- le
          // bouton d'ouverture peut alors dire ce qu'on va lire.
          step_count:
            selectedNode.node_type === "procedure" && body.frontmatter_yaml
              ? parseSkillSteps(body.frontmatter_yaml).length
              : null,
        });
      } catch (err) {
        if (cancelled) return;
        setDetail({
          status: "error",
          message: err instanceof Error ? err.message : "Network error",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedNode]);

  // ── Story 44.10: "Fed by" — the datastream mappings that actually land in
  // the selected dictionary field. Fetched only for target_field nodes, and
  // scoped to this project so the panel answers "what feeds it HERE" rather
  // than listing another project's streams.
  const [fedBy, setFedBy] = useState<FedByState>({ status: "idle" });

  useEffect(() => {
    if (!selectedNode || selectedNode.node_type !== "target_field") {
      setFedBy({ status: "idle" });
      return;
    }
    let cancelled = false;
    setFedBy({ status: "loading" });
    void (async () => {
      try {
        const res = await apiFetch(
          `/api/datamodel/mappings?target_field=${encodeURIComponent(selectedNode.id)}` +
            `&project_id=${encodeURIComponent(projectId)}`,
        );
        if (cancelled) return;
        if (!res.ok) {
          setFedBy({ status: "error", message: await readErrorMessage(res) });
          return;
        }
        const body = (await res.json()) as { mappings?: FedByRow[] };
        if (cancelled) return;
        setFedBy({ status: "ok", rows: body.mappings ?? [] });
      } catch (err) {
        if (cancelled) return;
        setFedBy({
          status: "error",
          message: err instanceof Error ? err.message : "Network error",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
    // Stable primitives, not the derived node object (44.10 re-review): a
    // bundle refetch mints a new object identity for the same field and would
    // re-fire this network read on every graph mutation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedNode?.node_type, selectedNode?.id, projectId]);

  // ── AI-158 : les remarques ouvertes SUR CE NOEUD.
  //
  // Le panneau vivait ICI et nulle part ailleurs, alors qu'on ne travaille
  // pas une Skill dans un mindmap : il est parti dans `NodeRemarks`, monte
  // aussi par l'etabli du Skill. `reviewReloadToken` existe parce que c'est
  // CET ecran qui remplit la file, depuis sa propre modale.
  const [reviewReloadToken, setReviewReloadToken] = useState(0);
  const focusNode = useCallback(
    (id: string) => {
      setSelectedId(id);
      rf?.fitView({ nodes: [{ id }], duration: 300, maxZoom: 1.2 });
    },
    [rf],
  );

  // ── Story 44.5: edge creation, deletion, drawer edit, canvas context menu ─

  /** Drag a handle to another handle -> open the edge_type modal. Never adds
   * an edge before the server confirms it (AC4: no phantom edges/nodes). */
  const onConnect = useCallback(
    (connection: Connection) => {
      const resolved = resolveConnection(connection, nodeById);
      if (!resolved) return;
      setEdgeModal({
        status: "open",
        fromId: resolved.fromNode.id,
        toId: resolved.toNode.id,
        fromType: resolved.fromNode.node_type,
        toType: resolved.toNode.node_type,
        edgeType: "",
        saving: false,
        error: null,
      });
    },
    [nodeById],
  );

  async function submitEdgeModal() {
    if (edgeModal.status !== "open") return;
    const edgeType = edgeModal.edgeType.trim();
    if (!edgeType) return;
    const fromNode = nodeById.get(edgeModal.fromId);
    const toNode = nodeById.get(edgeModal.toId);
    if (!fromNode || !toNode) {
      setEdgeModal({ status: "closed" });
      return;
    }
    setEdgeModal((prev) => (prev.status === "open" ? { ...prev, saving: true, error: null } : prev));
    try {
      // The scope rides the query string, like every other context call. The
      // body still repeats it (the server accepts a duplicate that agrees), so
      // the payload contract of Story 44.5 is unchanged.
      const res = await apiFetch(
        `/api/context/graph/edges?project_id=${encodeURIComponent(projectId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(buildEdgeCreatePayload(projectId, fromNode, toNode, edgeType)),
        },
      );
      if (!res.ok) {
        // A 404/403 here is the project gate, not a node-scope problem: the
        // server returns 404 when the project is not visible to the caller or
        // the caller's role cannot write. A node-scope refusal comes back as
        // 422 and carries its own message, which is kept verbatim.
        const message = isPermissionDenied(res.status)
          ? "Not permitted: you cannot write links in this project."
          : await readErrorMessage(res);
        setEdgeModal((prev) =>
          prev.status === "open" ? { ...prev, saving: false, error: message } : prev,
        );
        return;
      }
      const created = (await res.json()) as GraphEdgeRow;
      // Append to the ONE bundle already in memory — no refetch (44.4 rule
      // extends to writes: filters/layout pick this up on their own).
      setState((prev) =>
        prev.status === "ok"
          ? { status: "ok", bundle: { nodes: prev.bundle.nodes, edges: [...prev.bundle.edges, created] } }
          : prev,
      );
      setEdgeModal({ status: "closed" });
    } catch (err) {
      setEdgeModal((prev) =>
        prev.status === "open"
          ? { ...prev, saving: false, error: err instanceof Error ? err.message : "Network error" }
          : prev,
      );
    }
  }

  /** Shared by the keyboard-Delete flow and every drawer edge-row delete button. */
  async function handleDeleteEdge(edgeId: string) {
    setDeletingEdgeId(edgeId);
    setEdgeActionError((prev) => (prev?.id === edgeId ? null : prev));
    try {
      const res = await apiFetch(`/api/context/graph/edges/${encodeURIComponent(edgeId)}`, {
        method: "DELETE",
      });
      if (!res.ok && res.status !== 204) {
        const message = isPermissionDenied(res.status)
          ? "Not permitted: this link could not be removed."
          : await readErrorMessage(res);
        setEdgeActionError({ id: edgeId, message });
        setDeletingEdgeId(null);
        setConfirmDeleteEdgeId(null);
        return;
      }
      setState((prev) =>
        prev.status === "ok"
          ? {
              status: "ok",
              bundle: { nodes: prev.bundle.nodes, edges: prev.bundle.edges.filter((e) => e.id !== edgeId) },
            }
          : prev,
      );
      setSelectedEdgeId((cur) => (cur === edgeId ? null : cur));
      setConfirmDeleteEdgeId(null);
      setDeletingEdgeId(null);
    } catch (err) {
      setEdgeActionError({
        id: edgeId,
        message: err instanceof Error ? err.message : "Network error",
      });
      setDeletingEdgeId(null);
    }
  }

  /** Right-click canvas -> "New topic here" -> 44.1 create modal -> insert + relayout
   * (relayout happens for free: the new node flows through allNodes -> visible -> projected). */
  async function submitTopicEditor() {
    if (topicEditor.status === "closed") return;
    const title = topicEditor.title.trim();
    if (!title) return;
    const isEdit = topicEditor.status === "edit";
    // Empty owner text means "no explicit owner" (Story 44.11) -- sent as
    // null so the server clears/leaves-unset rather than storing "".
    const owner = topicEditor.owner.trim() || null;
    setTopicEditor((prev) => (prev.status === "closed" ? prev : { ...prev, saving: true, error: null }));
    try {
      const res =
        topicEditor.status === "edit"
          ? await apiFetch(`/api/context/topics/${encodeURIComponent(topicEditor.nodeId)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ title, body_md: topicEditor.body_md, owner }),
            })
          : await apiFetch("/api/context/topics", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                project_id: projectId,
                title,
                body_md: topicEditor.body_md,
                owner,
              }),
            });
      if (!res.ok) {
        const message = isPermissionDenied(res.status)
          ? "Not permitted."
          : await readErrorMessage(res);
        setTopicEditor((prev) =>
          prev.status === "closed" ? prev : { ...prev, saving: false, error: message },
        );
        return;
      }
      const saved = (await res.json()) as {
        id: string;
        title: string;
        body_md: string;
        project_id: string | null;
        owner?: string | null;
        created_by?: string;
        version_number?: number;
        status?: string;
        updated_at?: string | null;
      };
      // Same resolution rule as the server's graph bundle (context_api.py
      // _resolve_owner): explicit owner, else created_by, else "auto".
      const resolvedOwner = saved.owner || saved.created_by || "auto";
      if (isEdit) {
        setState((prev) => {
          if (prev.status !== "ok") return prev;
          return {
            status: "ok",
            bundle: {
              nodes: prev.bundle.nodes.map((n) =>
                n.id === saved.id
                  ? {
                      ...n,
                      title: saved.title,
                      excerpt: (saved.body_md ?? "").slice(0, 280),
                      owner: resolvedOwner,
                      owner_raw: saved.owner ?? null,
                      version_number: saved.version_number ?? n.version_number,
                    }
                  : n,
              ),
              edges: prev.bundle.edges,
            },
          };
        });
        // Bumped version shows without a full page reload (AC3): the PATCH
        // response IS the fresh row, so patch the drawer's own detail state
        // from it directly rather than firing a second GET for the same data.
        // `updated_at` moves with the body — leaving it behind would show the
        // new text under the previous save's date.
        setDetail((prev) =>
          prev.status === "ok"
            ? {
                ...prev,
                body_md: saved.body_md ?? prev.body_md,
                updated_at: saved.updated_at ?? prev.updated_at,
              }
            : prev,
        );
      } else {
        const newNode: GraphNodeRow = {
          id: saved.id,
          node_type: "topic",
          title: saved.title,
          excerpt: (saved.body_md ?? "").slice(0, 280),
          owner: resolvedOwner,
          owner_raw: saved.owner ?? null,
          version_number: saved.version_number ?? 1,
          scope: saved.project_id === null ? "platform" : "project",
          status: saved.status ?? "active",
        };
        setState((prev) =>
          prev.status === "ok"
            ? { status: "ok", bundle: { nodes: [...prev.bundle.nodes, newNode], edges: prev.bundle.edges } }
            : prev,
        );
      }
      setTopicEditor({ status: "closed" });
    } catch (err) {
      setTopicEditor((prev) =>
        prev.status === "closed"
          ? prev
          : { ...prev, saving: false, error: err instanceof Error ? err.message : "Network error" },
      );
    }
  }

  /** Story 44.11 + AI-157: "Request review" -- la remarque part en FILE, a la
   * version du noeud, et une ligne d'audit la double. Jusqu'au 2026-08-04 seule
   * la ligne d'audit existait : un journal append-only et scelle, dont rien ne
   * ressort. Toujours aucune notification -- la copie ne pretend pas le
   * contraire.
   *
   * `project_id` est desormais dans l'URL, comme pour toute la famille des
   * routes de contexte : c'est la seule source de l'organisation, et un noeud
   * de plateforme n'en porte aucune. La note est OBLIGATOIRE : une demande de
   * relecture qui ne dit rien n'est pas une remarque, c'est un clic. */
  async function submitReviewRequest() {
    if (reviewModal.status !== "open") return;
    if (!reviewModal.note.trim()) return;
    setReviewModal((prev) => (prev.status === "open" ? { ...prev, saving: true, error: null } : prev));
    try {
      const res = await apiFetch(
        `/api/context/nodes/${encodeURIComponent(reviewModal.nodeId)}/request-review` +
          `?project_id=${encodeURIComponent(projectId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            node_type: reviewModal.nodeType,
            note: reviewModal.note.trim(),
          }),
        },
      );
      if (!res.ok) {
        // English headline mapped from the server code; the verbatim (French)
        // server envelope stays available as a technical detail suffix
        // (44.11 re-review: English UI must not lead with French copy).
        const serverMessage = await readErrorMessage(res);
        const message = isPermissionDenied(res.status)
          ? "Not permitted."
          : res.status === 422
            ? `The request was refused as invalid. (${serverMessage})`
            : `The review request could not be recorded. (${serverMessage})`;
        setReviewModal((prev) =>
          prev.status === "open" ? { ...prev, saving: false, error: message } : prev,
        );
        return;
      }
      setReviewModal({ status: "success" });
      // AI-158 : la remarque vient d'entrer en file -- le panneau du tiroir doit
      // la montrer. Sans cette relecture, l'ecran affirmerait « aucune remarque
      // ouverte » juste apres en avoir accepte une.
      setReviewReloadToken((n) => n + 1);
    } catch (err) {
      setReviewModal((prev) =>
        prev.status === "open"
          ? { ...prev, saving: false, error: err instanceof Error ? err.message : "Network error" }
          : prev,
      );
    }
  }

  // Delete key on a selected edge asks for confirmation once, then deletes on
  // the second press — mirrors the drawer's row-level confirm (AC2). The
  // question and any refusal are rendered on the CANVAS (`kg-edge-confirm-bar`
  // below), because this path can be driven with the drawer closed. Escape is
  // NOT handled here: the single Escape owner above disarms the confirm.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null;
      const typing =
        !!target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (typing || !selectedEdgeId) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        // Story 45.3: a derived hierarchy edge or an authoritative business link
        // is inspectable but not deletable here. Its owner is Context Hub, and
        // the Context Graph endpoint has no row to delete for it.
        if (businessManagedEdgeIds.has(selectedEdgeId)) {
          setEdgeActionError({
            id: selectedEdgeId,
            message: "This link is governed by Context Hub. Manage it there, not on the canvas.",
          });
          return;
        }
        if (confirmDeleteEdgeId === selectedEdgeId) {
          void handleDeleteEdge(selectedEdgeId);
        } else {
          setConfirmDeleteEdgeId(selectedEdgeId);
        }
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedEdgeId, confirmDeleteEdgeId]);

  const inbound = useMemo(
    () => (selectedId ? allEdges.filter((e) => e.to_id === selectedId) : []),
    [allEdges, selectedId],
  );
  const outbound = useMemo(
    () => (selectedId ? allEdges.filter((e) => e.from_id === selectedId) : []),
    [allEdges, selectedId],
  );
  /**
   * Edges Context Hub owns: the derived `contains` hierarchy and the
   * authoritative business links. They touch a business node at one end, which
   * is exactly what makes them non-deletable from this canvas.
   */
  const businessManagedEdgeIds = useMemo(() => {
    const businessNodeIds = new Set(
      allNodes.filter((node) => BUSINESS_NODE_TYPES.has(node.node_type)).map((node) => node.id),
    );
    return new Set(
      allEdges
        .filter((edge) => businessNodeIds.has(edge.from_id) || businessNodeIds.has(edge.to_id))
        .map((edge) => edge.id),
    );
  }, [allNodes, allEdges]);

  /** The edge the canvas confirm bar talks about. */
  const selectedEdge = useMemo(
    () => (selectedEdgeId ? (allEdges.find((e) => e.id === selectedEdgeId) ?? null) : null),
    [allEdges, selectedEdgeId],
  );

  /** A drawer edge row points at a peer that may not be on the canvas. Say
   * WHY: dropped by the filters, or simply not part of this view. */
  function hiddenPeerLabel(peerId: string): string | null {
    if (projectedIds.has(peerId)) return null;
    return visibleIds.has(peerId) ? "Hidden in this view" : "Hidden by current filters";
  }

  /**
   * THE PEER'S TITLE, OR WHAT ITS ABSENCE MEANS — never `kgn_<ULID>`.
   *
   * Ten lines of this page read `nodeById.get(id)?.title ?? id`, which is the
   * console resolving a name and, failing, printing the address as prose: the
   * exact clause `visualization-and-rendering.md` calls incomplete ("or if a
   * label is composed in the browser"). The identifier said nothing a person
   * could act on, and it said it in the two places that matter most — the
   * sentence asking to confirm a deletion, and the modal naming the two nodes
   * about to be linked.
   *
   * `nodeById` is built from `allNodes`, the WHOLE bundle, not from what the
   * filters or the current view keep — `hiddenPeerLabel` answers that other
   * question separately, and the two are not the same absence. So a miss here
   * is not "filtered out": it is an edge whose peer the bundle does not carry,
   * which is what this sentence says.
   */
  function peerTitle(peerId: string): string {
    return nodeById.get(peerId)?.title ?? "Outside this graph";
  }

  const edgeTypes = useMemo(() => edgeTypeOptions(allEdges), [allEdges]);
  /**
   * "Nothing here yet" means the PROJECT has no graph — never "the filters kept
   * nothing". The two readings are different states with different ways out, and
   * the second one is `noMatch` below, which names the filters and offers to
   * reset them. Saying "add a first knowledge entry" to someone whose nodes are
   * merely filtered out would be false.
   */
  const isEmpty = state.status === "ok" && allNodes.length === 0;
  /**
   * A project of its own that holds nothing, on a platform that holds
   * something. MEASURED 2026-08-07: `kg-empty` was UNREACHABLE on any install
   * that ran `scripts/seed_context_platform_defaults.py`. The bundle is
   * AD-5-scoped -- a project sees platform rows AND its own -- so `allNodes`
   * is never zero once platform defaults exist, and a brand-new project fell
   * straight through to a canvas of entries nobody on that project wrote.
   *
   * Both readings of "empty" were wrong there. The panel below claims "no
   * placeholder nodes are drawn" while five appear; and simply loosening
   * `isEmpty` to ignore platform rows would hide a graph the project can
   * genuinely read, which is worse -- those entries ARE governed context and
   * analysis will use them.
   *
   * So the state is not widened, it is SPLIT. `isEmpty` keeps meaning "nothing
   * at all". This one means "nothing of this project's own", says how many
   * platform entries are readable, and leads to the same first action.
   */
  const platformOnly =
    state.status === "ok"
      && allNodes.length > 0
      && allNodes.every((node) => node.scope === "platform");
  const isLarge = allNodes.length > LARGE_GRAPH_THRESHOLD;

  /**
   * What the overlay knows and the canvas is NOT showing.
   *
   * Three ways a decoration can be a lie by omission, and all three are stated
   * rather than absorbed:
   *
   *   1. the read failed — the graph below is undecorated, which is not the
   *      same as a path that deviated from nothing;
   *   2. the path walked steps that reach no drawable node (a bare tool call, a
   *      version) — counted by the owner, never invented as a node here;
   *   3. a decorated node is not on this canvas — because the current type /
   *      link / scope filters exclude it, or because the mindmap has no node
   *      for it at all. A `missing` node in that state is the deviation the
   *      reader came for, and dropping it silently hides exactly that.
   */
  const overlayNotice = useMemo(() => {
    if (overlayError) {
      return { tone: "warning", title: "Overlay unavailable", body: overlayError };
    }
    if (!overlay) return null;

    const offCanvas = overlay.nodes.filter((n) => {
      const row = allNodes.find(
        (candidate) => candidate.id === n.node_id && candidate.node_type === n.node_type,
      );
      return !row || !projectedIds.has(row.id);
    });
    const undrawnMissing = offCanvas.filter((n) => n.state === "missing").length;

    const parts: string[] = [];
    if (overlay.lifecycle === "recording") {
      parts.push("This path is still recording, so it is not settled evidence yet.");
    }
    if (overlay.verdict) parts.push(`Assessment: ${overlay.verdict}.`);
    if (overlay.unrepresented_steps > 0) {
      parts.push(
        `${overlay.unrepresented_steps} step(s) reached nothing this map can draw; ` +
          "they stay visible in the path itself.",
      );
    }
    if (offCanvas.length > 0) {
      parts.push(
        `${offCanvas.length} decorated node(s) are not on this canvas` +
          (undrawnMissing > 0
            ? `, including ${undrawnMissing} the policy REQUIRED and no step reached.`
            : " — widen the filters to see them."),
      );
    }
    if (parts.length === 0) return null;
    return {
      tone: undrawnMissing > 0 || overlay.verdict === "fail" ? "warning" : "info",
      title: `AI Path ${overlay.path_id}`,
      body: parts.join(" "),
    };
  }, [overlay, overlayError, allNodes, projectedIds]);
  /** There ARE nodes, the filters just exclude every one of them. */
  const noMatch = state.status === "ok" && allNodes.length > 0 && visible.nodes.length === 0;

  function toggleType(type: NodeTypeKey) {
    setFilters((prev) => ({
      ...prev,
      types: { ...prev.types, [type]: !prev.types[type] },
    }));
  }

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Knowledge graph</h1>
          <p>
            Everything analysis can read for this project — governed Knowledge, Skills and
            generated schema docs, with the links between them. Excerpts are verbatim, never
            summarised.
          </p>
        </div>
        {onOpenKnowledge && (
          <div className="flex gap-2.5">
            <button className="secondary-button" type="button" onClick={onOpenKnowledge}>
              Open Knowledge
            </button>
          </div>
        )}
      </div>

      {state.status === "loading" && (
        <p className="kg-status" role="status">
          Loading the knowledge graph…
        </p>
      )}

      {state.status === "error" && (
        <div className="kg-load-error" role="alert" data-testid="kg-error">
          <span className="signal-label error">
            <span className="signal-mark" />
            Couldn&rsquo;t load the knowledge graph
          </span>
          {/* The page speaks English; the server envelope is kept verbatim but
              demoted to a technical detail rather than used as the headline. */}
          <p>
            The graph endpoint didn&rsquo;t return this project&rsquo;s knowledge, so nothing
            is drawn — no partial map is shown. Retry, or check the detail below.
          </p>
          <p className="kg-technical" data-testid="kg-error-detail">
            Server response: {state.message}
          </p>
          <button className="secondary-button" type="button" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {/* Reuses `.kg-empty` rather than minting a class: the two states are the
          same shape of notice, and the CSS ratchet (`finished_work_audit.py
          --gate`) is right to refuse a second copy of six rules for one word of
          difference. */}
      {platformOnly && (
        <section className="panel kg-empty" data-testid="kg-platform-only" role="status">
          <h2>Nothing of this project&rsquo;s own yet</h2>
          <p>
            The {allNodes.length} entr{allNodes.length === 1 ? "y" : "ies"} on the canvas below
            {" "}
            {allNodes.length === 1 ? "is" : "are"} platform-wide — governed context this project
            can read but did not write. Analysis will use {allNodes.length === 1 ? "it" : "them"}.
            Add a first entry of your own and it appears beside {allNodes.length === 1 ? "it" : "them"}.
          </p>
        </section>
      )}

      {isEmpty && (
        <section className="panel kg-empty" data-testid="kg-empty">
          <EmptyState
            title="Nothing in this project&rsquo;s graph yet"
            description="The graph draws the Knowledge, Skills and schema docs analysis can read. It appears here as soon as a first knowledge entry exists — no placeholder nodes are drawn."
          />
          {/* The canvas (and with it the right-click "New topic here") only
              exists once there IS a node, so an empty project needs its own way
              in — otherwise the one action that fixes the empty state is the
              one action the empty state cannot reach. */}
          <div className="kg-empty-actions">
            <button
              className="primary-button"
              type="button"
              data-testid="kg-empty-new-topic"
              onClick={() =>
                setTopicEditor({
                  status: "create",
                  title: "",
                  body_md: "",
                  owner: "",
                  saving: false,
                  error: null,
                })
              }
            >
              New topic
            </button>
            {onOpenKnowledge && (
              <button className="secondary-button" type="button" onClick={onOpenKnowledge}>
                Add knowledge
              </button>
            )}
          </div>
        </section>
      )}

      {state.status === "ok" && allNodes.length > 0 && (
        <>
          {isLarge && (
            <div className="kg-warning" role="status" data-testid="kg-large-warning">
              <span className="signal-label warning">
                <span className="signal-mark" />
                Large graph
              </span>
              <p>
                This project has <span className="number">{allNodes.length}</span> nodes. The
                map still renders, but narrowing by type, link or scope makes it readable.
              </p>
            </div>
          )}

          <div className="kg-view-row">
            <GraphViewSwitcher view={view} onChange={handleViewChange} />
            <span className="kg-projection-count number" data-testid="kg-projection-count">
              {projected.nodes.length} shown
            </span>
            <button
              className="secondary-button kg-tidy-button"
              type="button"
              data-testid="kg-tidy"
              onClick={handleTidy}
            >
              Tidy
            </button>
            {/* A LENS, not a filter — which is why it sits with the view
                switcher and not in "Graph filters" below. It hides nothing and
                selects nothing; it decorates what is already drawn (AC7). */}
            <label className="kg-field kg-field--path">
              <span>AI Path</span>
              <select
                data-testid="kg-ai-path"
                value={overlayPathId}
                // Never `disabled`: a disabled control cannot be focused, and
                // focus is what loads the list. It also has to stay reachable
                // to REPORT that the history could not be read.
                onFocus={ensurePaths}
                onMouseDown={ensurePaths}
                onChange={(e) => setOverlayPathId(e.target.value)}
              >
                <option value="">
                  {pathsState === "unavailable"
                    ? "AI Paths unavailable"
                    : pathsState === "loaded" && paths.length === 0
                      ? pathsFailures > 0
                        // Never "none recorded" when the server states it
                        // FAILED to record: that sentence was false for a whole
                        // day while the recorder refused every context walk.
                        ? "Recording is failing — this is not an empty history"
                        : "No AI Path recorded yet"
                      : "No overlay"}
                </option>
                {paths.map((p) => (
                  <option key={p.id} value={p.id}>
                    {aiPathOptionLabel(p)}
                  </option>
                ))}
              </select>
              {pathsState === "loaded" && pathsStats && (
                <small className="kg-note" data-testid="kg-ai-path-stats">
                  {formatAiPathStats(pathsStats)}
                </small>
              )}
              {/* Who actually opened the evidence, beside how the walks fared.
                  Same failure rule as the drift line above: absent when it could
                  not be read, because "nobody opened anything" and "we could not
                  count" are two facts and only one of them is actionable. */}
              {pathsState === "loaded" && pathsInspections && (
                <small className="kg-note" data-testid="kg-ai-path-inspections">
                  {formatInspectionSummary(pathsInspections)}
                </small>
              )}
            </label>
            {/* THE DOOR. The overlay decorates the canvas; the path's own steps,
                its branch evidence and its assessment are on `AiPathPage`, and
                until now the only way there was to type the address. The
                control sits OUTSIDE the label on purpose: a button inside a
                `<label>` would activate the select it wraps.

                It appears with a selected path rather than sitting disabled —
                the affordance belongs to the path the reader chose, and a
                permanently greyed button teaches nothing about what enables
                it. */}
            {onOpenAiPath && overlayPathId && (
              <button
                className="secondary-button"
                type="button"
                data-testid="kg-ai-path-open"
                aria-label={`Open AI Path ${overlayPathId}`}
                onClick={() => onOpenAiPath(overlayPathId)}
              >
                Open this path
              </button>
            )}
          </div>

          {/* Everything the overlay could not put on the canvas. Reported,
              because the alternative is a decorated graph that silently means
              "this is all of it" -- and a required node the mindmap cannot
              draw is exactly the deviation a reader came here to find. */}
          {overlayNotice && (
            <div className="kg-warning" role="status" data-testid="kg-overlay-notice">
              <span className={`signal-label ${overlayNotice.tone}`}>
                <span className="signal-mark" />
                {overlayNotice.title}
              </span>
              <p>{overlayNotice.body}</p>
            </div>
          )}

          {/* Story 49.6 AC9 — the Events this path went through, BY REFERENCE.
              Beside the canvas and not on it: the graph vocabulary carries no
              Event, and `data.md` keeps it owned by its Datastream. There is no
              create, edit or delete here, and nothing of the Event's content:
              its mapping, payload, samples and run evidence stay where they
              are owned, one link away. */}
          {overlay && overlay.event_references.length > 0 && (
            <div className="kg-warning" data-testid="kg-overlay-events">
              <span className="signal-label info">
                <span className="signal-mark" />
                Events on this path
              </span>
              <ul className="kg-event-refs">
                {overlay.event_references.map((ref) => (
                  <li key={ref.event_id} data-testid={`kg-event-ref-${ref.event_id}`}>
                    {ref.binding_state === "linked" && ref.owner_route ? (
                      <>
                        {onOpenEvent ? (
                          <button
                            type="button"
                            className="secondary-button"
                            data-testid={`kg-event-open-${ref.event_id}`}
                            onClick={() => onOpenEvent(ref.owner_route as OwnerReference)}
                          >
                            {ref.event_type ?? "Event"}
                            {ref.event_date ? ` · ${ref.event_date}` : ""}
                          </button>
                        ) : (
                          <span>
                            {ref.event_type ?? "Event"}
                            {ref.event_date ? ` · ${ref.event_date}` : ""}
                          </span>
                        )}{" "}
                        <span className="kg-node-owner">
                          owned by its Datastream
                          {ref.version_number ? `, v${ref.version_number}` : ""}
                        </span>
                      </>
                    ) : (
                      // Non-disclosing on purpose: an Event of another Project,
                      // one that was deleted, and one whose Datastream binding
                      // was never proven all read the same. Saying which would
                      // turn this list into a way to ask whether an Event exists.
                      <span data-testid={`kg-event-unavailable-${ref.event_id}`}>
                        An Event on this path could not be resolved to its owner
                        — shown as unavailable rather than linked to a Datastream
                        nobody proved owns it.
                      </span>
                    )}{" "}
                    {ref.ordinals.length > 0 ? (
                      <span className="kg-node-version">step {ref.ordinals.join(", ")}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="kg-toolbar" role="group" aria-label="Graph filters">
            <div className="kg-toggles">
              {NODE_TYPE_ORDER.map((type) => (
                <button
                  key={type}
                  type="button"
                  className={`kg-toggle kg-toggle--${type}${filters.types[type] ? " is-on" : ""}`}
                  aria-pressed={filters.types[type]}
                  data-testid={`kg-toggle-${type}`}
                  onClick={() => toggleType(type)}
                >
                  <span className="kg-toggle-dot" />
                  {NODE_TYPE_LABELS[type]}
                </button>
              ))}
            </div>

            <label className="kg-field">
              <span>Link type</span>
              <select
                data-testid="kg-edge-type"
                value={filters.edgeType}
                onChange={(e) => setFilters((p) => ({ ...p, edgeType: e.target.value }))}
              >
                <option value="all">All links</option>
                {edgeTypes.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>

            <label className="kg-field">
              <span>Scope</span>
              <select
                data-testid="kg-scope"
                value={filters.scope}
                onChange={(e) =>
                  setFilters((p) => ({
                    ...p,
                    scope: e.target.value as GraphFilterState["scope"],
                  }))
                }
              >
                <option value="all">All scopes</option>
                <option value="platform">Platform</option>
                <option value="project">Project</option>
              </select>
            </label>

            <label className="kg-field kg-field--search">
              <span>Search</span>
              <input
                ref={searchInputRef}
                type="search"
                data-testid="kg-search"
                placeholder="Title or excerpt — press / to focus"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>

            <span className="kg-count number" data-testid="kg-count">
              {visible.nodes.length}/{allNodes.length} nodes · {visible.edges.length} links
            </span>
          </div>

          {layoutError && (
            <div className="kg-load-error" role="alert" data-testid="kg-layout-error">
              <span className="signal-label error">
                <span className="signal-mark" />
                Couldn&rsquo;t lay the graph out
              </span>
              <p>{layoutError}</p>
            </div>
          )}

          <div className={`kg-stage${selectedNode ? " has-drawer" : ""}`}>
            <div className="panel kg-canvas" data-testid="kg-canvas">
              <ReactFlow<KgFlowNode, Edge>
                nodes={rfNodes}
                edges={rfEdges}
                nodeTypes={NODE_TYPES}
                onlyRenderVisibleElements={true}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onInit={handleFlowInit}
                onNodeClick={(_event, node) => setSelectedId(node.id)}
                onEdgeClick={(_event, edge) => {
                  setSelectedEdgeId(edge.id);
                  // Selecting a DIFFERENT edge disarms the previous one's
                  // confirm: otherwise a single Delete on the new edge would
                  // land on an already-armed state and delete without asking.
                  setConfirmDeleteEdgeId((current) => (current === edge.id ? current : null));
                  setEdgeActionError((current) => (current?.id === edge.id ? current : null));
                }}
                onConnect={onConnect}
                onPaneClick={() => {
                  setSelectedId(null);
                  setSelectedEdgeId(null);
                  // The confirm and its error belong to the selection that just
                  // went away. Leaving the confirm armed let a re-click plus a
                  // single Delete skip the confirmation step entirely.
                  setConfirmDeleteEdgeId(null);
                  setEdgeActionError(null);
                  setContextMenu(null);
                }}
                onPaneContextMenu={(event) => {
                  event.preventDefault();
                  setContextMenu({ x: event.clientX, y: event.clientY });
                }}
                nodesConnectable
                /* React Flow's own Delete/Backspace shortcut removes nodes and
                   edges from the LOCAL store with no server call, leaving a
                   canvas that silently disagrees with the database until the
                   next reload. Deletion goes through our confirm + DELETE path
                   or it does not happen at all. */
                deleteKeyCode={null}
                fitView
                minZoom={0.15}
                proOptions={{ hideAttribution: false }}
              >
                <Background gap={22} size={1} />
                <Controls showInteractive={false} />
                <MiniMap className="kg-minimap" pannable zoomable />
              </ReactFlow>

              {/* Filters can hide everything; an empty canvas would otherwise be
                  indistinguishable from a render that failed. */}
              {noMatch && (
                <div className="kg-no-match" role="status" data-testid="kg-no-match">
                  <h3>No node matches these filters</h3>
                  <p>
                    This project has <span className="number">{allNodes.length}</span> nodes —
                    the current type, link and scope filters exclude all of them.
                  </p>
                  <button
                    className="secondary-button"
                    type="button"
                    data-testid="kg-reset-filters"
                    onClick={() => setFilters(DEFAULT_FILTERS)}
                  >
                    Reset filters
                  </button>
                </div>
              )}

              {/* Story 45.3: an empty business scope shows a directed empty
                  state, never unrelated nodes. The way out is naming the scope
                  that is hiding them, not "reset filters" — the filters are not
                  what emptied the canvas. */}
              {businessScope?.selected && scoped.nodes.length === 0 && allNodes.length > 0 && (
                <div className="kg-no-match" role="status" data-testid="kg-business-scope-empty">
                  <EmptyState
                    title="Nothing is linked to this business scope yet"
                    description={`This project has ${allNodes.length} governed node(s); none of them belong to the selected domain or layer, or to anything beneath it.`}
                  />
                  {businessScope.onSelect && (
                    <button
                      className="secondary-button"
                      type="button"
                      data-testid="kg-business-scope-clear"
                      onClick={() => businessScope.onSelect?.(null)}
                    >
                      All business
                    </button>
                  )}
                </div>
              )}

              {/* The current view can legitimately show nothing (e.g. Schema
                  with no schema docs, Orphans with none) even though the filters
                  above have matching nodes — say so instead of a blank canvas. */}
              {!noMatch && projected.nodes.length === 0 && visible.nodes.length > 0 && (
                <div className="kg-no-match" role="status" data-testid="kg-view-empty">
                  <h3>Nothing to show in {GRAPH_VIEW_LABELS[view]}</h3>
                  <p>
                    The current filters keep <span className="number">{visible.nodes.length}</span>{" "}
                    node(s), but none of them belong in this view.
                  </p>
                </div>
              )}

              {/* Canvas-level confirm. The Delete-key path can be driven with
                  no drawer open, so the question it is asking — and a refusal
                  coming back from the server — must be visible on the canvas
                  itself; the drawer rows are not the only place edges die. */}
              {selectedEdge &&
                (confirmDeleteEdgeId === selectedEdge.id ||
                  edgeActionError?.id === selectedEdge.id) && (
                  <div
                    className="kg-edge-confirm-bar"
                    role="alert"
                    data-testid="kg-edge-confirm-bar"
                  >
                    {confirmDeleteEdgeId === selectedEdge.id && (
                      <p className="kg-edge-confirm-question">
                        Remove link “{selectedEdge.edge_type}” between{" "}
                        {peerTitle(selectedEdge.from_id)} and{" "}
                        {peerTitle(selectedEdge.to_id)}?{" "}
                        {deletingEdgeId === selectedEdge.id
                          ? "Removing…"
                          : "Press Delete again to confirm · Esc to cancel"}
                      </p>
                    )}
                    {edgeActionError?.id === selectedEdge.id &&
                      // Suppress when the open drawer already announces this
                      // same refusal on its matching Links row — two identical
                      // role="alert" regions would be read twice (44.4
                      // re-review LOW). The drawer lists exactly the edges
                      // touching the selected node.
                      !(
                        selectedId &&
                        (selectedEdge.from_id === selectedId || selectedEdge.to_id === selectedId)
                      ) && (
                        <p
                          className="kg-inline-error"
                          data-testid="kg-edge-confirm-bar-error"
                        >
                          {edgeActionError.message}
                        </p>
                      )}
                  </div>
                )}

              {contextMenu && (
                <div
                  className="kg-context-menu"
                  data-testid="kg-context-menu"
                  role="menu"
                  style={{ position: "fixed", left: contextMenu.x, top: contextMenu.y }}
                >
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="kg-context-new-topic"
                    onClick={() => {
                      setContextMenu(null);
                      setTopicEditor({ status: "create", title: "", body_md: "", owner: "", saving: false, error: null });
                    }}
                  >
                    New topic here
                  </button>
                </div>
              )}
            </div>

            {selectedNode && (
              <aside
                className="panel kg-drawer"
                data-testid="kg-drawer"
                aria-label={`Details for ${selectedNode.title}`}
              >
                <div className="kg-drawer-head">
                  <div>
                    <span className="kg-node-kind">
                      {NODE_TYPE_LABELS[selectedNode.node_type]}
                    </span>
                    <h2>{selectedNode.title}</h2>
                  </div>
                  <div className="kg-drawer-head-actions">
                    {selectedNode.node_type === "topic" && (
                      <button
                        className="secondary-button"
                        type="button"
                        data-testid="kg-drawer-edit"
                        disabled={detail.status !== "ok"}
                        onClick={() =>
                          detail.status === "ok" &&
                          setTopicEditor({
                            status: "edit",
                            nodeId: selectedNode.id,
                            title: selectedNode.title,
                            body_md: detail.body_md,
                            owner: selectedNode.owner_raw ?? "",
                            saving: false,
                            error: null,
                          })
                        }
                      >
                        Edit
                      </button>
                    )}
                    {selectedNode.node_type === "procedure" &&
                      (onOpenProcedure ? (
                        <button
                          className="secondary-button"
                          type="button"
                          data-testid="kg-drawer-edit-procedure"
                          onClick={() => onOpenProcedure(selectedNode.id)}
                        >
                          {detail.status === "ok" && detail.step_count
                            ? `Open the Skill — ${detail.step_count} ${detail.step_count === 1 ? "step" : "steps"}`
                            : "Open the Skill"}
                        </button>
                      ) : (
                        <span className="kg-note" data-testid="kg-drawer-edit-procedure-note">
                          Open this Skill from the Skills Registry page.
                        </span>
                      ))}
                    {selectedNode.node_type === "target_field" &&
                      (onOpenSemanticModel ? (
                        <button
                          className="secondary-button"
                          type="button"
                          data-testid="kg-drawer-open-semantic-model"
                          onClick={onOpenSemanticModel}
                        >
                          Open in Semantic model
                        </button>
                      ) : (
                        <span className="kg-note" data-testid="kg-drawer-open-semantic-model-note">
                          Open this field from the Semantic model page.
                        </span>
                      ))}
                    {/* Story 44.11: any knowledge consumer can flag a topic or
                        procedure for review — audit-only, no notification. */}
                    {(selectedNode.node_type === "topic" ||
                      selectedNode.node_type === "procedure") && (
                      <button
                        className="secondary-button"
                        type="button"
                        data-testid="kg-drawer-request-review"
                        onClick={() =>
                          setReviewModal({
                            status: "open",
                            nodeId: selectedNode.id,
                            nodeType: selectedNode.node_type as "topic" | "procedure",
                            note: "",
                            saving: false,
                            error: null,
                          })
                        }
                      >
                        Request review
                      </button>
                    )}
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => setSelectedId(null)}
                    >
                      Close
                    </button>
                  </div>
                </div>

                {selectedNode.node_type === "schema_doc" && (
                  <div className="kg-readonly-banner" data-testid="kg-readonly-banner">
                    Generated schema documentation. It is read-only here and is rewritten by
                    the schema context generator, not by hand.
                  </div>
                )}

                {selectedNode.node_type === "target_field" && (
                  <div className="kg-readonly-banner" data-testid="kg-field-readonly-banner">
                    Data-dictionary field. It is read-only on the map — its definition,
                    approval and history live on the Semantic model page.
                  </div>
                )}

                <dl className="kg-meta">
                  <div>
                    <dt>Owner</dt>
                    <dd>{selectedNode.owner ?? "Generated"}</dd>
                  </div>
                  <div>
                    <dt>Scope</dt>
                    <dd>{selectedNode.scope === "platform" ? "Platform" : "Project"}</dd>
                  </div>
                  <div>
                    <dt>Version</dt>
                    <dd className="number">v{selectedNode.version_number}</dd>
                  </div>
                  {selectedNode.doc_kind ? (
                    <div>
                      <dt>Doc kind</dt>
                      <dd>{graphWord(selectedNode.doc_kind)}</dd>
                    </div>
                  ) : null}
                  {selectedNode.node_type === "target_field" ? (
                    <>
                      <div>
                        <dt>Field name</dt>
                        {/* THE LABEL FIRST, THEN THE WIRE NAME — `console-presentation.md` §4.
                            On a `target_field` node the `id` IS the column's wire name
                            (`clicks`, `cost_micros`), never a ULID, and `title` carries the word
                            the business uses for it. Rendering the id alone made the panel say
                            the base's spelling of a field a person already has a name for. */}
                        <dd data-testid="kg-field-name">
                          {selectedNode.title}{" "}
                          <ObjectId value={selectedNode.id} title="Field wire name" />
                        </dd>
                      </div>
                      {selectedNode.field_kind ? (
                        <div>
                          <dt>Kind</dt>
                          <dd data-testid="kg-field-kind-meta">{graphWord(selectedNode.field_kind)}</dd>
                        </div>
                      ) : null}
                      <div>
                        <dt>Status</dt>
                        <dd>{stateLabel(selectedNode.status)}</dd>
                      </div>
                    </>
                  ) : null}
                  {detail.status === "ok" && formatUpdated(detail.updated_at) && (
                    <div>
                      <dt>Updated</dt>
                      <dd>
                        <time className="number">{formatUpdated(detail.updated_at)}</time>
                      </dd>
                    </div>
                  )}
                </dl>

                <section className="kg-drawer-section">
                  <h3>Source content</h3>
                  {detail.status === "loading" && (
                    <p className="kg-status" role="status">
                      Loading the full entry…
                    </p>
                  )}
                  {detail.status === "error" && (
                    <div className="kg-inline-error" role="alert" data-testid="kg-detail-error">
                      {detail.message}
                    </div>
                  )}
                  {detail.status === "ok" && (
                    <RawMarkdown
                      className="kg-body"
                      testId="kg-drawer-body"
                      text={detail.body_md}
                      placeholder="This entry has no body yet."
                    />
                  )}
                  {detail.status === "excerpt_only" && (
                    <>
                      <p className="kg-note">
                        Showing the verbatim excerpt: generated schema docs have no
                        single-document read endpoint yet.
                      </p>
                      <RawMarkdown
                        className="kg-body"
                        testId="kg-drawer-body"
                        text={selectedNode.excerpt}
                        placeholder="This document has no body."
                      />
                    </>
                  )}
                  {detail.status === "field_summary" && (
                    <>
                      <p className="kg-note">
                        Description (summary) — read the full definition in the Semantic
                        model.
                      </p>
                      <RawMarkdown
                        className="kg-body"
                        testId="kg-drawer-body"
                        text={
                          // Server caps excerpts at 280 chars; make the cut
                          // visible instead of ending mid-sentence (44.10
                          // re-review).
                          selectedNode.excerpt.length === 280
                            ? `${selectedNode.excerpt}…`
                            : selectedNode.excerpt
                        }
                        placeholder="This field has no description yet."
                      />
                    </>
                  )}
                </section>

                {selectedNode.node_type === "target_field" && (
                  <section className="kg-drawer-section" data-testid="kg-fed-by">
                    <h3>{fedBy.status === "ok" ? `Fed by (${fedBy.rows.length})` : "Fed by"}</h3>
                    <p className="kg-note">
                      The datastream mappings that actually land in this field, in this
                      project. Read-only — mappings are edited on the Mapping page.
                    </p>
                    {fedBy.status === "loading" && (
                      <p className="kg-status" role="status">
                        Loading what feeds this field…
                      </p>
                    )}
                    {fedBy.status === "error" && (
                      <div className="kg-inline-error" role="alert" data-testid="kg-fed-by-error">
                        {fedBy.message}
                      </div>
                    )}
                    {fedBy.status === "ok" && fedBy.rows.length === 0 && (
                      <p className="kg-note" data-testid="kg-fed-by-empty">
                        Nothing feeds this field yet — no datastream in this project maps a
                        source column onto it.
                      </p>
                    )}
                    {fedBy.status === "ok" && fedBy.rows.length > 0 && (
                      <ul className="kg-fed-by-list">
                        {fedBy.rows.map((row) => (
                          <li
                            key={`${row.datastream_id}:${row.source_field}`}
                            className="kg-fed-by-row"
                            data-testid={`kg-fed-by-${row.datastream_id}-${row.source_field}`}
                          >
                            <span className="kg-fed-by-source">{row.source_field}</span>
                            <span className="kg-fed-by-arrow" aria-hidden="true">
                              →
                            </span>
                            {/* `dimension_lineage._plan_rows` reads
                                `SELECT d.id, d.name … FROM app.datastreams`, and
                                `name` is `NOT NULL` (migration 023), so the word
                                is served for every row of "Fed by". The
                                `?? row.datastream_id` was unreachable and would
                                have put `ds_<ULID>` on the right of the arrow,
                                where the whole line's job is to say WHICH
                                Datastream feeds this field. */}
                            <span className="kg-fed-by-stream">
                              {row.datastream_name ?? "Unnamed Datastream"}
                            </span>
                            {row.module_name && (
                              <span className="kg-tag kg-tag--module">{row.module_name}</span>
                            )}
                            {row.is_key_column && <span className="kg-tag">key</span>}
                          </li>
                        ))}
                      </ul>
                    )}
                  </section>
                )}

                {/* AI-158 : ce que « Request review » remplit, visible la ou on
                    l'a rempli -- et desormais aussi sur l'etabli du Skill, par
                    le meme composant. Le panneau n'appartient pas au mindmap :
                    il appartient au noeud. */}
                <NodeRemarks
                  projectId={projectId}
                  nodeId={selectedNode.id}
                  nodeType={
                    selectedNode.node_type === "topic" ||
                    selectedNode.node_type === "procedure"
                      ? selectedNode.node_type
                      : null
                  }
                  nodeVersion={selectedNode.version_number ?? null}
                  className="kg-drawer-section"
                  reloadToken={reviewReloadToken}
                  /* The remark is read here and answered on the node's own
                     workbench — the mindmap never becomes a second editor.
                     `onAdjust` receives the remark, and the remark carries the
                     node it speaks of, so the hand-over never has to guess
                     which node the drawer had selected. */
                  onAdjust={onAdjustNode ? (remark) => onAdjustNode(remark.node_type, remark.node_id) : undefined}
                />

                <section className="kg-drawer-section">
                  <h3>Links in ({inbound.length})</h3>
                  {inbound.length === 0 ? (
                    <p className="kg-note">Nothing links to this node.</p>
                  ) : (
                    <ul className="kg-edge-list" data-testid="kg-inbound">
                      {inbound.map((e) => (
                        <li key={e.id} className="kg-edge-row">
                          <button
                            type="button"
                            className="kg-edge-nav"
                            aria-label={`Open ${peerTitle(e.from_id)}`}
                            disabled={hiddenPeerLabel(e.from_id) !== null}
                            title={hiddenPeerLabel(e.from_id) ?? undefined}
                            onClick={() => focusNode(e.from_id)}
                          >
                            <span className="kg-edge-type">{graphWord(e.edge_type)}</span>
                            <span className="kg-edge-peer">
                              {peerTitle(e.from_id)}
                            </span>
                            {hiddenPeerLabel(e.from_id) && (
                              <span className="kg-edge-hidden">{hiddenPeerLabel(e.from_id)}</span>
                            )}
                          </button>
                          {confirmDeleteEdgeId === e.id ? (
                            <span className="kg-edge-confirm" data-testid={`kg-edge-confirm-${e.id}`}>
                              <button
                                type="button"
                                className="kg-edge-confirm-yes"
                                data-testid={`kg-edge-delete-confirm-${e.id}`}
                                disabled={deletingEdgeId === e.id}
                                onClick={() => void handleDeleteEdge(e.id)}
                              >
                                {deletingEdgeId === e.id ? "Removing…" : "Confirm"}
                              </button>
                              <button
                                type="button"
                                className="kg-edge-confirm-no"
                                onClick={() => setConfirmDeleteEdgeId(null)}
                              >
                                Cancel
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="kg-edge-delete"
                              aria-label={`Delete link to ${peerTitle(e.from_id)}`}
                              data-testid={`kg-edge-delete-${e.id}`}
                              onClick={() => setConfirmDeleteEdgeId(e.id)}
                            >
                              ✕
                            </button>
                          )}
                          {edgeActionError && edgeActionError.id === e.id && (
                            <p
                              className="kg-inline-error"
                              role="alert"
                              data-testid={`kg-edge-error-${e.id}`}
                            >
                              {edgeActionError.message}
                            </p>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </section>

                <section className="kg-drawer-section">
                  <h3>Links out ({outbound.length})</h3>
                  {outbound.length === 0 ? (
                    <p className="kg-note">This node links to nothing yet.</p>
                  ) : (
                    <ul className="kg-edge-list" data-testid="kg-outbound">
                      {outbound.map((e) => (
                        <li key={e.id} className="kg-edge-row">
                          <button
                            type="button"
                            className="kg-edge-nav"
                            aria-label={`Open ${peerTitle(e.to_id)}`}
                            disabled={hiddenPeerLabel(e.to_id) !== null}
                            title={hiddenPeerLabel(e.to_id) ?? undefined}
                            onClick={() => focusNode(e.to_id)}
                          >
                            <span className="kg-edge-type">{graphWord(e.edge_type)}</span>
                            <span className="kg-edge-peer">
                              {peerTitle(e.to_id)}
                            </span>
                            {hiddenPeerLabel(e.to_id) && (
                              <span className="kg-edge-hidden">{hiddenPeerLabel(e.to_id)}</span>
                            )}
                          </button>
                          {confirmDeleteEdgeId === e.id ? (
                            <span className="kg-edge-confirm" data-testid={`kg-edge-confirm-${e.id}`}>
                              <button
                                type="button"
                                className="kg-edge-confirm-yes"
                                data-testid={`kg-edge-delete-confirm-${e.id}`}
                                disabled={deletingEdgeId === e.id}
                                onClick={() => void handleDeleteEdge(e.id)}
                              >
                                {deletingEdgeId === e.id ? "Removing…" : "Confirm"}
                              </button>
                              <button
                                type="button"
                                className="kg-edge-confirm-no"
                                onClick={() => setConfirmDeleteEdgeId(null)}
                              >
                                Cancel
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="kg-edge-delete"
                              aria-label={`Delete link to ${peerTitle(e.to_id)}`}
                              data-testid={`kg-edge-delete-${e.id}`}
                              onClick={() => setConfirmDeleteEdgeId(e.id)}
                            >
                              ✕
                            </button>
                          )}
                          {edgeActionError && edgeActionError.id === e.id && (
                            <p
                              className="kg-inline-error"
                              role="alert"
                              data-testid={`kg-edge-error-${e.id}`}
                            >
                              {edgeActionError.message}
                            </p>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              </aside>
            )}
          </div>

          {edgeModal.status === "open" && (
            <div
              className="kg-modal-backdrop"
              role="dialog"
              aria-modal="true"
              aria-labelledby="kg-edge-modal-heading"
            >
              <div className="panel kg-modal">
                <h2 id="kg-edge-modal-heading">Link these two nodes</h2>
                <p className="kg-note">
                  {peerTitle(edgeModal.fromId)} →{" "}
                  {peerTitle(edgeModal.toId)}
                </p>

                {edgeModal.error && (
                  <div className="kg-inline-error" role="alert" data-testid="kg-edge-modal-error">
                    {edgeModal.error}
                  </div>
                )}

                <div className="kg-edge-type-suggestions">
                  {EDGE_TYPE_SUGGESTIONS.map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      className={`kg-edge-suggestion${edgeModal.edgeType === suggestion ? " is-active" : ""}`}
                      data-testid={`kg-edge-suggestion-${suggestion}`}
                      onClick={() =>
                        setEdgeModal((prev) =>
                          prev.status === "open" ? { ...prev, edgeType: suggestion } : prev,
                        )
                      }
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>

                <label className="kg-field">
                  <span>Link type</span>
                  <input
                    type="text"
                    data-testid="kg-edge-type-input"
                    value={edgeModal.edgeType}
                    placeholder="e.g. depends_on"
                    onChange={(e) =>
                      setEdgeModal((prev) =>
                        prev.status === "open" ? { ...prev, edgeType: e.target.value } : prev,
                      )
                    }
                  />
                </label>

                <div className="kg-modal-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setEdgeModal({ status: "closed" })}
                  >
                    Cancel
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    data-testid="kg-edge-modal-submit"
                    disabled={edgeModal.saving || !edgeModal.edgeType.trim()}
                    onClick={() => void submitEdgeModal()}
                  >
                    {edgeModal.saving ? "Linking…" : "Create link"}
                  </button>
                </div>
              </div>
            </div>
          )}

        </>
      )}

      {/* Deliberately OUTSIDE the "there are nodes" branch: the empty state
          opens this same editor, and an empty project is precisely the case
          where creating the first topic matters most. */}
      {topicEditor.status !== "closed" && (
        <div
          className="kg-modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="kg-topic-editor-heading"
        >
          <div className="panel kg-modal kg-modal--wide">
            <h2 id="kg-topic-editor-heading">
              {topicEditor.status === "create" ? "New topic" : "Edit topic"}
            </h2>

            {topicEditor.error && (
              <div className="kg-inline-error" role="alert" data-testid="kg-topic-editor-error">
                {topicEditor.error}
              </div>
            )}

            <label className="kg-field">
              <span>Title</span>
              <input
                type="text"
                data-testid="kg-topic-editor-title"
                value={topicEditor.title}
                onChange={(e) =>
                  setTopicEditor((prev) =>
                    prev.status === "closed" ? prev : { ...prev, title: e.target.value },
                  )
                }
              />
            </label>

            <label className="kg-field">
              <span>Owner (email)</span>
              <input
                type="text"
                data-testid="kg-topic-editor-owner"
                value={topicEditor.owner}
                placeholder="Unset — falls back to created_by, then &ldquo;auto&rdquo;"
                onChange={(e) =>
                  setTopicEditor((prev) =>
                    prev.status === "closed" ? prev : { ...prev, owner: e.target.value },
                  )
                }
              />
            </label>

            {/* Side-by-side Markdown + raw preview, the same shape Story 44.1
                ships on Context ▸ Knowledge — the drawer claims parity with
                that editor, so it has to actually show a preview. */}
            <div className="kg-editor-split">
              <label className="kg-field">
                <span>Body (Markdown)</span>
                <textarea
                  rows={12}
                  data-testid="kg-topic-editor-body"
                  value={topicEditor.body_md}
                  onChange={(e) =>
                    setTopicEditor((prev) =>
                      prev.status === "closed" ? prev : { ...prev, body_md: e.target.value },
                    )
                  }
                />
              </label>
              <div className="kg-editor-preview" data-testid="kg-topic-editor-preview">
                <span className="kg-editor-preview-label">Preview</span>
                <RawMarkdown
                  className="kg-body"
                  text={topicEditor.body_md}
                  placeholder="The preview appears here."
                />
              </div>
            </div>

            <div className="kg-modal-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setTopicEditor({ status: "closed" })}
              >
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                data-testid="kg-topic-editor-submit"
                disabled={topicEditor.saving || !topicEditor.title.trim()}
                onClick={() => void submitTopicEditor()}
              >
                {topicEditor.saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Story 44.11: "Request review" modal — deliberately its own dialog
          (not folded into the drawer) so the confirmation message survives
          the drawer being closed or the selection changing. */}
      {reviewModal.status !== "closed" && (
        <div
          className="kg-modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="kg-review-modal-heading"
        >
          <div className="panel kg-modal">
            <h2 id="kg-review-modal-heading">Request review</h2>

            {reviewModal.status === "open" && (
              <>
                <p className="kg-note">
                  This does not send a message to anyone — it queues your remark against
                  this node&rsquo;s current version, for its owner to accept or decline.
                </p>

                {reviewModal.error && (
                  <div className="kg-inline-error" role="alert" data-testid="kg-review-modal-error">
                    {reviewModal.error}
                  </div>
                )}

                {/* La note n'est PLUS facultative : la file la refuse vide
                    (`length(btrim(note)) >= 1`), et une remarque muette ne peut
                    ni se verifier ni se clore. */}
                <label className="kg-field">
                  <span>Note</span>
                  <textarea
                    rows={4}
                    required
                    data-testid="kg-review-modal-note"
                    value={reviewModal.note}
                    placeholder="What should the owner look at?"
                    onChange={(e) =>
                      setReviewModal((prev) =>
                        prev.status === "open" ? { ...prev, note: e.target.value } : prev,
                      )
                    }
                  />
                </label>

                <div className="kg-modal-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setReviewModal({ status: "closed" })}
                  >
                    Cancel
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    data-testid="kg-review-modal-submit"
                    disabled={reviewModal.saving || !reviewModal.note.trim()}
                    onClick={() => void submitReviewRequest()}
                  >
                    {reviewModal.saving ? "Sending…" : "Request review"}
                  </button>
                </div>
              </>
            )}

            {reviewModal.status === "success" && (
              <>
                <p className="kg-note" role="status" data-testid="kg-review-modal-success">
                  Review requested — it is queued against this node&rsquo;s current
                  version, and stays open until someone accepts or declines it.
                </p>
                <div className="kg-modal-actions">
                  <button
                    className="primary-button"
                    type="button"
                    onClick={() => setReviewModal({ status: "closed" })}
                  >
                    Close
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
