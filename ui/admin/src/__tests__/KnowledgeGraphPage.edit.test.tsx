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
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
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
      if (url === "/api/context/graph/edges") {
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
    expect(calls.filter((c) => c.url === "/api/context/graph/edges")).toHaveLength(0);
    expect(screen.getByTestId("kg-count")).toHaveTextContent("2/2 nodes · 1 links");

    await user.click(screen.getByTestId("kg-edge-suggestion-depends_on"));
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
      if (url === "/api/context/graph/edges") {
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

describe("KnowledgeGraphPage — Request review (Story 44.11)", () => {
  it("POSTs node_type + note and shows the honest confirmation copy", async () => {
    const calls = stubFetch((url, init) => {
      if (url === "/api/context/nodes/top_1/request-review" && init?.method === "POST") {
        return resp(201, { status: "requested" });
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
        "Review requested — the owner will see it in the audit trail.",
      );
    });
    // Never implies a message/notification was sent to anyone.
    expect(screen.getByTestId("kg-review-modal-success")).not.toHaveTextContent(/notif|email sent/i);

    const postCall = calls.find(
      (c) => c.url === "/api/context/nodes/top_1/request-review" && c.init?.method === "POST",
    );
    expect(postCall).toBeDefined();
    expect(JSON.parse(String(postCall!.init!.body))).toEqual({
      node_type: "topic",
      note: "Please double-check this figure.",
    });
  }, TEST_TIMEOUT);

  it("shows the server's exact error and keeps the modal open on failure", async () => {
    stubFetch((url, init) => {
      if (url === "/api/context/nodes/top_1/request-review" && init?.method === "POST") {
        return resp(500, { code: "db_error", message: "Erreur lors de la demande de revue" });
      }
      return defaultHandler(url);
    });
    const user = userEvent.setup();
    render(<KnowledgeGraphPage projectId="p1" />);

    fireEvent.click(await screen.findByTestId("kg-node-top_1"));
    await user.click(await screen.findByTestId("kg-drawer-request-review"));
    await user.click(await screen.findByTestId("kg-review-modal-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("kg-review-modal-error")).toHaveTextContent(
        "Erreur lors de la demande de revue",
      );
    });
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
