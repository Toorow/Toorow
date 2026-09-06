/**
 * Stories 45.2 and 45.3: the two halves that were never wired.
 *
 * C-3 -- `SkillEditorDrawer`'s only `<SkillEditorDrawer` call site in the whole
 * history of `main` was its own test file, so every acceptance criterion of 45.2
 * was proved against a component no operator could reach. These tests drive it
 * through `Procedures`, the page whose task list claimed the mount.
 *
 * C-4 -- the string `business` had never appeared in `KnowledgeGraphPage.tsx`.
 * These tests drive the scope through the exported pure functions and through the
 * page, including the no-refetch rule the story is explicit about.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Procedures from "../shell/pages/Procedures";
import {
  applyBusinessScope,
  businessScopeIds,
  BUSINESS_NODE_TYPES,
  NODE_TYPE_LABELS,
  NODE_TYPE_ORDER,
  type BusinessScopeInput,
  type GraphEdgeRow,
  type GraphNodeRow,
} from "../KnowledgeGraphPage";
import type { ContextHubContentContext } from "../connaissances/ContextHubLayout";

// --- Story 45.3 taxonomy: one domain, a layer, a nested layer, a sibling -----
const TAXONOMY = {
  domains: [{ id: "bdm_retail" }, { id: "bdm_finance" }],
  classifications: [
    { id: "bcl_subs", domain_id: "bdm_retail", parent_id: null },
    { id: "bcl_annual", domain_id: "bdm_retail", parent_id: "bcl_subs" },
    { id: "bcl_stores", domain_id: "bdm_retail", parent_id: null },
    { id: "bcl_tax", domain_id: "bdm_finance", parent_id: null },
  ],
  links: [
    { taxonomy_type: "business_classification", taxonomy_id: "bcl_annual", target_id: "tpc_renewals" },
    { taxonomy_type: "business_domain", taxonomy_id: "bdm_finance", target_id: "tpc_vat" },
  ],
};

function node(id: string, node_type: GraphNodeRow["node_type"]): GraphNodeRow {
  return {
    id,
    node_type,
    title: id,
    excerpt: "",
    owner: null,
    version_number: 1,
    scope: "project",
    status: "active",
  } as GraphNodeRow;
}

const NODES: GraphNodeRow[] = [
  node("bdm_retail", "business_domain"),
  node("bdm_finance", "business_domain"),
  node("bcl_subs", "business_classification"),
  node("bcl_annual", "business_classification"),
  node("bcl_stores", "business_classification"),
  node("bcl_tax", "business_classification"),
  node("tpc_renewals", "topic"),
  node("tpc_vat", "topic"),
];

const EDGES: GraphEdgeRow[] = [
  { id: "e1", from_id: "bdm_retail", to_id: "bcl_subs", edge_type: "contains" } as GraphEdgeRow,
  { id: "e2", from_id: "bcl_subs", to_id: "bcl_annual", edge_type: "contains" } as GraphEdgeRow,
  { id: "e3", from_id: "bcl_annual", to_id: "tpc_renewals", edge_type: "explains" } as GraphEdgeRow,
  { id: "e4", from_id: "bdm_finance", to_id: "tpc_vat", edge_type: "explains" } as GraphEdgeRow,
];

function scope(selected: BusinessScopeInput["selected"]): BusinessScopeInput {
  return { selected, ...TAXONOMY } as BusinessScopeInput;
}

describe("Story 45.3 — business scope over the governed graph", () => {
  it("keeps the ancestor path and the whole subtree of a selected layer", () => {
    const ids = businessScopeIds(scope({ type: "business_classification", id: "bcl_subs" }));
    // The domain above it, itself, and the nested layer beneath it.
    expect(ids).toEqual(new Set(["bdm_retail", "bcl_subs", "bcl_annual"]));
    // Not the sibling layer, and not the other domain.
    expect(ids?.has("bcl_stores")).toBe(false);
    expect(ids?.has("bdm_finance")).toBe(false);
  });

  it("keeps a whole domain's descendants when the domain is selected", () => {
    const ids = businessScopeIds(scope({ type: "business_domain", id: "bdm_retail" }));
    expect(ids).toEqual(new Set(["bdm_retail", "bcl_subs", "bcl_annual", "bcl_stores"]));
  });

  it("keeps the governed resources linked to anything in scope, and nothing else", () => {
    const bundle = applyBusinessScope(NODES, EDGES, scope({ type: "business_domain", id: "bdm_retail" }));
    expect(bundle.nodes.map((item) => item.id).sort()).toEqual(
      ["bcl_annual", "bcl_stores", "bcl_subs", "bdm_retail", "tpc_renewals"],
    );
    // Edges are kept only when both ends survive: the finance edge is gone.
    expect(bundle.edges.map((item) => item.id).sort()).toEqual(["e1", "e2", "e3"]);
  });

  it("restores the complete bundle when the scope is cleared", () => {
    const bundle = applyBusinessScope(NODES, EDGES, scope(null));
    expect(bundle.nodes).toHaveLength(NODES.length);
    expect(bundle.edges).toHaveLength(EDGES.length);
    expect(businessScopeIds(scope(null))).toBeNull();
  });

  it("is bounded by a visited set if the taxonomy ever contains a cycle", () => {
    const cyclic = {
      selected: { type: "business_classification", id: "a" },
      domains: [{ id: "d" }],
      classifications: [
        { id: "a", domain_id: "d", parent_id: "b" },
        { id: "b", domain_id: "d", parent_id: "a" },
      ],
      links: [],
    } as BusinessScopeInput;
    expect(businessScopeIds(cyclic)).toEqual(new Set(["a", "b", "d"]));
  });

  it("gives the three Context Hub node types a label and marks them business-managed", () => {
    // Story 45.1 projected them server-side; they rendered with no kind badge.
    expect(NODE_TYPE_LABELS.business_domain).toBe("Business Domain");
    expect(NODE_TYPE_LABELS.business_classification).toBe("Business layer");
    expect(NODE_TYPE_LABELS.report_view).toBe("Report view");
    expect(BUSINESS_NODE_TYPES.has("business_domain")).toBe(true);
    expect(BUSINESS_NODE_TYPES.has("topic")).toBe(false);
    // Business domains lead the order: the mindmap is rooted in them
    // (context-hub.md, "a usable mindmap rooted in Business Domains"). Asserted
    // separately from the full list so the rooting invariant survives any later
    // addition instead of being re-derived from a literal each time.
    expect(NODE_TYPE_ORDER.slice(0, 2)).toEqual([
      "business_domain",
      "business_classification",
    ]);
    expect(NODE_TYPE_ORDER).toEqual([
      "business_domain",
      "business_classification",
      "topic",
      "procedure",
      "schema_doc",
      "target_field",
      // AI-298: next to the dictionary field, and never merged with it — the
      // registry is where a field a project declared under its own source lives.
      "canonical_field",
      "report_view",
      // Story 49.6: the governed objects the graph REACHES. Without them a
      // business link to a Datastream produced an edge whose endpoint had no
      // node, and the bundle dropped it as dangling — the link existed and the
      // mindmap showed nothing.
      "semantic_view",
      "semantic_concept",
      "datastream",
    ]);
  });
});

// --- Story 45.2: the drawer, driven through the Procedures page --------------
/** The full capability shape the page validates; a partial one reads as "could
 *  not be verified" and disables editing. */
const CAPS = { can_write: true, version_history: true, usage: false };
const PROCEDURE = {
  id: "prc_1",
  project_id: "p1",
  name: "Attribution reconciliation",
  description: "How to reconcile post-click attribution",
  frontmatter_yaml: 'name: "Attribution reconciliation"\ndescription: "How to reconcile post-click attribution"\nreview_cadence: "quarterly"\n',
  body_md: "1. Pull the report\n",
  status: "active",
  owner: null,
  created_by: "owner@example.com",
  created_at: "2026-07-28T10:00:00Z",
  updated_at: "2026-07-28T10:00:00Z",
  version_number: 2,
  capabilities: CAPS,
};

/**
 * Declared as the type the page receives, the way SkillEditorDrawer.test does.
 * Untyped, `status: "active"` widened to `string` and the fixture stopped being
 * checked against the contract at all -- it could have carried a status the
 * screen has no branch for and nothing would have said so.
 */
const BUSINESS_CONTEXT: ContextHubContentContext = {
  graph: null,
  selected: null,
  selectedNode: null,
  domains: [
    { id: "bdm_retail", slug: "retail", name: "Retail", description: "", owner: null, status: "active", version_number: 1, current_version_id: "mdver_retail_1" },
  ],
  classifications: [],
  links: [],
  selectedLinks: [],
  selectTaxonomy: vi.fn(),
  refresh: vi.fn().mockResolvedValue(undefined),
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("Story 45.2 — the skill drawer, reachable from the page that owns it", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("opens the normalized drawer instead of the raw procedure modal", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/context/procedures")) {
        return Promise.resolve(jsonResponse({ procedures: [PROCEDURE], capabilities: CAPS }));
      }
      if (url.startsWith("/api/context/skill-tools")) {
        return Promise.resolve(jsonResponse({
          tools: [{ name: "get_report", description: "Read a governed report", profile: "kpi", effect: "read", data_class: "governed", confirmation_mode: "none" }],
        }));
      }
      if (url.startsWith("/api/datamodel/fields")) {
        return Promise.resolve(jsonResponse([
          { name: "media_cost_micros", display_name: "Media cost", field_kind: "metric", description: null, status: "approved" },
        ]));
      }
      return Promise.resolve(jsonResponse({}));
    }));

    render(<Procedures projectId="p1" businessContext={BUSINESS_CONTEXT} />);
    fireEvent.click(await screen.findByRole("button", { name: /Add Skill/i }));

    // The drawer, not the v1 modal: an ordered execution rail with real server
    // tools and governed MDM metrics, none of them hardcoded.
    expect(await screen.findByRole("dialog", { name: /create skill/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("get_report")).toBeInTheDocument());
    expect(screen.getByText("Media cost")).toBeInTheDocument();
    // The client taxonomy is selectable because Context Hub passed it down.
    expect(screen.getByText("Retail", { selector: "option" })).toBeInTheDocument();
  });

  it("writes validated tool_bindings, keeps unknown frontmatter keys, and links the business scope", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      if (url.startsWith("/api/context/procedures") && init?.method === "PATCH") {
        return Promise.resolve(jsonResponse({ ...PROCEDURE, version_number: 3 }));
      }
      if (url.startsWith("/api/context/procedures")) {
        return Promise.resolve(jsonResponse({ procedures: [PROCEDURE], capabilities: CAPS }));
      }
      if (url.startsWith("/api/context/skill-tools")) {
        return Promise.resolve(jsonResponse({
          tools: [{ name: "get_report", description: "Read a governed report", profile: "kpi", effect: "read", data_class: "governed", confirmation_mode: "none" }],
        }));
      }
      if (url.startsWith("/api/datamodel/fields")) return Promise.resolve(jsonResponse([]));
      if (url.startsWith("/api/context/business-links")) return Promise.resolve(jsonResponse({ id: "blink_9" }, 201));
      return Promise.resolve(jsonResponse({}));
    }));

    render(<Procedures projectId="p1" businessContext={BUSINESS_CONTEXT} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Skill" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(screen.getByText("get_report")).toBeInTheDocument());

    // Bind the first step to a real catalog tool and assign the business scope.
    const toolSelects = dialog.querySelectorAll<HTMLSelectElement>("select");
    const stepTool = Array.from(toolSelects).find((select) =>
      Array.from(select.options).some((option) => option.value === "get_report"));
    expect(stepTool).toBeTruthy();
    await userEvent.selectOptions(stepTool!, "get_report");
    // The client taxonomy Context Hub passed down; a conditional assertion here
    // would let the test pass while proving nothing.
    const business = Array.from(toolSelects).find((select) =>
      Array.from(select.options).some((option) => option.value.includes("bdm_retail")));
    expect(business).toBeTruthy();
    await userEvent.selectOptions(business!, business!.options[1].value);

    fireEvent.click(screen.getByRole("button", { name: /^save/i }));

    await waitFor(() => expect(calls.some((call) => call.init?.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.init?.method === "PATCH")!;
    const payload = JSON.parse(String(patch.init?.body)) as { frontmatter_yaml: string; body_md: string };
    // The binding is written in the normalized frontmatter…
    expect(payload.frontmatter_yaml).toContain("tool_bindings:");
    expect(payload.frontmatter_yaml).toContain("get_report");
    // …the unknown key survives verbatim…
    expect(payload.frontmatter_yaml).toContain('review_cadence: "quarterly"');
    // …and the step order is what the rendered body says.
    expect(payload.body_md).toContain("Pull the report");

    // …and the association goes through the existing typed store, after the
    // procedure exists.
    await waitFor(() =>
      expect(calls.some((call) => call.url.startsWith("/api/context/business-links")
        && call.init?.method === "POST")).toBe(true));
  });

  it("keeps a saved skill saved when only its business assignment fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith("/api/context/procedures") && init?.method === "PATCH") {
        return Promise.resolve(jsonResponse({ ...PROCEDURE, version_number: 3 }));
      }
      if (url.startsWith("/api/context/procedures")) {
        return Promise.resolve(jsonResponse({ procedures: [PROCEDURE], capabilities: CAPS }));
      }
      if (url.startsWith("/api/context/skill-tools")) return Promise.resolve(jsonResponse({ tools: [] }));
      if (url.startsWith("/api/datamodel/fields")) return Promise.resolve(jsonResponse([]));
      return Promise.resolve(jsonResponse({}));
    }));

    render(<Procedures projectId="p1" businessContext={BUSINESS_CONTEXT} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit Skill" }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: /^save/i }));

    // No assignment selected -> no business-link call, and the drawer closes on
    // a successful save rather than reporting a failure it did not have.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
