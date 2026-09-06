/**
 * KnowledgeGraphPage.edit.test.tsx — Story 44.5 ("Graph Editing — Edges and
 * Quick Actions"), kept as a SIBLING of KnowledgeGraphPage.test.tsx (44.4/44.6)
 * rather than folded in, per that file's own header: "prefer the sibling to
 * keep files manageable".
 *
 * Pins:
 *   - the edge-create payload/connection-resolution PURE logic (exact POST
 *     body, and "never resolve a dangling connection into a modal");
 *   - a real onConnect round-trip driven through React Flow's own click-to-
 *     connect handle affordance (not a mock of the library) -> modal ->
 *     POST -> the new edge appears in the rendered link count, with NO
 *     phantom edge added before the 2xx;
 *   - edge SELECT (click on the rendered `rf__edge-*` element React Flow
 *     itself exposes) + Delete key -> confirm -> DELETE -> removed, with the
 *     question AND any refusal rendered on the canvas (that path has no drawer
 *     open to speak through);
 *   - React Flow's own Backspace shortcut cannot remove anything locally;
 *   - Escape backs out exactly ONE surface (the editor, not the drawer under it);
 *   - the topic editor previews the Markdown, and an EMPTY project can reach it;
 *   - the drawer's per-row delete button -> confirm -> DELETE -> removed;
 *   - a 404 on delete surfaces "not permitted" and leaves the edge in place;
 *   - "New topic here" from the canvas context menu -> create modal -> POST
 *     -> the new node is inserted (found by its own `kg-node-*` testid).
 *
 * jsdom shim block is copied verbatim from KnowledgeGraphPage.test.tsx (same
 * comment there explains why): React Flow needs ResizeObserver,
 * DOMMatrixReadOnly, element offset dimensions and SVGElement.getBBox, none of
 * which jsdom implements.
 */
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import KnowledgeGraphPage, {
  buildEdgeCreatePayload,
  isPermissionDenied,
  resolveConnection,
  type GraphEdgeRow,
  type GraphNodeRow,
} from "../KnowledgeGraphPage";

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
  // React Flow's connect-on-click validation calls doc.elementFromPoint(...)
  // (isValidHandle) which jsdom does not implement -- without this shim the
  // click throws an uncaught TypeError and the edge-creation tests never see
  // the modal. The shim returns whatever handle the test is about to click
  // (set via clickHandle below).
  (Document.prototype as unknown as { elementFromPoint: () => Element | null }).elementFromPoint =
    () => handleUnderPointer;
  (Document.prototype as unknown as { elementsFromPoint: () => Element[] }).elementsFromPoint =
    () => (handleUnderPointer ? [handleUnderPointer] : []);
});

/** The handle jsdom's elementFromPoint shim reports as being under the pointer. */
let handleUnderPointer: Element | null = null;

/** Click a React Flow handle with the elementFromPoint shim pointed at it. */
function clickHandle(el: HTMLElement) {
  handleUnderPointer = el;
  fireEvent.click(el);
}

// ---------------------------------------------------------------------------
// Fixtures — same shape as server/core/context_api.py::_get_graph
// ---------------------------------------------------------------------------

const TOPIC: GraphNodeRow = {
  id: "top_1",
  node_type: "topic",
  title: "ROAS calculation policy",
  excerpt: "Return on ad spend is computed on deduplicated conversions.",
  owner: "ann@toorow.com",
  owner_raw: "ann@toorow.com",
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

const BUNDLE = { nodes: [TOPIC, PROCEDURE], edges: [EDGE_EXPLAINS] };

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

/** The scope rides the QUERY on the create call, as on every other context
 *  route. `POST /graph/edges` used to read it from the body alone, so the
 *  server ignored the query, made the edge PLATFORM-scoped, and the
 *  deny-by-default platform gate answered 404 on a project the caller owns
 *  (live finding F3). The body still repeats it — pinned below. */
const EDGE_CREATE_URL = "/api/context/graph/edges?project_id=p1";

const defaultHandler = (url: string) => {
  if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
  if (url.startsWith("/api/context/graph/edges/")) {
    // Success path for edge deletion (44.4 re-review: without this branch the
    // DELETE fell into the 500 catch-all and both success-path tests were red).
    return resp(204, null);
  }
  if (url.startsWith("/api/context/topics/top_1")) {
    return resp(200, { id: "top_1", body_md: "Full body.", updated_at: null });
  }
  if (url.startsWith("/api/context/procedures/proc_1")) {
    return resp(200, { id: "proc_1", body_md: "Step 1.", updated_at: null });
  }
  // AI-158 : le tiroir lit la file des remarques du noeud selectionne. Vide par
  // defaut -- les tests qui en veulent une la posent eux-memes.
  if (url.startsWith("/api/context/review-requests")) {
    return resp(200, { requests: [], can_resolve: true });
  }
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
};

/** Une remarque en file, telle que `GET /api/context/review-requests` la rend. */
const REMARK = {
  id: "crr_1",
  node_id: "top_1",
  node_type: "topic" as const,
  node_version: 1,
  note: "This constraint no longer exists, it should be removed.",
  requested_by: "ann@example.com",
  origin: "human" as const,
  status: "open" as const,
  created_at: "2026-08-04T10:00:00+00:00",
  proposed_change: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Pure logic
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — edge-create pure logic", () => {
  it("builds the exact POST body for /api/context/graph/edges", () => {
    expect(buildEdgeCreatePayload("p1", TOPIC, PROCEDURE, "explains")).toEqual({
      project_id: "p1",
      from_id: "top_1",
      from_type: "topic",
      to_id: "proc_1",
      to_type: "procedure",
      edge_type: "explains",
    });
  });

  it("trims the edge_type before it reaches the server", () => {
    expect(buildEdgeCreatePayload("p1", TOPIC, PROCEDURE, "  depends_on  ").edge_type).toBe(
      "depends_on",
    );
  });

  it("never resolves a connection touching an id outside the known node set", () => {
    const nodeById = new Map([[TOPIC.id, TOPIC], [PROCEDURE.id, PROCEDURE]]);
    expect(resolveConnection({ source: "top_1", target: "proc_1" }, nodeById)).toEqual({
      fromNode: TOPIC,
      toNode: PROCEDURE,
    });
    expect(resolveConnection({ source: "top_1", target: "ghost" }, nodeById)).toBeNull();
    expect(resolveConnection({ source: null, target: "proc_1" }, nodeById)).toBeNull();
  });

  it("treats 404 and 403 as permission denials, nothing else", () => {
    expect(isPermissionDenied(404)).toBe(true);
    expect(isPermissionDenied(403)).toBe(true);
    expect(isPermissionDenied(422)).toBe(false);
    expect(isPermissionDenied(500)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Edge creation — real onConnect via React Flow's own click-to-connect handles
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — edge creation", () => {
  it("onConnect opens the edge_type modal, and a suggestion + Create link POSTs the pinned body, updating the count with no phantom edge before the response", async () => {
    const calls = stubFetch((url) => {
      if (url === EDGE_CREATE_URL) {
        return resp(201, {
          id: "edge_new",
          from_id: "top_1",
          to_id: "proc_1",
          from_type: "topic",
          to_type: "procedure",
          edge_type: "depends_on",
          created_by: "ann@toorow.com",
          created_at: "2026-07-26T00:00:00+00:00",
        });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    const { container } = render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");

    // Click-to-connect (React Flow's own handle affordance, connectOnClick):
    // click the source handle of top_1, then the target handle of proc_1.
    const sourceHandle = container.querySelector('[data-nodeid="top_1"].source') as HTMLElement;
    const targetHandle = container.querySelector('[data-nodeid="proc_1"].target') as HTMLElement;
    expect(sourceHandle).toBeTruthy();
    expect(targetHandle).toBeTruthy();
    clickHandle(sourceHandle);
    clickHandle(targetHandle);

    const heading = await screen.findByText("Link these two nodes");
    expect(heading).toBeInTheDocument();

    // No POST fired yet, and the count is unchanged: opening the modal alone
    // must never touch the network or the rendered edge set.
    expect(calls.filter((c) => c.url === EDGE_CREATE_URL)).toHaveLength(0);
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");

    await user.click(screen.getByTestId("kg-edge-suggestion-depends_on"));
    await user.click(screen.getByTestId("kg-edge-modal-submit"));

    await waitFor(() => {
      expect(screen.queryByText("Link these two nodes")).not.toBeInTheDocument();
    });

    const edgeCall = calls.find((c) => c.url === EDGE_CREATE_URL);
    expect(edgeCall).toBeDefined();
    expect(JSON.parse(String(edgeCall!.init!.body))).toEqual({
      project_id: "p1",
      from_id: "top_1",
      from_type: "topic",
      to_id: "proc_1",
      to_type: "procedure",
      edge_type: "depends_on",
    });

    // Appended to local state — no refetch of the bundle.
    await waitFor(() => {
      expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 2 links");
    });
    expect(calls.filter((c) => c.url.startsWith("/api/context/graph?"))).toHaveLength(1);
  }, TEST_TIMEOUT);

  it("keeps no phantom edge when the server rejects the link (422/404)", async () => {
    stubFetch((url) => {
      if (url === EDGE_CREATE_URL) {
        return resp(404, { code: "not_found", message: "Projet introuvable" });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    const { container } = render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    const sourceHandle = container.querySelector('[data-nodeid="top_1"].source') as HTMLElement;
    const targetHandle = container.querySelector('[data-nodeid="proc_1"].target') as HTMLElement;
    clickHandle(sourceHandle);
    clickHandle(targetHandle);

    await screen.findByText("Link these two nodes");
    await user.click(screen.getByTestId("kg-edge-suggestion-relates_to"));
    await user.click(screen.getByTestId("kg-edge-modal-submit"));

    expect(await screen.findByTestId("kg-edge-modal-error")).toHaveTextContent(
      /not permitted/i,
    );
    // The modal stays open on a rejection (draft preserved) and the count
    // never moved: no edge was added before the 2xx.
    expect(screen.getByText("Link these two nodes")).toBeInTheDocument();
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// Edge deletion
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — edge deletion", () => {
  it("select via click on the rendered edge + Delete key asks once, then deletes on the second press", async () => {
    const calls = stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");

    const edgeEl = await screen.findByTestId("rf__edge-edge_1");
    fireEvent.click(edgeEl);
    fireEvent.keyDown(document.body, { key: "Delete" });
    // First press only asks for confirmation — no DELETE yet.
    expect(calls.some((c) => c.url === "/api/context/graph/edges/edge_1")).toBe(false);

    fireEvent.keyDown(document.body, { key: "Delete" });

    await waitFor(() => {
      expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 0 links");
    });
    const deleteCall = calls.find((c) => c.url === "/api/context/graph/edges/edge_1");
    expect(deleteCall?.init?.method).toBe("DELETE");
  }, TEST_TIMEOUT);

  it("drawer row delete button: confirm inline, then DELETE removes it", async () => {
    const calls = stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    const outbound = await screen.findByTestId("kg-outbound");

    await user.click(within(outbound).getByTestId("kg-edge-delete-edge_1"));
    // Inline confirm replaces the delete icon — the link is not gone yet.
    expect(within(outbound).getByTestId("kg-edge-delete-confirm-edge_1")).toBeInTheDocument();
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");

    await user.click(within(outbound).getByTestId("kg-edge-delete-confirm-edge_1"));

    await waitFor(() => {
      expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 0 links");
    });
    expect(calls.some((c) => c.url === "/api/context/graph/edges/edge_1" && c.init?.method === "DELETE")).toBe(
      true,
    );
  }, TEST_TIMEOUT);

  it("asks its question ON THE CANVAS — the Delete-key path has no drawer to speak through", async () => {
    stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    fireEvent.click(await screen.findByTestId("rf__edge-edge_1"));
    // Selecting an edge does not open the drawer: without a canvas-level bar
    // the confirm state would exist only in memory, invisible to the operator.
    expect(screen.queryByTestId("kg-drawer")).not.toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "Delete" });

    const bar = await screen.findByTestId("kg-edge-confirm-bar");
    expect(bar).toHaveTextContent(
      "Remove link “explains” between ROAS calculation policy and Weekly performance review?",
    );
    expect(bar).toHaveTextContent("Press Delete again to confirm");

    // Esc disarms it — and, being the single Escape owner, does only that.
    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByTestId("kg-edge-confirm-bar")).not.toBeInTheDocument();
    });
  }, TEST_TIMEOUT);

  it("a refusal on the keyboard path is reported on the canvas too", async () => {
    stubFetch((url, init) => {
      if (url === "/api/context/graph/edges/edge_1" && init?.method === "DELETE") {
        return resp(404, { code: "not_found", message: "Liaison introuvable" });
      }
      return defaultHandler(url);
    });
    render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    fireEvent.click(await screen.findByTestId("rf__edge-edge_1"));
    fireEvent.keyDown(document.body, { key: "Delete" });
    await screen.findByTestId("kg-edge-confirm-bar");
    fireEvent.keyDown(document.body, { key: "Delete" });

    expect(await screen.findByTestId("kg-edge-confirm-bar-error")).toHaveTextContent(
      /not permitted/i,
    );
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");
  }, TEST_TIMEOUT);

  it("React Flow's own Backspace shortcut cannot remove anything locally", async () => {
    const calls = stubFetch(defaultHandler);
    render(<KnowledgeGraphPage projectId="p1" />);

    const node = await screen.findByTestId("kg-node-top_1");
    fireEvent.click(node);
    await screen.findByTestId("kg-drawer");

    // The library's default deleteKeyCode is "Backspace", and it removes nodes
    // and edges from its own store with NO server call — a canvas that lies.
    fireEvent.keyDown(document.body, { key: "Backspace" });

    await waitFor(() => {
      expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");
    });
    expect(screen.getByTestId("kg-node-top_1")).toBeInTheDocument();
    expect(screen.getByTestId("kg-node-proc_1")).toBeInTheDocument();
    expect(calls.some((c) => c.init?.method === "DELETE")).toBe(false);
  }, TEST_TIMEOUT);

  it("a 404 on delete is a non-destructive 'not permitted', not a crash — the edge stays", async () => {
    stubFetch((url, init) => {
      if (url === "/api/context/graph/edges/edge_1" && init?.method === "DELETE") {
        return resp(404, { code: "not_found", message: "Liaison introuvable" });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    const outbound = await screen.findByTestId("kg-outbound");
    await user.click(within(outbound).getByTestId("kg-edge-delete-edge_1"));
    await user.click(within(outbound).getByTestId("kg-edge-delete-confirm-edge_1"));

    expect(await screen.findByTestId("kg-edge-error-edge_1")).toHaveTextContent(/not permitted/i);
    // The link is still there: no destructive change happened.
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// Drawer edit (topic) — same editor pattern, in place, no full reload
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — drawer topic edit", () => {
  it("edits a topic from the drawer and shows the bumped version without a full reload", async () => {
    const calls = stubFetch((url, init) => {
      if (url === "/api/context/topics/top_1" && init?.method === "PATCH") {
        return resp(200, {
          id: "top_1",
          project_id: "p1",
          title: "ROAS policy (revised)",
          body_md: "Updated body.",
          status: "active",
          created_by: "ann@toorow.com",
          version_number: 3,
        });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await waitFor(() => expect(screen.getByTestId("kg-drawer-edit")).toBeEnabled());
    await user.click(screen.getByTestId("kg-drawer-edit"));

    const titleInput = await screen.findByTestId("kg-topic-editor-title");
    expect(titleInput).toHaveValue("ROAS calculation policy");
    await user.clear(titleInput);
    await user.type(titleInput, "ROAS policy (revised)");
    await user.click(screen.getByTestId("kg-topic-editor-submit"));

    await waitFor(() => {
      expect(screen.queryByTestId("kg-topic-editor-title")).not.toBeInTheDocument();
    });

    const patchCall = calls.find((c) => c.url === "/api/context/topics/top_1" && c.init?.method === "PATCH");
    expect(patchCall).toBeDefined();
    expect(JSON.parse(String(patchCall!.init!.body))).toEqual({
      title: "ROAS policy (revised)",
      body_md: "Full body.",
      // The owner input pre-fills from the node's owner_raw and is
      // unchanged in this test -- it round-trips verbatim (Story 44.11).
      owner: "ann@toorow.com",
    });

    // The node card reflects the bumped version without a fresh /graph fetch.
    await waitFor(() => {
      expect(within(screen.getByTestId("kg-node-top_1")).getByText("v3")).toBeInTheDocument();
    });
    expect(calls.filter((c) => c.url.startsWith("/api/context/graph?"))).toHaveLength(1);
  }, TEST_TIMEOUT);

  it("shows a live preview of the Markdown being edited", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await waitFor(() => expect(screen.getByTestId("kg-drawer-edit")).toBeEnabled());
    await user.click(screen.getByTestId("kg-drawer-edit"));

    // The editor claims parity with Story 44.1's (title + Markdown + preview).
    const preview = await screen.findByTestId("kg-topic-editor-preview");
    expect(preview).toHaveTextContent("Full body.");
    await user.type(screen.getByTestId("kg-topic-editor-body"), " More.");
    await waitFor(() => {
      expect(screen.getByTestId("kg-topic-editor-preview")).toHaveTextContent("Full body. More.");
    });
  }, TEST_TIMEOUT);

  it("Escape closes the editor it was aimed at, and NOT the drawer underneath", async () => {
    stubFetch(defaultHandler);
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await waitFor(() => expect(screen.getByTestId("kg-drawer-edit")).toBeEnabled());
    await user.click(screen.getByTestId("kg-drawer-edit"));
    await screen.findByTestId("kg-topic-editor-title");

    fireEvent.keyDown(document.body, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByTestId("kg-topic-editor-title")).not.toBeInTheDocument();
    });
    // One Escape, one surface: several listeners used to fire at once and the
    // drawer disappeared along with the modal the operator was dismissing.
    expect(screen.getByTestId("kg-drawer")).toBeInTheDocument();
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// Story 44.11: owner editing + Request review
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — owner editing (Story 44.11)", () => {
  it("pre-fills the Owner input from owner_raw and sends the typed value on PATCH", async () => {
    const calls = stubFetch((url, init) => {
      if (url === "/api/context/topics/top_1" && init?.method === "PATCH") {
        return resp(200, {
          id: "top_1",
          project_id: "p1",
          title: "ROAS calculation policy",
          body_md: "Full body.",
          status: "active",
          owner: "carla@toorow.com",
          created_by: "ann@toorow.com",
          version_number: 3,
        });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await waitFor(() => expect(screen.getByTestId("kg-drawer-edit")).toBeEnabled());
    await user.click(screen.getByTestId("kg-drawer-edit"));

    const ownerInput = await screen.findByTestId("kg-topic-editor-owner");
    expect(ownerInput).toHaveValue("ann@toorow.com");
    await user.clear(ownerInput);
    await user.type(ownerInput, "carla@toorow.com");
    await user.click(screen.getByTestId("kg-topic-editor-submit"));

    await waitFor(() => {
      expect(screen.queryByTestId("kg-topic-editor-title")).not.toBeInTheDocument();
    });

    const patchCall = calls.find(
      (c) => c.url === "/api/context/topics/top_1" && c.init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall!.init!.body))).toEqual({
      title: "ROAS calculation policy",
      body_md: "Full body.",
      owner: "carla@toorow.com",
    });
  }, TEST_TIMEOUT);

  it("keeps the Owner input EMPTY when owner_raw is null — a created_by fallback is never silently promoted to an explicit owner", async () => {
    // 44.11 re-review: the pre-fill test above passes identically whether the
    // component reads `owner` (resolved) or `owner_raw` (explicit). This
    // fixture splits them: resolved owner shows ann@ but owner_raw is null,
    // so the input MUST be empty and an unchanged submit MUST send owner: null.
    const nodeWithFallbackOwner = {
      ...TOPIC,
      owner: "ann@toorow.com", // resolved display value (fallback to created_by)
      owner_raw: null, // no EXPLICIT owner was ever set
    };
    const calls = stubFetch((url, init) => {
      if (url.startsWith("/api/context/graph?")) {
        return resp(200, { nodes: [nodeWithFallbackOwner, PROCEDURE], edges: [EDGE_EXPLAINS] });
      }
      if (url === "/api/context/topics/top_1" && init?.method === "PATCH") {
        return resp(200, {
          id: "top_1",
          title: "ROAS calculation policy",
          body_md: "Full body.",
          owner: null,
          updated_at: "2026-07-26T12:00:00+00:00",
          version_number: 3,
        });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await waitFor(() => expect(screen.getByTestId("kg-drawer-edit")).toBeEnabled());
    await user.click(screen.getByTestId("kg-drawer-edit"));

    const ownerInput = await screen.findByTestId("kg-topic-editor-owner");
    expect(ownerInput).toHaveValue("");
    await user.click(screen.getByTestId("kg-topic-editor-submit"));

    await waitFor(() => {
      expect(screen.queryByTestId("kg-topic-editor-title")).not.toBeInTheDocument();
    });
    const patchCall = calls.find(
      (c) => c.url === "/api/context/topics/top_1" && c.init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall!.init!.body)).owner).toBeNull();
  }, TEST_TIMEOUT);
});

const REVIEW_URL = "/api/context/nodes/top_1/request-review?project_id=p1";

describe("KnowledgeGraphPage — Request review (Story 44.11 + AI-157)", () => {
  it("POSTs node_type + note WITH the project scope, and shows the honest copy", async () => {
    const calls = stubFetch((url, init) => {
      if (url === REVIEW_URL && init?.method === "POST") {
        return resp(201, { status: "requested", request: { id: "crr_1", node_version: 3 } });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await user.click(await screen.findByTestId("kg-drawer-request-review"));

    const noteInput = await screen.findByTestId("kg-review-modal-note");
    await user.type(noteInput, "Please double-check this figure.");
    await user.click(screen.getByTestId("kg-review-modal-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("kg-review-modal-success")).toHaveTextContent(
        /queued against this node/i,
      );
    });
    // Never implies a message/notification was sent to anyone.
    expect(screen.getByTestId("kg-review-modal-success")).not.toHaveTextContent(/notif|email sent/i);

    // AI-157 : `project_id` est la SEULE source de l'organisation que la file
    // exige. Sans lui la route rend 422, et la remarque ne part nulle part.
    const postCall = calls.find((c) => c.url === REVIEW_URL && c.init?.method === "POST");
    expect(postCall).toBeDefined();
    expect(JSON.parse(String(postCall!.init!.body))).toEqual({
      node_type: "topic",
      note: "Please double-check this figure.",
    });
  }, TEST_TIMEOUT);

  it("refuses to send an empty note rather than letting the server 422", async () => {
    const calls = stubFetch((url) => defaultHandler(url));
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await user.click(await screen.findByTestId("kg-drawer-request-review"));

    // La file refuse une note vide (`length(btrim(note)) >= 1`) : une remarque
    // muette ne peut ni se verifier ni se clore.
    expect(await screen.findByTestId("kg-review-modal-submit")).toBeDisabled();
    expect(calls.some((c) => c.url.startsWith("/api/context/nodes/top_1/request-review"))).toBe(
      false,
    );
  }, TEST_TIMEOUT);

  it("shows the server's exact error and keeps the modal open on failure", async () => {
    stubFetch((url, init) => {
      if (url === REVIEW_URL && init?.method === "POST") {
        return resp(500, { code: "db_error", message: "Erreur lors de la demande de revue" });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await user.click(await screen.findByTestId("kg-drawer-request-review"));
    await user.type(await screen.findByTestId("kg-review-modal-note"), "look at this");
    await user.click(screen.getByTestId("kg-review-modal-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("kg-review-modal-error")).toHaveTextContent(
        "Erreur lors de la demande de revue",
      );
    });
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// AI-158 — la file se VOIT, la ou on la remplit
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — open remarks on a node (AI-158)", () => {
  it("lists the node's open remarks with the version each one speaks of", async () => {
    const calls = stubFetch((url) => {
      if (url.startsWith("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: true });
      }
      return defaultHandler(url);
    });
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));

    expect(await screen.findByTestId("kg-review-request-crr_1")).toHaveTextContent(
      "This constraint no longer exists",
    );
    expect(screen.getByTestId("kg-review-queue")).toHaveTextContent("Open remarks (1)");
    // Scoped to the node AND the project -- the queue of one node, not the org's.
    expect(
      calls.some(
        (c) =>
          c.url.includes("/api/context/review-requests") &&
          c.url.includes("project_id=p1") &&
          c.url.includes("node_id=top_1"),
      ),
    ).toBe(true);
  }, TEST_TIMEOUT);

  it("flags a remark that speaks of an older version than the node's current one", async () => {
    stubFetch((url) => {
      if (url.startsWith("/api/context/review-requests")) {
        // TOPIC is v2 in the bundle; the remark was raised against v1.
        return resp(200, { requests: [{ ...REMARK, node_version: 1 }], can_resolve: true });
      }
      return defaultHandler(url);
    });
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));

    // « Cette contrainte n'existe plus » ne veut rien dire si on ignore de
    // quelle version on parle -- et si le noeud a avance depuis, ça se voit.
    expect(await screen.findByTestId("kg-review-stale-crr_1")).toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("closes a remark and re-reads the queue rather than guessing the result", async () => {
    let resolved = false;
    const calls = stubFetch((url, init) => {
      if (url.startsWith("/api/context/review-requests/crr_1/resolve") && init?.method === "POST") {
        resolved = true;
        return resp(200, { ...REMARK, status: "accepted", resolved_by: "me" });
      }
      if (url.startsWith("/api/context/review-requests")) {
        return resp(200, { requests: resolved ? [] : [REMARK], can_resolve: true });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await user.click(await screen.findByTestId("kg-review-accept-crr_1"));

    await waitFor(() => {
      expect(screen.getByTestId("kg-review-queue-empty")).toBeInTheDocument();
    });
    const body = JSON.parse(
      String(
        calls.find((c) => c.url.includes("/resolve") && c.init?.method === "POST")!.init!.body,
      ),
    );
    expect(body).toEqual({ status: "accepted" });
    // Une acceptation peut POSER un lien en base : la vue RELIT plutot que de
    // deviner ce que le serveur a fait.
    expect(
      calls.filter((c) => c.url.includes("/api/context/review-requests?")).length,
    ).toBeGreaterThan(1);
  }, TEST_TIMEOUT);

  it("offers no Accept/Decline when the server says the caller cannot resolve", async () => {
    stubFetch((url) => {
      if (url.startsWith("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: false });
      }
      return defaultHandler(url);
    });
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));

    // Deposer une remarque est un droit de lecteur ; la trancher est un acte
    // d'ecriture. La vue n'invente pas ce droit -- le serveur le dit.
    expect(await screen.findByTestId("kg-review-readonly-crr_1")).toBeInTheDocument();
    expect(screen.queryByTestId("kg-review-accept-crr_1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("kg-review-decline-crr_1")).not.toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("does not turn the non-disclosing 404 into a wrong reason", async () => {
    stubFetch((url, init) => {
      if (url.startsWith("/api/context/review-requests/crr_1/resolve") && init?.method === "POST") {
        return resp(404, { code: "not_found", message: "Review request not found" });
      }
      if (url.startsWith("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: true });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await user.click(await screen.findByTestId("kg-review-accept-crr_1"));

    // Le refus est NOMME, et la remarque reste visible : une action qui echoue
    // en silence laisse croire qu'elle a porte.
    await waitFor(() => {
      expect(screen.getByTestId("kg-review-resolve-error")).toBeInTheDocument();
    });
    const message = screen.getByTestId("kg-review-resolve-error").textContent ?? "";
    // Le serveur rend le MEME 404 pour « pas a vous », « inconnue » et « deja
    // close » -- et ne les distingue pas exprès. La vue ne doit donc en affirmer
    // aucune : elle nomme l'incertitude au lieu de la trancher a tort.
    expect(message).toMatch(/already be closed/i);
    expect(message).toMatch(/not yours/i);
    expect(message).not.toMatch(/^Not permitted\./);
    expect(screen.getByTestId("kg-review-request-crr_1")).toBeInTheDocument();
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// Canvas context menu — "New topic here"
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — new topic from the canvas", () => {
  it("right-click -> New topic here -> create -> the node is inserted", async () => {
    const calls = stubFetch((url, init) => {
      if (url === "/api/context/topics" && init?.method === "POST") {
        return resp(201, {
          id: "top_new",
          project_id: "p1",
          title: "Fresh topic",
          body_md: "Body.",
          status: "active",
          created_by: "ann@toorow.com",
          version_number: 1,
        });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    const { container } = render(<KnowledgeGraphPage projectId="p1" />);

    await screen.findByTestId("kg-node-top_1");
    const pane = container.querySelector(".react-flow__pane") as HTMLElement;
    expect(pane).toBeTruthy();
    fireEvent.contextMenu(pane);

    await user.click(await screen.findByTestId("kg-context-new-topic"));
    await user.type(screen.getByTestId("kg-topic-editor-title"), "Fresh topic");
    await user.type(screen.getByTestId("kg-topic-editor-body"), "Body.");
    await user.click(screen.getByTestId("kg-topic-editor-submit"));

    await waitFor(() => {
      expect(screen.queryByTestId("kg-topic-editor-title")).not.toBeInTheDocument();
    });

    const postCall = calls.find((c) => c.url === "/api/context/topics" && c.init?.method === "POST");
    expect(postCall).toBeDefined();
    expect(JSON.parse(String(postCall!.init!.body))).toEqual({
      project_id: "p1",
      title: "Fresh topic",
      body_md: "Body.",
      owner: null,
    });

    await screen.findByTestId("kg-node-top_new");
    expect(screen.getByTestId("kg-count")).toHaveTextContent("3/3 nodes");
  }, TEST_TIMEOUT);

  it("an EMPTY project can create its first topic — the canvas is not the only way in", async () => {
    const calls = stubFetch((url, init) => {
      if (url.startsWith("/api/context/graph?")) return resp(200, { nodes: [], edges: [] });
      if (url === "/api/context/topics" && init?.method === "POST") {
        return resp(201, {
          id: "top_first",
          project_id: "p1",
          title: "First topic",
          body_md: "",
          status: "active",
          created_by: "ann@toorow.com",
          version_number: 1,
        });
      }
      return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    // No canvas at all here, so the right-click "New topic here" affordance
    // does not exist: the empty state has to carry the action itself.
    await screen.findByTestId("kg-empty");
    expect(screen.queryByTestId("kg-canvas")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("kg-empty-new-topic"));
    await user.type(await screen.findByTestId("kg-topic-editor-title"), "First topic");
    await user.click(screen.getByTestId("kg-topic-editor-submit"));

    await screen.findByTestId("kg-node-top_first");
    expect(
      calls.some((c) => c.url === "/api/context/topics" && c.init?.method === "POST"),
    ).toBe(true);
  }, TEST_TIMEOUT);
});

// ---------------------------------------------------------------------------
// A remark read in the mindmap is ANSWERED on the node's own workbench
//
// The drawer offered Accept and Decline and nothing else, so the queue could be
// emptied here without a word of the corpus changing. `context-hub.md` refuses
// both halves of the easy fix: the queue must be actionable, and "one does not
// work a Skill inside a mindmap" — so the mindmap hands the reader over to the
// surface that owns the write instead of growing a second editor.
// ---------------------------------------------------------------------------

describe("KnowledgeGraphPage — a remark can be acted on, not only closed", () => {
  it("hands the node over to its own workbench, naming the kind the remark speaks of", async () => {
    const onAdjustNode = vi.fn();
    stubFetch((url) => {
      if (url.startsWith("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: true });
      }
      return defaultHandler(url);
    });
    render(<KnowledgeGraphPage projectId="p1" onAdjustNode={onAdjustNode} />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    fireEvent.click(await screen.findByTestId("kg-review-adjust-crr_1"));

    // The REMARK carries the node it speaks of, so the hand-over never guesses
    // which node the drawer had selected.
    expect(onAdjustNode).toHaveBeenCalledWith("topic", "top_1");
  }, TEST_TIMEOUT);

  it("renders no adjust button when no host owns the editor", async () => {
    // A button that goes nowhere is worse than no button: it reads as a gesture
    // the product offers and then swallows.
    stubFetch((url) => {
      if (url.startsWith("/api/context/review-requests")) {
        return resp(200, { requests: [REMARK], can_resolve: true });
      }
      return defaultHandler(url);
    });
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    expect(await screen.findByTestId("kg-review-request-crr_1")).toBeInTheDocument();
    expect(screen.queryByTestId("kg-review-adjust-crr_1")).not.toBeInTheDocument();
  }, TEST_TIMEOUT);
});
