/**
 * KnowledgeGraphPage — Story 44.4 (Context ▸ Graph, read + drawer).
 *
 * Pins the behaviours the story is actually judged on:
 *   - ONE `GET /api/context/graph` per load; filters re-layout client-side and
 *     must never refetch;
 *   - nodes carry verbatim excerpts, owner, version, Platform / auto / doc_kind
 *     badges — no AI summary, no invented owner;
 *   - node-type toggles remove nodes AND the edges that touched them;
 *   - honest loading / error / empty states (empty offers a real CTA);
 *   - clicking a node opens the drawer, which fetches the FULL body (topics and
 *     procedures) and, for schema docs — which have no single-doc route — says
 *     it is showing the excerpt and shows the read-only banner (AD-17);
 *   - >500 nodes warns but still renders (R44-UX04).
 *
 * jsdom notes: React Flow needs ResizeObserver, DOMMatrixReadOnly, element
 * offset dimensions and SVGElement.getBBox, none of which jsdom implements.
 * They are stubbed below (the shapes React Flow's own testing guide uses).
 * elkjs is NOT mocked: the page imports the bundled build precisely so the real
 * layered layout runs in-process here — `layoutProjection("domains", …)`, which
 * IS the 44.4 layered-RIGHT layout since 44.6 generalised it, is asserted
 * directly.
 */
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MarkerType } from "@xyflow/react";
import KnowledgeGraphPage, {
  applyGraphFilters,
  edgeTypeOptions,
  layoutProjection,
  nodeMatchesQuery,
  projectGraph,
  toElkGraphForProjection,
  toFlowEdges,
  DEFAULT_FILTERS,
  type GraphEdgeRow,
  type GraphNodeRow,
} from "../KnowledgeGraphPage";
import { WORKSPACE_BY_KEY } from "../shell/navigation";

// ---------------------------------------------------------------------------
// jsdom shims React Flow requires
// ---------------------------------------------------------------------------

class ResizeObserverStub {
  callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
    // XYPanZoom's extentResizeObserver reads entry.contentRect.width/height --
    // an entry without contentRect crashes React Flow during mount (the whole
    // tree unmounts and every rendering test sees an empty body).
    const contentRect = {
      width: 800, height: 600, x: 0, y: 0, top: 0, left: 0, bottom: 600, right: 800,
      toJSON() { return this; },
    } as DOMRectReadOnly;
    this.callback(
      [{ target, contentRect } as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

class DOMMatrixReadOnlyStub {
  m22: number;
  constructor(transform?: string) {
    const scale = transform?.match(/scale\(([0-9.]+)\)/)?.[1];
    this.m22 = scale === undefined ? 1 : Number.parseFloat(scale);
  }
}

/**
 * Real elk (no worker) is genuinely doing the layout, and React Flow settles
 * over a couple of animation frames — 1s (the testing-library default) is not a
 * meaningful budget here, so async utilities get a real one. Per-test timeouts
 * below match.
 */
const ASYNC_TIMEOUT = 15000;
const TEST_TIMEOUT = 30000;
configure({ asyncUtilTimeout: ASYNC_TIMEOUT });

beforeAll(() => {
  // NOT vi.stubGlobal: afterEach() unstubs globals, and these shims must live
  // for the whole file (React Flow reads them on every mount).
  const g = globalThis as unknown as Record<string, unknown>;
  g.ResizeObserver = ResizeObserverStub;
  g.DOMMatrixReadOnly = DOMMatrixReadOnlyStub;
  Object.defineProperties(HTMLElement.prototype, {
    offsetHeight: {
      configurable: true,
      get(this: HTMLElement) {
        return Number.parseFloat(this.style.height) || 800;
      },
    },
    offsetWidth: {
      configurable: true,
      get(this: HTMLElement) {
        return Number.parseFloat(this.style.width) || 1200;
      },
    },
  });
  (SVGElement.prototype as unknown as { getBBox: () => DOMRect }).getBBox = () =>
    ({ x: 0, y: 0, width: 0, height: 0 }) as DOMRect;
});

// ---------------------------------------------------------------------------
// Fixtures — shaped exactly like server/core/context_api.py::_get_graph
// ---------------------------------------------------------------------------

const TOPIC: GraphNodeRow = {
  id: "top_1",
  node_type: "topic",
  title: "ROAS calculation policy",
  excerpt: "Return on ad spend is computed on deduplicated conversions.",
  owner: "ann@toorow.com",
  version_number: 2,
  scope: "project",
  status: "active",
};

const PROCEDURE: GraphNodeRow = {
  id: "proc_1",
  node_type: "procedure",
  title: "Weekly performance review",
  excerpt: "Pull the last 7 days, compare against the previous period.",
  owner: "bob@toorow.com",
  version_number: 1,
  scope: "platform",
  status: "active",
};

const SCHEMA_DOC: GraphNodeRow = {
  id: "doc_1",
  node_type: "schema_doc",
  title: "marts.fct_campaign_daily",
  doc_kind: "columns",
  excerpt: "campaign_id, date_day, impressions, clicks, cost_micros.",
  owner: null,
  version_number: 3,
  scope: "project",
  status: "active",
};

const EDGE_EXPLAINS: GraphEdgeRow = {
  id: "edge_1",
  from_id: "top_1",
  to_id: "proc_1",
  from_type: "topic",
  to_type: "procedure",
  edge_type: "explains",
  created_by: "ann@toorow.com",
  created_at: "2026-07-20T10:00:00+00:00",
};

const EDGE_DEPENDS: GraphEdgeRow = {
  id: "edge_2",
  from_id: "proc_1",
  to_id: "doc_1",
  from_type: "procedure",
  to_type: "schema_doc",
  edge_type: "depends_on",
  created_by: "bob@toorow.com",
  created_at: "2026-07-21T10:00:00+00:00",
};

const BUNDLE = {
  nodes: [TOPIC, PROCEDURE, SCHEMA_DOC],
  edges: [EDGE_EXPLAINS, EDGE_DEPENDS],
};

const FULL_TOPIC_BODY =
  "Return on ad spend is computed on deduplicated conversions.\n\n" +
  "## Exclusions\nBrand search is excluded from the blended figure.";

function resp(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function stubFetch(handler: (url: string) => Response | Promise<Response>) {
  const calls: string[] = [];
  const mock = vi.fn((url: string) => {
    calls.push(String(url));
    return Promise.resolve(handler(String(url)));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

/** Only the one-bundle read, not the per-node drawer reads. */
function graphCalls(calls: string[]) {
  return calls.filter((u) => u.startsWith("/api/context/graph?"));
}

const defaultHandler = (url: string) => {
  if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
  if (url.startsWith("/api/context/topics/top_1")) {
    return resp(200, {
      id: "top_1",
      body_md: FULL_TOPIC_BODY,
      updated_at: "2026-07-24T10:00:00+00:00",
    });
  }
  if (url.startsWith("/api/context/procedures/proc_1")) {
    return resp(200, { id: "proc_1", body_md: "Step 1. Open the report.", updated_at: null });
  }
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Pure logic — filters, search, edge options, elk input
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — pure graph logic", () => {
  it("hiding a node type also drops every edge that touched it", () => {
    const out = applyGraphFilters(BUNDLE.nodes, BUNDLE.edges, {
      ...DEFAULT_FILTERS,
      // target_field (Story 44.10) is the fourth key GraphFilterState requires;
      // this fixture has no dictionary node, so its value is immaterial here.
      types: { topic: true, procedure: false, schema_doc: true, target_field: true },
    });
    expect(out.nodes.map((n) => n.id)).toEqual(["top_1", "doc_1"]);
    // Both edges had the procedure as an endpoint — neither may survive dangling.
    expect(out.edges).toEqual([]);
  });

  it("filters by scope and by edge_type independently", () => {
    expect(
      applyGraphFilters(BUNDLE.nodes, BUNDLE.edges, {
        ...DEFAULT_FILTERS,
        scope: "platform",
      }).nodes.map((n) => n.id),
    ).toEqual(["proc_1"]);

    const explains = applyGraphFilters(BUNDLE.nodes, BUNDLE.edges, {
      ...DEFAULT_FILTERS,
      edgeType: "explains",
    });
    expect(explains.nodes).toHaveLength(3);
    expect(explains.edges.map((e) => e.id)).toEqual(["edge_1"]);
  });

  it("searches title and excerpt, case-insensitively, and never on an empty query", () => {
    expect(nodeMatchesQuery(TOPIC, "roas")).toBe(true);
    expect(nodeMatchesQuery(TOPIC, "DEDUPLICATED")).toBe(true);
    expect(nodeMatchesQuery(TOPIC, "cost_micros")).toBe(false);
    expect(nodeMatchesQuery(TOPIC, "   ")).toBe(false);
  });

  it("derives the edge_type options from the payload, sorted and deduplicated", () => {
    expect(edgeTypeOptions([EDGE_EXPLAINS, EDGE_DEPENDS, EDGE_EXPLAINS])).toEqual([
      "depends_on",
      "explains",
    ]);
  });

  // 44.6 folded the 44.4 `toElkGraph`/`layoutGraph` pair into the generic
  // projection pair, of which Domains IS the 44.4 layout — these two tests
  // therefore assert the same contract through the code the page really runs,
  // instead of through a duplicate that only tests could reach.
  it("describes a layered RIGHT elk graph with one child per node", () => {
    const g = toElkGraphForProjection(projectGraph("domains", BUNDLE.nodes, BUNDLE.edges));
    expect(g.layoutOptions["elk.algorithm"]).toBe("layered");
    expect(g.layoutOptions["elk.direction"]).toBe("RIGHT");
    expect(g.children.map((c) => c.id)).toEqual(["top_1", "proc_1", "doc_1"]);
    expect(g.edges).toEqual([
      { id: "edge_1", sources: ["top_1"], targets: ["proc_1"] },
      { id: "edge_2", sources: ["proc_1"], targets: ["doc_1"] },
    ]);
  });

  it("hands React Flow one labelled, arrow-headed edge per link", () => {
    // React Flow renders edge labels into SVG, which jsdom cannot introspect —
    // without this assertion `label` could be dropped and every DOM test would
    // still pass, leaving the map with unnamed relationships.
    expect(toFlowEdges(BUNDLE.edges)).toEqual([
      {
        id: "edge_1",
        source: "top_1",
        target: "proc_1",
        label: "explains",
        type: "smoothstep",
        labelShowBg: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      },
      {
        id: "edge_2",
        source: "proc_1",
        target: "doc_1",
        label: "depends_on",
        type: "smoothstep",
        labelShowBg: false,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      },
    ]);
  });

  it("runs the real elk layout: a linear chain lays out left-to-right", async () => {
    const positions = await layoutProjection(
      "domains",
      projectGraph("domains", BUNDLE.nodes, BUNDLE.edges),
    );
    expect(Object.keys(positions).sort()).toEqual(["doc_1", "proc_1", "top_1"]);
    expect(positions.proc_1.x).toBeGreaterThan(positions.top_1.x);
    expect(positions.doc_1.x).toBeGreaterThan(positions.proc_1.x);
  }, 20000);
});

// ---------------------------------------------------------------------------
// Navigation wiring
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — navigation", () => {
  it("is reachable as Context ▸ Graph", () => {
    expect(WORKSPACE_BY_KEY.context.subnav).toContainEqual({ slug: "graph", label: "Graph" });
  });
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — rendering", () => {
  it("renders one card per node with verbatim excerpt, owner, version and badges", async () => {
    stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    const topicCard = await screen.findByTestId("kg-node-top_1");
    expect(within(topicCard).getByText(TOPIC.excerpt)).toBeInTheDocument();
    expect(within(topicCard).getByText("ann@toorow.com")).toBeInTheDocument();
    expect(within(topicCard).getByText("v2")).toBeInTheDocument();

    const procCard = screen.getByTestId("kg-node-proc_1");
    expect(within(procCard).getByText("Platform")).toBeInTheDocument();

    const docCard = screen.getByTestId("kg-node-doc_1");
    // schema docs are generator-written: an "auto" badge, the doc_kind, and no
    // fabricated owner.
    expect(within(docCard).getByText("auto")).toBeInTheDocument();
    expect(within(docCard).getByText("columns")).toBeInTheDocument();
    expect(within(docCard).getByText("Generated")).toBeInTheDocument();

    // The rendered counter is the page's own view of the laid-out node/edge set.
    expect(screen.getByTestId("kg-count")).toHaveTextContent("3/3 nodes · 2 links");
  }, TEST_TIMEOUT);

  it("reads /api/context/graph exactly once and never refetches on a filter change", async () => {
    const calls = stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-proc_1");
    expect(graphCalls(calls)).toHaveLength(1);

    await user.click(screen.getByTestId("kg-toggle-procedure"));

    await waitFor(() => {
      expect(screen.queryByTestId("kg-node-proc_1")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("kg-node-top_1")).toBeInTheDocument();
    // Both edges touched the procedure, so the visible link count collapses.
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/3 nodes · 0 links");
    // The re-layout happened entirely client-side.
    expect(graphCalls(calls)).toHaveLength(1);
  }, TEST_TIMEOUT);

  it("scope filter keeps only platform nodes without refetching", async () => {
    const calls = stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    await user.selectOptions(screen.getByTestId("kg-scope"), "platform");

    await waitFor(() => {
      expect(screen.queryByTestId("kg-node-top_1")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("kg-node-proc_1")).toBeInTheDocument();
    expect(graphCalls(calls)).toHaveLength(1);
  }, TEST_TIMEOUT);

  it("search highlights matching nodes and dims the rest instead of hiding them", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    await user.type(screen.getByTestId("kg-search"), "deduplicated");

    await waitFor(() => {
      expect(screen.getByTestId("kg-node-top_1")).toHaveAttribute("data-match", "true");
    });
    const procCard = screen.getByTestId("kg-node-proc_1");
    expect(procCard).toHaveAttribute("data-match", "false");
    expect(procCard.className).toContain("is-dim");
    // The match is marked in place, on the verbatim text.
    expect(within(screen.getByTestId("kg-node-top_1")).getByText("deduplicated").tagName).toBe(
      "MARK",
    );
  }, TEST_TIMEOUT);

  it("focuses the search box on '/' ", async () => {
    stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);
    await screen.findByTestId("kg-node-top_1");

    expect(screen.getByTestId("kg-search")).not.toHaveFocus();
    fireEvent.keyDown(document.body, { key: "/" });
    expect(screen.getByTestId("kg-search")).toHaveFocus();
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// Loading / error / empty
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — honest states", () => {
  it("explains a failed load in English and keeps the server envelope as detail", async () => {
    stubFetch(() =>
      resp(500, { code: "db_error", message: "Erreur lors de la récupération du graphe" }),
    );
    render(<KnowledgeGraphPage projectId="p1" />);

    const error = await screen.findByTestId("kg-error");
    // The page is English: the API's French envelope is evidence, not the copy.
    expect(within(error).getByText(/Couldn’t load the knowledge graph/)).toBeInTheDocument();
    expect(error).toHaveTextContent(/didn’t return this project’s knowledge/);
    expect(screen.getByTestId("kg-error-detail")).toHaveTextContent(
      "Erreur lors de la récupération du graphe",
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByTestId("kg-canvas")).not.toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("says so when the filters exclude every node, and can reset them", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    for (const type of ["topic", "procedure", "schema_doc"]) {
      await user.click(screen.getByTestId(`kg-toggle-${type}`));
    }

    // A blank canvas would be indistinguishable from a broken render.
    const overlay = await screen.findByTestId("kg-no-match");
    expect(overlay).toHaveTextContent("No node matches these filters");
    expect(screen.getByTestId("kg-count")).toHaveTextContent("0/3 nodes");

    await user.click(screen.getByTestId("kg-reset-filters"));
    await waitFor(() => {
      expect(screen.getByTestId("kg-node-top_1")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("kg-no-match")).not.toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("renders an empty state with a Knowledge CTA and no fake nodes", async () => {
    stubFetch((url) =>
      url.startsWith("/api/context/graph?")
        ? resp(200, { nodes: [], edges: [] })
        : resp(500, { message: "unexpected" }),
    );
    const onOpenKnowledge = vi.fn();
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" onOpenKnowledge={onOpenKnowledge} />);

    await screen.findByTestId("kg-empty");
    expect(screen.queryByTestId("kg-canvas")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add knowledge" }));
    expect(onOpenKnowledge).toHaveBeenCalledTimes(1);
  }, TEST_TIMEOUT);

  it("warns above 500 nodes but still renders the canvas", async () => {
    const many: GraphNodeRow[] = Array.from({ length: 501 }, (_, i) => ({
      ...TOPIC,
      id: `top_${i}`,
      title: `Topic ${i}`,
    }));
    stubFetch((url) =>
      url.startsWith("/api/context/graph?")
        ? resp(200, { nodes: many, edges: [] })
        : resp(500, { message: "unexpected" }),
    );
    render(<KnowledgeGraphPage projectId="p1" />);

    const warning = await screen.findByTestId("kg-large-warning", undefined, { timeout: 20000 });
    expect(warning).toHaveTextContent("501");
    expect(screen.getByTestId("kg-canvas")).toBeInTheDocument();
  }, 30000);
});

// ---------------------------------------------------------------------------
// Drawer
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — drawer", () => {
  it("fetches and shows the FULL body of a topic, plus its links", async () => {
    const calls = stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));

    const drawer = await screen.findByTestId("kg-drawer");
    await waitFor(() => {
      expect(screen.getByTestId("kg-drawer-body")).toHaveTextContent("Brand search is excluded");
    });
    // The bundle only carried the 280-char excerpt: the full body came from the
    // per-node read, not from the graph payload.
    expect(calls).toContain("/api/context/topics/top_1");
    expect(FULL_TOPIC_BODY.length).toBeGreaterThan(TOPIC.excerpt.length);

    expect(within(drawer).getByText("ann@toorow.com")).toBeInTheDocument();
    expect(within(drawer).getByText("v2")).toBeInTheDocument();
    expect(within(drawer).getByText("Project")).toBeInTheDocument();

    // Outbound: explains -> the procedure. Inbound: none.
    const outbound = within(drawer).getByTestId("kg-outbound");
    expect(within(outbound).getByText("explains")).toBeInTheDocument();
    expect(within(outbound).getByText("Weekly performance review")).toBeInTheDocument();
    expect(within(drawer).queryByTestId("kg-inbound")).not.toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("navigates to the linked node when an edge row is clicked", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    const outbound = await screen.findByTestId("kg-outbound");
    // Story 44.5 added a delete button to each row alongside the nav button,
    // so the row is targeted by its "Open <title>" accessible name.
    await user.click(within(outbound).getByRole("button", { name: /^Open /i }));

    await waitFor(() => {
      expect(
        within(screen.getByTestId("kg-drawer")).getByRole("heading", {
          name: "Weekly performance review",
        }),
      ).toBeInTheDocument();
    });
    // The procedure's own full body was then read from its own route.
    await waitFor(() => {
      expect(screen.getByTestId("kg-drawer-body")).toHaveTextContent("Step 1. Open the report.");
    });
  }, TEST_TIMEOUT);

  it("schema docs: read-only banner, excerpt with a note, and no invented endpoint", async () => {
    const calls = stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-doc_1"));

    const drawer = await screen.findByTestId("kg-drawer");
    expect(within(drawer).getByTestId("kg-readonly-banner")).toHaveTextContent(
      "read-only",
    );
    expect(screen.getByTestId("kg-drawer-body")).toHaveTextContent(SCHEMA_DOC.excerpt);
    expect(within(drawer).getByText(/no single-document read endpoint/i)).toBeInTheDocument();
    // No per-document route exists for schema docs: nothing beyond the bundle
    // read may have been called.
    expect(calls).toHaveLength(1);
    expect(graphCalls(calls)).toHaveLength(1);
  }, TEST_TIMEOUT);

  it("surfaces a drawer read failure instead of showing a stale body", async () => {
    stubFetch((url) => {
      if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
      return resp(404, { code: "not_found", message: "Topic introuvable" });
    });
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));

    expect(await screen.findByTestId("kg-detail-error")).toHaveTextContent("Topic introuvable");
    expect(screen.queryByTestId("kg-drawer-body")).not.toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("closes the drawer when its node is filtered out, and disables hidden peers", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    // Open the procedure's drawer, then hide procedures: the drawer cannot keep
    // describing a node the operator can no longer see on the canvas.
    fireEvent.click(await screen.findByTestId("kg-node-proc_1"));
    await screen.findByTestId("kg-drawer");
    await user.click(screen.getByTestId("kg-toggle-procedure"));
    await waitFor(() => {
      expect(screen.queryByTestId("kg-drawer")).not.toBeInTheDocument();
    });

    // Re-show procedures, hide schema docs, and open the procedure again: its
    // outbound peer doc_1 is out of filter, so the row says so and cannot be
    // clicked (focusing an absent node would silently no-op).
    await user.click(screen.getByTestId("kg-toggle-procedure"));
    await user.click(screen.getByTestId("kg-toggle-schema_doc"));
    fireEvent.click(await screen.findByTestId("kg-node-proc_1"));

    // Story 44.5 added a delete button alongside the nav button in each row,
    // so the nav ("Open <title>") and delete ("Delete link to <title>")
    // buttons are targeted by their distinct accessible names.
    const outbound = await screen.findByTestId("kg-outbound");
    const peer = within(outbound).getByRole("button", { name: /^Open /i });
    expect(peer).toBeDisabled();
    expect(peer).toHaveTextContent("Hidden by current filters");

    // The inbound peer is still visible, so that row stays actionable.
    const inbound = screen.getByTestId("kg-inbound");
    expect(within(inbound).getByRole("button", { name: /^Open /i })).toBeEnabled();
  }, TEST_TIMEOUT);

  it("Escape closes the drawer", async () => {
    stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await screen.findByTestId("kg-drawer");

    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByTestId("kg-drawer")).not.toBeInTheDocument();
    });
  }, TEST_TIMEOUT);
});
