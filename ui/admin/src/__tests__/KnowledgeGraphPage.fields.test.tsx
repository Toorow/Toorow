/**
 * KnowledgeGraphPage — Story 44.10 (dictionary fields as graph nodes).
 *
 * A fourth sibling file rather than more tests in KnowledgeGraphPage.test.tsx /
 * .edit.test.tsx / .projections.test.tsx, per those files' own convention
 * ("prefer the sibling to keep files manageable").
 *
 * Pins:
 *   - a target_field node renders with the Dictionary-field kind label, the
 *     metric/dimension badge and the dictionary node-type class, and its own
 *     type toggle removes it (and the edges that touched it) from the canvas;
 *   - the drawer for a field is read-only, states so, exposes "Open in
 *     Semantic model" (and an honest note when the navigation prop is absent),
 *     and its "Fed by" panel lists the real datastream mappings returned by
 *     GET /api/datamodel/mappings?target_field=…;
 *   - an edge created to a field through the 44.5 modal POSTs
 *     to_type: "target_field" — the payload the migration-117 CHECK accepts.
 *
 * jsdom shim block copied verbatim from KnowledgeGraphPage.edit.test.tsx
 * (React Flow needs ResizeObserver, DOMMatrixReadOnly, offset dimensions,
 * SVGElement.getBBox and elementFromPoint, none of which jsdom implements).
 */
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import KnowledgeGraphPage, {
  buildEdgeCreatePayload,
  DEFAULT_FILTERS,
  NODE_TYPE_LABELS,
  NODE_TYPE_ORDER,
  applyGraphFilters,
  type GraphEdgeRow,
  type GraphNodeRow,
} from "../KnowledgeGraphPage";

// ---------------------------------------------------------------------------
// jsdom shims React Flow requires
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

/** The handle jsdom's elementFromPoint shim reports as being under the pointer. */
let handleUnderPointer: Element | null = null;

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
  (Document.prototype as unknown as { elementFromPoint: () => Element | null }).elementFromPoint =
    () => handleUnderPointer;
  (Document.prototype as unknown as { elementsFromPoint: () => Element[] }).elementsFromPoint =
    () => (handleUnderPointer ? [handleUnderPointer] : []);
});

function clickHandle(el: HTMLElement) {
  handleUnderPointer = el;
  fireEvent.click(el);
}

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

const FIELD_CLICKS: GraphNodeRow = {
  id: "clicks",
  node_type: "target_field",
  title: "Clicks",
  excerpt: "Ad clicks, deduplicated across networks.",
  owner: "ann@toorow.com",
  version_number: 5,
  scope: "platform",
  status: "approved",
  field_kind: "metric",
};

const FIELD_COUNTRY: GraphNodeRow = {
  id: "country",
  node_type: "target_field",
  title: "Country",
  excerpt: "",
  owner: "bob@toorow.com",
  version_number: 1,
  scope: "platform",
  status: "approved",
  field_kind: "dimension",
};

const EDGE_EXPLAINS: GraphEdgeRow = {
  id: "edge_1",
  from_id: "top_1",
  to_id: "clicks",
  from_type: "topic",
  to_type: "target_field",
  edge_type: "explains",
  created_by: "ann@toorow.com",
  created_at: "2026-07-26T10:00:00+00:00",
};

const BUNDLE = { nodes: [TOPIC, FIELD_CLICKS, FIELD_COUNTRY], edges: [EDGE_EXPLAINS] };

const FED_BY = {
  mappings: [
    {
      datastream_id: "ds_meta",
      datastream_name: "Meta Ads — FR",
      module_name: "meta-ads",
      project_id: "p1",
      enabled: true,
      source_field: "inline_link_clicks",
      is_key_column: false,
    },
    {
      datastream_id: "ds_ga4",
      datastream_name: "GA4 — web",
      module_name: "ga4",
      project_id: "p1",
      enabled: true,
      source_field: "sessionClicks",
      is_key_column: false,
    },
  ],
};

function resp(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const mock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return Promise.resolve(handler(String(url), init));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

const defaultHandler = (url: string) => {
  if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
  if (url.startsWith("/api/datamodel/mappings?target_field=clicks")) return resp(200, FED_BY);
  if (url.startsWith("/api/datamodel/mappings?target_field=country")) {
    return resp(200, { mappings: [] });
  }
  if (url.startsWith("/api/context/topics/top_1")) {
    return resp(200, { id: "top_1", body_md: "Full body.", updated_at: null });
  }
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  handleUnderPointer = null;
});

// ---------------------------------------------------------------------------
// Pure logic
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — target_field pure logic", () => {
  it("registers the dictionary as a fourth, English-labelled node type", () => {
    expect(NODE_TYPE_ORDER).toEqual(["topic", "procedure", "schema_doc", "target_field"]);
    expect(NODE_TYPE_LABELS.target_field).toBe("Dictionary field");
    expect(DEFAULT_FILTERS.types.target_field).toBe(true);
  });

  it("hiding the dictionary drops its nodes AND the edges that touched them", () => {
    const out = applyGraphFilters(BUNDLE.nodes, BUNDLE.edges, {
      ...DEFAULT_FILTERS,
      types: { topic: true, procedure: true, schema_doc: true, target_field: false },
    });
    expect(out.nodes.map((n) => n.id)).toEqual(["top_1"]);
    expect(out.edges).toEqual([]);
  });

  it("builds an edge-create body carrying target_field on either end", () => {
    expect(buildEdgeCreatePayload("p1", TOPIC, FIELD_CLICKS, "explains")).toEqual({
      project_id: "p1",
      from_id: "top_1",
      from_type: "topic",
      to_id: "clicks",
      to_type: "target_field",
      edge_type: "explains",
    });
    // Field -> field, the other shape migration 117's widened CHECK allows.
    expect(buildEdgeCreatePayload("p1", FIELD_CLICKS, FIELD_COUNTRY, "relates_to")).toMatchObject({
      from_type: "target_field",
      to_type: "target_field",
    });
  });
});

// ---------------------------------------------------------------------------
// Rendering + type toggle
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — dictionary field nodes on the canvas", () => {
  it(
    "renders a field node with its kind badge, and its own toggle filters it away",
    async () => {
      stubFetch(defaultHandler);
      const user = userEvent.setup();
      render(<KnowledgeGraphPage projectId="p1" />);

      const card = await screen.findByTestId("kg-node-clicks", {}, { timeout: ASYNC_TIMEOUT });
      expect(card).toHaveAttribute("data-node-type", "target_field");
      expect(card.className).toContain("kg-node--target_field");
      expect(within(card).getByText("Dictionary field")).toBeInTheDocument();
      // metric / dimension badge carried from the server payload's field_kind.
      expect(screen.getByTestId("kg-field-kind-clicks")).toHaveTextContent("metric");
      expect(screen.getByTestId("kg-field-kind-country")).toHaveTextContent("dimension");
      expect(screen.getByTestId("kg-count")).toHaveTextContent("3/3 nodes · 1 links");

      await user.click(screen.getByTestId("kg-toggle-target_field"));

      await waitFor(() => {
        expect(screen.queryByTestId("kg-node-clicks")).not.toBeInTheDocument();
      });
      expect(screen.queryByTestId("kg-node-country")).not.toBeInTheDocument();
      expect(screen.getByTestId("kg-node-top_1")).toBeInTheDocument();
      // The topic->field edge went with them: no dangling link on the canvas.
      expect(screen.getByTestId("kg-count")).toHaveTextContent("1/3 nodes · 0 links");
    },
    TEST_TIMEOUT,
  );
});

// ---------------------------------------------------------------------------
// Drawer: read-only summary, Semantic-model action, "Fed by"
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — dictionary field drawer", () => {
  it(
    "shows a read-only summary, the Semantic model action, and the Fed-by rows",
    async () => {
      const calls = stubFetch(defaultHandler);
      const onOpenSemanticModel = vi.fn();
      const user = userEvent.setup();
      render(
        <KnowledgeGraphPage projectId="p1" onOpenSemanticModel={onOpenSemanticModel} />,
      );

      fireEvent.click(
        await screen.findByTestId("kg-node-clicks", {}, { timeout: ASYNC_TIMEOUT }),
      );

      const drawer = await screen.findByTestId("kg-drawer");
      expect(within(drawer).getByTestId("kg-field-readonly-banner")).toBeInTheDocument();
      expect(within(drawer).getByTestId("kg-field-name")).toHaveTextContent("clicks");
      expect(within(drawer).getByTestId("kg-field-kind-meta")).toHaveTextContent("metric");
      // Verbatim description, no invented summary; v5 from the versions table.
      expect(within(drawer).getByTestId("kg-drawer-body")).toHaveTextContent(
        "Ad clicks, deduplicated across networks.",
      );
      expect(within(drawer).getByText("v5")).toBeInTheDocument();

      // No per-node context read is attempted: a field has no /api/context/… doc.
      expect(calls.some((c) => c.url.startsWith("/api/context/topics/clicks"))).toBe(false);
      expect(calls.some((c) => c.url.startsWith("/api/context/procedures/clicks"))).toBe(false);

      // "Fed by" asked the mappings endpoint for THIS field, scoped to the project.
      const fedByCall = calls.find((c) => c.url.startsWith("/api/datamodel/mappings?"));
      expect(fedByCall?.url).toBe("/api/datamodel/mappings?target_field=clicks&project_id=p1");

      const fedBy = await within(drawer).findByTestId("kg-fed-by");
      expect(within(fedBy).getByText("Fed by (2)")).toBeInTheDocument();
      const metaRow = within(fedBy).getByTestId("kg-fed-by-ds_meta-inline_link_clicks");
      expect(metaRow).toHaveTextContent("inline_link_clicks");
      expect(metaRow).toHaveTextContent("Meta Ads — FR");
      expect(metaRow).toHaveTextContent("meta-ads");
      expect(
        within(fedBy).getByTestId("kg-fed-by-ds_ga4-sessionClicks"),
      ).toHaveTextContent("GA4 — web");

      await user.click(within(drawer).getByTestId("kg-drawer-open-semantic-model"));
      expect(onOpenSemanticModel).toHaveBeenCalledTimes(1);
    },
    TEST_TIMEOUT,
  );

  it(
    "says so honestly when nothing feeds the field, and offers no dead button without the nav prop",
    async () => {
      stubFetch(defaultHandler);
      render(<KnowledgeGraphPage projectId="p1" />);

      fireEvent.click(
        await screen.findByTestId("kg-node-country", {}, { timeout: ASYNC_TIMEOUT }),
      );

      const drawer = await screen.findByTestId("kg-drawer");
      expect(await within(drawer).findByTestId("kg-fed-by-empty")).toHaveTextContent(
        /Nothing feeds this field yet/i,
      );
      // No onOpenSemanticModel prop -> a note, never a button that does nothing.
      expect(
        within(drawer).queryByTestId("kg-drawer-open-semantic-model"),
      ).not.toBeInTheDocument();
      expect(
        within(drawer).getByTestId("kg-drawer-open-semantic-model-note"),
      ).toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );

  it(
    "surfaces a Fed-by read failure instead of pretending nothing feeds the field",
    async () => {
      stubFetch((url) => {
        if (url.startsWith("/api/datamodel/mappings?")) {
          return resp(500, { code: "db_error", message: "Erreur de base de donnees" });
        }
        return defaultHandler(url);
      });
      render(<KnowledgeGraphPage projectId="p1" />);

      fireEvent.click(
        await screen.findByTestId("kg-node-clicks", {}, { timeout: ASYNC_TIMEOUT }),
      );

      const drawer = await screen.findByTestId("kg-drawer");
      expect(await within(drawer).findByTestId("kg-fed-by-error")).toHaveTextContent(
        "Erreur de base de donnees",
      );
      expect(within(drawer).queryByTestId("kg-fed-by-empty")).not.toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );
});

// ---------------------------------------------------------------------------
// Edge creation to a field through the 44.5 modal
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — linking a field through the 44.5 modal", () => {
  it(
    "POSTs to_type: 'target_field' when a topic is connected to a dictionary field",
    async () => {
      const calls = stubFetch((url) => {
        if (url === "/api/context/graph/edges") {
          return resp(201, {
            id: "edge_new",
            from_id: "top_1",
            to_id: "country",
            from_type: "topic",
            to_type: "target_field",
            edge_type: "defines",
            created_by: "ann@toorow.com",
            created_at: "2026-07-26T12:00:00+00:00",
          });
        }
        return defaultHandler(url);
      });
      const user = userEvent.setup();
      const { container } = render(<KnowledgeGraphPage projectId="p1" />);

      await screen.findByTestId("kg-node-top_1", {}, { timeout: ASYNC_TIMEOUT });

      const source = container.querySelector('[data-nodeid="top_1"].source') as HTMLElement;
      const target = container.querySelector('[data-nodeid="country"].target') as HTMLElement;
      clickHandle(source);
      clickHandle(target);

      await screen.findByText("Link these two nodes");
      await user.click(screen.getByTestId("kg-edge-suggestion-defines"));
      await user.click(screen.getByTestId("kg-edge-modal-submit"));

      await waitFor(() => {
        expect(screen.queryByText("Link these two nodes")).not.toBeInTheDocument();
      });

      const edgeCall = calls.find((c) => c.url === "/api/context/graph/edges");
      expect(edgeCall).toBeDefined();
      expect(JSON.parse(String(edgeCall!.init!.body))).toEqual({
        project_id: "p1",
        from_id: "top_1",
        from_type: "topic",
        to_id: "country",
        to_type: "target_field",
        edge_type: "defines",
      });

      // The new link is on the map only after the 2xx: 1 seeded + 1 created.
      await waitFor(() => {
        expect(screen.getByTestId("kg-count")).toHaveTextContent("3/3 nodes · 2 links");
      });
    },
    TEST_TIMEOUT,
  );
});
