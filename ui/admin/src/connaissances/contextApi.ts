/**
 * contextApi — the ONE surviving fetch helper of the 11.1 context REST module.
 *
 * All writes go exclusively through these helpers (AD-15), and every call goes
 * through `apiFetch` — the ONE seam that attaches the bearer token (injected
 * `__TOOROW_API_KEY__` OR the `api_token` in localStorage). This module used to
 * hand-roll its own header from `__TOOROW_API_KEY__` only, which silently
 * dropped the localStorage token every non-disabled auth mode relies on.
 */

import { apiFetch } from "../lib/apiFetch";
import type { ApiError } from "./types";

// ---------------------------------------------------------------------------
// Error envelope (unchanged contract: throw the server's {code, message})
// ---------------------------------------------------------------------------

async function throwIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  const err: ApiError = await res
    .json()
    .catch(() => ({ code: "unknown", message: `HTTP ${res.status}` }));
  throw err;
}

/* THE TOPIC AND PROCEDURE HELPERS ARE NOT HERE ANY MORE — measured 2026-08-25
 * (audit 03): all ten had ZERO callers. `KnowledgeBasePage.tsx` and
 * `shell/pages/Procedures.tsx` have called the endpoints themselves, through
 * `apiFetch` with the mandatory `project_id`, since Story 44.1 — and the two
 * archive helpers here omitted that parameter, so a caller would have bought a
 * guaranteed 422. Ten unused wrappers around one seam is how the two drift
 * apart; the endpoints' contract lives in `server/core/context_api.py`. */

/* THE PROJECT-WIDE EDGE READ IS NOT HERE ANY MORE either (story 49-6, lot 3).
 *
 * `listGraphEdges` had exactly one caller, the Usage facet, and what it did
 * there was fetch EVERY edge of the project so the browser could keep the two
 * or three naming one node. That panel now asks the relationship authority for
 * the node it is showing (`listNodeRelationships` below), which leaves this
 * wrapper with zero callers — and a wrapper kept "just in case" is how a
 * fan-out this document forbids finds its way back in. The mindmap reads,
 * POSTs and DELETEs `/api/context/graph/edges` itself, on the canvas where both
 * endpoints are visible.
 */

/* THE EDGE WRITES ARE NOT HERE ANY MORE, and that is the point.
 *
 * `createGraphEdge` and `deleteGraphEdge` had exactly one caller, the
 * superseded `GraphEdges.tsx`, deleted with it. `KnowledgeGraphPage.tsx` has
 * POSTed and DELETEd `/api/context/graph/edges` itself since Story 44.5, on the
 * canvas where both endpoints are visible and where removing a link asks twice.
 * Keeping a second, unused writer here is how the two drift apart again. */

// ---------------------------------------------------------------------------
// The "Used by / Related" facet (story 49-6, AC3/AC5)
// ---------------------------------------------------------------------------

/**
 * ONE DOOR, THE AUTHORITY'S. `GET /api/context/relationships` is a bounded,
 * permission-filtered server projection over `app.context_relationships`
 * (`server/core/context_api.py:1691`, `context_relationships.list_for_node:588`).
 * It replaces reading EVERY edge of the project plus the whole graph bundle and
 * filtering in the browser — the fan-out `context-hub.md` forbids, and the
 * reason a `semantic_view`, a `metric` or a `datastream` peer was invisible: the
 * legacy projection's CHECK does not know those three, so they are held by the
 * authority alone.
 *
 * A FAILED READ IS NOT "NO RELATIONS". The route answers 200 with
 * `state: "unavailable"` and a sentence rather than 500, so the panel says why
 * and the rest of the workbench keeps working. That typed answer is handed back
 * here as-is; only a refusal (401/404/422) throws, because those are answers
 * about the CALLER, not about the relations.
 */

/** One end of a relation: a type and an id, and nothing copied. */
export interface RelationshipEndpoint {
  type: string;
  id: string;
}

/** The owner's OWN address, resolved server-side from the route table. */
export interface RelationshipOwnerLink {
  surface: string;
  href: string;
}

export interface RelatedItem {
  relationship_id: string;
  relationship_kind: string;
  status: string;
  provenance: string;
  created_by: string;
  created_at: string;
  direction: "outgoing" | "incoming";
  other: RelationshipEndpoint;
  owner: RelationshipOwnerLink;
}

export type NodeRelationships =
  | {
      state: "ready";
      node: RelationshipEndpoint;
      outgoing: RelatedItem[];
      incoming: RelatedItem[];
      limit: number;
      truncated: boolean;
    }
  | {
      state: "unavailable";
      node: RelationshipEndpoint;
      reason: string;
      message: string;
    };

/** The sentence used when the server could not supply one of its own. Never an
 *  empty list: "I could not look" and "nothing is related" are two facts. */
const UNREADABLE = "Open this panel again in a moment: the related items could not be read.";

function relatedItems(value: unknown, direction: "outgoing" | "incoming"): RelatedItem[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is RelatedItem =>
      !!item
      && typeof item === "object"
      && typeof (item as RelatedItem).relationship_id === "string"
      && !!(item as RelatedItem).other
      && typeof (item as RelatedItem).other.type === "string",
  ).map((item) => ({ ...item, direction }));
}

export async function listNodeRelationships({
  projectId,
  nodeType,
  nodeId,
  limit,
}: {
  projectId: string | null;
  nodeType: string;
  nodeId: string;
  /** Omitted = the server's own bound (`DEFAULT_FACET_LIMIT`). The panel never
   *  invents a wider one: past that a facet is a dump, and the server says so
   *  with `truncated` rather than letting a reader believe the list is whole. */
  limit?: number;
}): Promise<NodeRelationships> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  params.set("node_type", nodeType);
  params.set("node_id", nodeId);
  if (limit) params.set("limit", String(limit));
  const res = await apiFetch(`/api/context/relationships?${params.toString()}`);
  await throwIfNotOk(res);
  const body = (await res.json()) as Record<string, unknown>;
  const node: RelationshipEndpoint = {
    type: nodeType,
    id: nodeId,
    ...(body.node && typeof body.node === "object" ? (body.node as RelationshipEndpoint) : {}),
  };
  if (body.state !== "ready") {
    return {
      state: "unavailable",
      node,
      reason: typeof body.reason === "string" ? body.reason : "related_items_unreadable",
      message: typeof body.message === "string" && body.message.trim() !== "" ? body.message : UNREADABLE,
    };
  }
  return {
    state: "ready",
    node,
    outgoing: relatedItems(body.outgoing, "outgoing"),
    incoming: relatedItems(body.incoming, "incoming"),
    limit: typeof body.limit === "number" ? body.limit : 0,
    truncated: body.truncated === true,
  };
}
