/**
 * KnowledgeGraphPage — Story 44.6 ("Re-projection Views & Graph Polish").
 *
 * Sibling to KnowledgeGraphPage.test.tsx (44.4/44.5), which keeps owning the
 * read+filter+drawer behaviours. This file pins only the 44.6 additions:
 *   - `projectGraph` is a PURE function per view — asserted directly, no DOM;
 *   - switching views never refetches `/api/context/graph` (client-side only);
 *   - switching views RE-FITS the viewport (AC1) and Tidy re-fits the NEW
 *     layout, not the one it just replaced;
 *   - the MiniMap renders;
 *   - the Tidy button re-runs the layout (elk.layout is called again);
 *   - Up/Down cycles search hits;
 *   - a ~300-node graph still mounts and switches views, and rapid view
 *     clicking coalesces into a single elk run (the 120ms debounce).
 *
 * jsdom shims: identical block to KnowledgeGraphPage.test.tsx — React Flow
 * needs ResizeObserver, DOMMatrixReadOnly, element offset dimensions and
 * SVGElement.getBBox, none of which jsdom implements.
 */
import { configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ELK from "elkjs/lib/elk.bundled.js";
import KnowledgeGraphPage, {
  projectGraph,
  schemaGroupOf,
  gridPositions,
  stripPrivateOptions,
  toElkGraphForProjection,
  type GraphEdgeRow,
  type GraphNodeRow,
} from "../KnowledgeGraphPage";

/** Just enough of the React Flow instance for the viewport assertions. */
type FlowLike = { fitView: (options?: unknown) => unknown };

// ---------------------------------------------------------------------------
// jsdom shims React Flow requires (copied from KnowledgeGraphPage.test.tsx)
// ---------------------------------------------------------------------------

class ResizeObserverStub {
  callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }
  observe(target: Element) {
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

const ASYNC_TIMEOUT = 15000;
const TEST_TIMEOUT = 30000;
configure({ asyncUtilTimeout: ASYNC_TIMEOUT });

beforeAll(() => {
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

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures
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

const SCHEMA_FCT: GraphNodeRow = {
  id: "doc_fct",
  node_type: "schema_doc",
  title: "marts.fct_campaign_daily",
  doc_kind: "columns",
  excerpt: "campaign_id, date_day, impressions, clicks, cost_micros.",
  owner: null,
  version_number: 3,
  scope: "project",
  status: "active",
};

const SCHEMA_DIM: GraphNodeRow = {
  id: "doc_dim",
  node_type: "schema_doc",
  title: "marts.dim_campaign",
  doc_kind: "columns",
  excerpt: "campaign_id, campaign_name, channel.",
  owner: null,
  version_number: 1,
  scope: "project",
  status: "active",
};

const SCHEMA_STG: GraphNodeRow = {
  id: "doc_stg",
  node_type: "schema_doc",
  title: "staging.stg_events",
  doc_kind: "sample_values",
  excerpt: "event_id, event_type, occurred_at.",
  owner: null,
  version_number: 1,
  scope: "project",
  status: "active",
};

/** Never touched by any edge below — the Orphans projection's whole point. */
const ORPHAN_TOPIC: GraphNodeRow = {
  id: "top_orphan",
  node_type: "topic",
  title: "Unlinked glossary note",
  excerpt: "Not referenced by any procedure or schema doc yet.",
  owner: "ann@toorow.com",
  version_number: 1,
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
  to_id: "doc_fct",
  from_type: "procedure",
  to_type: "schema_doc",
  edge_type: "depends_on",
  created_by: "bob@toorow.com",
  created_at: "2026-07-21T10:00:00+00:00",
};

const EDGE_SCHEMA_LINK: GraphEdgeRow = {
  id: "edge_3",
  from_id: "doc_fct",
  to_id: "doc_dim",
  from_type: "schema_doc",
  to_type: "schema_doc",
  edge_type: "references",
  created_by: "bob@toorow.com",
  created_at: "2026-07-22T10:00:00+00:00",
};

const ALL_NODES = [TOPIC, PROCEDURE, SCHEMA_FCT, SCHEMA_DIM, SCHEMA_STG, ORPHAN_TOPIC];
const ALL_EDGES = [EDGE_EXPLAINS, EDGE_DEPENDS, EDGE_SCHEMA_LINK];

const BUNDLE = { nodes: ALL_NODES, edges: ALL_EDGES };

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

const defaultHandler = (url: string) => {
  if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
};

function graphCalls(calls: string[]) {
  return calls.filter((u) => u.startsWith("/api/context/graph?"));
}

// ---------------------------------------------------------------------------
// Pure projections
// ---------------------------------------------------------------------------

describe("projectGraph — pure per-view projections", () => {
  it("Domains: passes the visible set through unchanged, layered RIGHT", () => {
    const out = projectGraph("domains", ALL_NODES, ALL_EDGES);
    expect(out.nodes).toBe(ALL_NODES);
    expect(out.edges).toBe(ALL_EDGES);
    expect(out.elkOptions["elk.direction"]).toBe("RIGHT");
  });

  it("Procedures: reorders procedure nodes first and flags the ordering", () => {
    const out = projectGraph("procedures", ALL_NODES, ALL_EDGES);
    expect(out.nodes[0].node_type).toBe("procedure");
    expect(out.nodes.map((n) => n.id)).toContain("proc_1");
    expect(out.edges).toEqual(ALL_EDGES);
    // The "procedures first" decision is OURS, not an elk option: it is a
    // sibling field on the projection, and nothing `x-…` may reach elk.
    expect(out.proceduresFirst).toBe(true);
    expect(Object.keys(out.elkOptions).some((k) => k.startsWith("x-"))).toBe(false);
    expect(out.elkOptions["elk.direction"]).toBe("DOWN");
  });

  it("never hands elk a private x- option", () => {
    expect(stripPrivateOptions({ "elk.direction": "DOWN", "x-toorow-marker": "true" })).toEqual({
      "elk.direction": "DOWN",
    });
    const graph = toElkGraphForProjection({
      nodes: [PROCEDURE],
      edges: [],
      elkOptions: { "elk.algorithm": "layered", "x-toorow-marker": "true" },
    });
    expect(graph.layoutOptions).toEqual({ "elk.algorithm": "layered" });
  });

  it("Schema: keeps only schema docs, groups them by title prefix, drops dangling edges", () => {
    const out = projectGraph("schema", ALL_NODES, ALL_EDGES);
    expect(out.nodes.map((n) => n.node_type)).toEqual(["schema_doc", "schema_doc", "schema_doc"]);
    // Grouped and sorted: "marts" before "staging", and within "marts" the two
    // docs alphabetically ("dim_campaign" before "fct_campaign_daily").
    expect(out.nodes.map((n) => n.id)).toEqual(["doc_dim", "doc_fct", "doc_stg"]);
    expect(out.groups).toEqual({
      doc_fct: "marts",
      doc_dim: "marts",
      doc_stg: "staging",
    });
    // edge_3 (doc_fct -> doc_dim) is between two schema docs and survives;
    // edge_2 (proc_1 -> doc_fct) would dangle without the procedure, and does not.
    expect(out.edges.map((e) => e.id)).toEqual(["edge_3"]);
  });

  it("schemaGroupOf splits on the first '.' and falls back to the whole title", () => {
    expect(schemaGroupOf("marts.fct_campaign_daily")).toBe("marts");
    expect(schemaGroupOf("no_prefix_title")).toBe("no_prefix_title");
  });

  it("Orphans: only nodes touched by zero edges, on either side", () => {
    const out = projectGraph("orphans", ALL_NODES, ALL_EDGES);
    // doc_stg is deliberately edge-less in the fixture too (the Schema view
    // needs a staging doc) -- BOTH orphans must surface, in ALL_NODES order.
    expect(out.nodes.map((n) => n.id)).toEqual(["doc_stg", "top_orphan"]);
    expect(out.edges).toEqual([]);
  });

  it("gridPositions lays out orphan nodes on a simple grid, no elk involved", () => {
    const positions = gridPositions([ORPHAN_TOPIC, TOPIC, PROCEDURE, SCHEMA_FCT, SCHEMA_DIM], 2);
    expect(positions[ORPHAN_TOPIC.id]).toEqual({ x: 0, y: 0 });
    expect(positions[TOPIC.id].x).toBeGreaterThan(positions[ORPHAN_TOPIC.id].x);
    expect(positions[PROCEDURE.id].y).toBeGreaterThan(positions[ORPHAN_TOPIC.id].y);
  });
});

// ---------------------------------------------------------------------------
// Wired into the page: no refetch, MiniMap, Tidy
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — view switcher wiring", () => {
  it("switches views client-side only — the graph endpoint is never called again", async () => {
    const calls = stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    expect(graphCalls(calls)).toHaveLength(1);

    await user.click(screen.getByTestId("kg-view-procedures"));
    await waitFor(() => {
      expect(screen.getByTestId("kg-view-procedures")).toHaveAttribute("aria-pressed", "true");
    });

    await user.click(screen.getByTestId("kg-view-schema"));
    await waitFor(() => {
      expect(screen.getByTestId("kg-view-schema")).toHaveAttribute("aria-pressed", "true");
    });
    // Schema keeps only the 3 schema docs.
    await waitFor(() => {
      expect(screen.getByTestId("kg-projection-count")).toHaveTextContent("3 shown");
    });

    await user.click(screen.getByTestId("kg-view-orphans"));
    await waitFor(() => {
      // Two orphans in the fixture: top_orphan AND the edge-less doc_stg.
      expect(screen.getByTestId("kg-projection-count")).toHaveTextContent("2 shown");
    });

    // Throughout every view switch, exactly one bundle fetch ever happened.
    expect(graphCalls(calls)).toHaveLength(1);
  }, TEST_TIMEOUT);

  it("renders a themed MiniMap on the canvas", async () => {
    stubFetch(defaultHandler);
    const { container } = render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    await waitFor(() => {
      expect(container.querySelector(".react-flow__minimap")).toBeInTheDocument();
    });
  }, TEST_TIMEOUT);

  it("Tidy re-runs the layout (elk.layout is invoked again) and re-fits AFTER the new positions are committed", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    // Spying on the shared elk CLASS's prototype catches the call regardless
    // of whether the page invokes it via its own module-local `elk` instance
    // — a same-module `vi.spyOn` on an exported function would not.
    const layoutSpy = vi.spyOn(ELK.prototype, "layout");
    const flowRef: { current?: FlowLike } = {};

    render(
      <KnowledgeGraphPage
        projectId="p1"
        onFlowInit={(instance) => {
          flowRef.current = instance as unknown as FlowLike;
        }}
      />,
    );
    await screen.findByTestId("kg-node-top_1");

    await waitFor(() => {
      expect(layoutSpy).toHaveBeenCalled();
    });
    await waitFor(() => expect(flowRef.current).toBeTruthy());
    const callsBeforeTidy = layoutSpy.mock.calls.length;
    const fitSpy = vi.spyOn(flowRef.current as FlowLike, "fitView");

    await user.click(screen.getByTestId("kg-tidy"));

    await waitFor(() => {
      expect(layoutSpy.mock.calls.length).toBeGreaterThan(callsBeforeTidy);
    });
    // Fitting inside relayout()'s own `.then()` would fit the layout React has
    // not committed yet; the fit must land after the commit.
    await waitFor(() => {
      expect(fitSpy).toHaveBeenCalled();
    });
    expect(layoutSpy.mock.invocationCallOrder[layoutSpy.mock.calls.length - 1]).toBeLessThan(
      fitSpy.mock.invocationCallOrder[0],
    );
  }, TEST_TIMEOUT);

  it("switching view re-fits the viewport (AC1), a filter toggle does not", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    const flowRef: { current?: FlowLike } = {};

    render(
      <KnowledgeGraphPage
        projectId="p1"
        onFlowInit={(instance) => {
          flowRef.current = instance as unknown as FlowLike;
        }}
      />,
    );
    await screen.findByTestId("kg-node-top_1");
    await waitFor(() => expect(flowRef.current).toBeTruthy());
    const fitSpy = vi.spyOn(flowRef.current as FlowLike, "fitView");

    // A filter toggle re-lays out, but must NOT yank the viewport: the
    // operator is narrowing the map and expects to stay where they are.
    await user.click(screen.getByTestId("kg-toggle-schema_doc"));
    await waitFor(() => {
      expect(screen.getByTestId("kg-projection-count")).toHaveTextContent("3 shown");
    });
    expect(fitSpy).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("kg-view-procedures"));
    await waitFor(() => {
      expect(fitSpy).toHaveBeenCalled();
    });
  }, TEST_TIMEOUT);

  it("Up/Down cycles the search hits without the search box holding focus", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    const flowRef: { current?: FlowLike } = {};

    render(
      <KnowledgeGraphPage
        projectId="p1"
        onFlowInit={(instance) => {
          flowRef.current = instance as unknown as FlowLike;
        }}
      />,
    );
    await screen.findByTestId("kg-node-top_1");
    await waitFor(() => expect(flowRef.current).toBeTruthy());

    // "campaign" matches exactly the two marts schema docs, in projection
    // order: doc_fct first, then doc_dim.
    await user.type(screen.getByTestId("kg-search"), "campaign");
    const fitSpy = vi.spyOn(flowRef.current as FlowLike, "fitView");
    await waitFor(() => {
      expect(screen.getByTestId("kg-node-doc_fct")).toHaveAttribute("data-match", "true");
    });

    // Fired on the BODY, not the search input: cycling used to require the
    // search box to still hold focus, which it rarely does by then.
    fireEvent.keyDown(document.body, { key: "ArrowDown" });

    await waitFor(() => {
      expect(fitSpy).toHaveBeenCalledWith(
        expect.objectContaining({ nodes: [{ id: "doc_dim" }] }),
      );
    });
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// Scale smoke — a corpus-sized graph, and the debounce that keeps it usable
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — scale", () => {
  const MANY: GraphNodeRow[] = Array.from({ length: 300 }, (_, i) => ({
    ...TOPIC,
    id: `top_${i}`,
    title: `Topic ${i}`,
  }));

  it("mounts ~300 nodes, switches views, and coalesces rapid view clicks into one elk run", async () => {
    stubFetch((url) =>
      url.startsWith("/api/context/graph?")
        ? resp(200, { nodes: MANY, edges: [] })
        : resp(500, { message: "unexpected" }),
    );
    const layoutSpy = vi.spyOn(ELK.prototype, "layout");
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_0", undefined, { timeout: 30000 });
    await waitFor(() => {
      expect(screen.getByTestId("kg-projection-count")).toHaveTextContent("300 shown");
    });
    const runsAfterMount = layoutSpy.mock.calls.length;

    // Three clicks inside the 120ms debounce window: only the LAST projection
    // may reach elk, otherwise every impatient click costs a full layout of
    // the whole corpus. Undebounced, all three would already have fired by the
    // time the first assertion below polls.
    fireEvent.click(screen.getByTestId("kg-view-schema"));
    fireEvent.click(screen.getByTestId("kg-view-orphans"));
    fireEvent.click(screen.getByTestId("kg-view-procedures"));

    expect(screen.getByTestId("kg-view-procedures")).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => {
      expect(layoutSpy.mock.calls.length).toBeGreaterThan(runsAfterMount);
    });
    expect(layoutSpy.mock.calls.length - runsAfterMount).toBe(1);
    await waitFor(() => {
      expect(screen.getByTestId("kg-projection-count")).toHaveTextContent("300 shown");
    });
  }, 60000);

  it("still warns above 500 nodes at 501, and still draws the canvas", async () => {
    // elk is stubbed here on purpose: this test is about the threshold copy,
    // not about laying 501 cards out (the layout itself is covered above).
    vi.spyOn(ELK.prototype, "layout").mockResolvedValue({ id: "root", children: [] } as never);
    stubFetch((url) =>
      url.startsWith("/api/context/graph?")
        ? resp(200, {
            nodes: Array.from({ length: 501 }, (_, i) => ({
              ...TOPIC,
              id: `top_${i}`,
              title: `Topic ${i}`,
            })),
            edges: [],
          })
        : resp(500, { message: "unexpected" }),
    );
    render(<KnowledgeGraphPage projectId="p1" />);

    const warning = await screen.findByTestId("kg-large-warning", undefined, { timeout: 30000 });
    expect(warning).toHaveTextContent("501");
    expect(screen.getByTestId("kg-canvas")).toBeInTheDocument();
  }, 60000);
});
