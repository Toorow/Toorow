/**
 * Vitest tests for GraphEdges (Story 11.4, F-3).
 *
 * Coverage:
 *   - Renders existing edges for the selected node (from API response).
 *   - Add-edge form: clicking «+ Ajouter» opens the form; submitting calls
 *     POST /api/context/graph/edges with the correct body and endpoint.
 *   - Delete edge: clicking the delete button calls DELETE /api/context/graph/edges/{id}.
 *   - French copy assertions (UX-DR10).
 *   - Empty-state French message when no edges exist.
 *
 * F-2/F-4 — AD-5 list gap:
 *   - When viewing a project-scoped list, a PLATFORM edge (project_id=null)
 *     appears ALONGSIDE project edges in the rendered list.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import GraphEdges from "../connaissances/GraphEdges";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderEdges(
  nodeId = "top_01",
  projectId: string | null = "proj_1",
) {
  return render(
    <ThemeProvider theme={adminTheme}>
      <GraphEdges nodeId={nodeId} projectId={projectId} nodeType="topic" />
    </ThemeProvider>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Sample data
// ---------------------------------------------------------------------------

const EDGE_FROM_TOP_01 = {
  id: "edge_01",
  from_id: "top_01",
  from_type: "topic",
  to_id: "proc_01",
  to_type: "procedure",
  edge_type: "related",
  project_id: "proj_1",
  created_by: "admin@example.com",
  created_at: "2026-07-20T10:00:00Z",
};

const EDGE_PLATFORM = {
  id: "edge_plat",
  from_id: "top_plat",
  from_type: "topic",
  to_id: "top_01",   // points TO our node
  to_type: "topic",
  edge_type: "refines",
  project_id: null,  // platform scope
  created_by: "admin@example.com",
  created_at: "2026-07-19T09:00:00Z",
};

const EDGE_UNRELATED = {
  id: "edge_other",
  from_id: "top_99",
  from_type: "topic",
  to_id: "proc_99",
  to_type: "procedure",
  edge_type: "related",
  project_id: "proj_1",
  created_by: "admin@example.com",
  created_at: "2026-07-18T08:00:00Z",
};

// ---------------------------------------------------------------------------
// Fetch mock factory
// ---------------------------------------------------------------------------

type MockCall = { url: string; method: string; body?: unknown };

function buildMock(opts: {
  edgesPayload?: unknown[];
  edgesStatus?: number;
  createStatus?: number;
  createPayload?: unknown;
  deleteStatus?: number;
}) {
  const calls: MockCall[] = [];

  const mock = vi.fn().mockImplementation(async (url: RequestInfo | URL, init?: RequestInit) => {
    const urlStr = String(url);
    const method = (init?.method ?? "GET").toUpperCase();

    let bodyParsed: unknown;
    if (init?.body) {
      try { bodyParsed = JSON.parse(init.body as string); } catch { /* noop */ }
    }
    calls.push({ url: urlStr, method, body: bodyParsed });

    // List edges
    if (urlStr.includes("/api/context/graph/edges") && method === "GET" && !urlStr.match(/edges\/.+/)) {
      const status = opts.edgesStatus ?? 200;
      return new Response(
        JSON.stringify({ edges: opts.edgesPayload ?? [] }),
        { status, headers: { "Content-Type": "application/json" } }
      );
    }

    // Create edge
    if (urlStr.endsWith("/api/context/graph/edges") && method === "POST") {
      const status = opts.createStatus ?? 201;
      const payload = opts.createPayload ?? {
        id: "edge_new",
        from_id: (bodyParsed as Record<string, string>)?.from_id ?? "top_01",
        from_type: "topic",
        to_id: (bodyParsed as Record<string, string>)?.to_id ?? "top_02",
        to_type: (bodyParsed as Record<string, string>)?.to_type ?? "topic",
        edge_type: (bodyParsed as Record<string, string>)?.edge_type ?? "related",
        project_id: "proj_1",
        created_by: "admin@example.com",
        created_at: "2026-07-20T10:05:00Z",
      };
      const errorBody = opts.createPayload ?? { code: "invalid_param", message: "Erreur de validation" };
      return new Response(
        JSON.stringify(status >= 200 && status < 300 ? payload : errorBody),
        { status, headers: { "Content-Type": "application/json" } }
      );
    }

    // Delete edge
    if (urlStr.match(/\/api\/context\/graph\/edges\/[^/]+$/) && method === "DELETE") {
      return new Response(null, { status: opts.deleteStatus ?? 204 });
    }

    return new Response(JSON.stringify({ code: "not_found", message: "Not found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  });

  vi.stubGlobal("fetch", mock);
  return { mock, calls };
}

// ---------------------------------------------------------------------------
// 1. Empty state
// ---------------------------------------------------------------------------

describe("GraphEdges — état vide", () => {
  it("affiche le message français «Aucune liaison» quand il n'y a pas d'arête", async () => {
    buildMock({ edgesPayload: [] });
    renderEdges();

    await waitFor(() => {
      expect(screen.getByTestId("graph-edges-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("graph-edges-empty")).toHaveTextContent(
      /aucune liaison définie/i
    );
  });
});

// ---------------------------------------------------------------------------
// 2. Renders existing edges
// ---------------------------------------------------------------------------

describe("GraphEdges — affichage des liaisons existantes", () => {
  it("affiche les arêtes du nœud courant depuis la réponse API", async () => {
    buildMock({ edgesPayload: [EDGE_FROM_TOP_01, EDGE_UNRELATED] });
    renderEdges("top_01");

    await waitFor(() => {
      // EDGE_FROM_TOP_01 involves top_01; EDGE_UNRELATED does not
      expect(screen.getByTestId("graph-edge-row-edge_01")).toBeInTheDocument();
    });

    // The unrelated edge must NOT render
    expect(screen.queryByTestId("graph-edge-row-edge_other")).not.toBeInTheDocument();
  });

  it("affiche les arêtes «plateforme» (project_id=null) aux côtés des arêtes projet (AD-5 F-2/F-4)", async () => {
    // API returns both: a project edge AND a platform edge (both involve top_01)
    buildMock({ edgesPayload: [EDGE_FROM_TOP_01, EDGE_PLATFORM] });
    renderEdges("top_01");

    await waitFor(() => {
      expect(screen.getByTestId("graph-edge-row-edge_01")).toBeInTheDocument();
    });

    // Platform edge also involves top_01 (as to_id), so it should appear
    expect(screen.getByTestId("graph-edge-row-edge_plat")).toBeInTheDocument();

    // The platform chip must be visible for the platform edge
    const platformChips = screen.getAllByText("plateforme");
    expect(platformChips.length).toBeGreaterThanOrEqual(1);
  });

  it("appelle GET /api/context/graph/edges avec project_id", async () => {
    const { calls } = buildMock({ edgesPayload: [] });
    renderEdges("top_01", "proj_1");

    await waitFor(() => {
      const getCall = calls.find((c) => c.method === "GET" && c.url.includes("/api/context/graph/edges"));
      expect(getCall).toBeDefined();
      expect(getCall!.url).toContain("project_id=proj_1");
    });
  });
});

// ---------------------------------------------------------------------------
// 3. Create edge
// ---------------------------------------------------------------------------

describe("GraphEdges — créer une liaison", () => {
  it("ouvre le formulaire quand on clique sur «+ Ajouter»", async () => {
    buildMock({ edgesPayload: [] });
    renderEdges();

    await waitFor(() => {
      expect(screen.getByTestId("graph-edges-panel")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("btn-add-graph-edge"));

    await waitFor(() => {
      expect(screen.getByTestId("graph-edge-add-form")).toBeInTheDocument();
    });
    expect(screen.getByTestId("graph-edge-to-id-input")).toBeInTheDocument();
  });

  it("envoie POST /api/context/graph/edges avec le bon corps et point de terminaison", async () => {
    const user = userEvent.setup();
    const { calls } = buildMock({ edgesPayload: [], createStatus: 201 });
    renderEdges("top_01", "proj_1");

    await waitFor(() => {
      expect(screen.getByTestId("graph-edges-panel")).toBeInTheDocument();
    });

    // Open the add form
    fireEvent.click(screen.getByTestId("btn-add-graph-edge"));
    await waitFor(() => {
      expect(screen.getByTestId("graph-edge-to-id-input")).toBeInTheDocument();
    });

    // Fill in the target node ID
    const input = screen.getByTestId("graph-edge-to-id-input").querySelector("input")!;
    await user.type(input, "top_02");

    // Submit
    fireEvent.click(screen.getByTestId("btn-save-graph-edge"));

    await waitFor(() => {
      const postCall = calls.find(
        (c) => c.method === "POST" && c.url.endsWith("/api/context/graph/edges")
      );
      expect(postCall).toBeDefined();
      const body = postCall!.body as Record<string, unknown>;
      expect(body.from_id).toBe("top_01");
      expect(body.to_id).toBe("top_02");
      expect(body.from_type).toBe("topic");
      expect(body.project_id).toBe("proj_1");
    });
  });

  it("affiche l'erreur de création en français sur 422", async () => {
    buildMock({
      edgesPayload: [],
      createStatus: 422,
      createPayload: { code: "invalid_param", message: "Type de nœud source invalide : 'invalid_type'." },
    });
    renderEdges();

    await waitFor(() => {
      expect(screen.getByTestId("graph-edges-panel")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("btn-add-graph-edge"));
    await waitFor(() => {
      expect(screen.getByTestId("btn-save-graph-edge")).toBeInTheDocument();
    });

    // Fill something so it's not disabled
    const input = screen.getByTestId("graph-edge-to-id-input").querySelector("input")!;
    fireEvent.change(input, { target: { value: "bad_id" } });

    fireEvent.click(screen.getByTestId("btn-save-graph-edge"));

    await waitFor(() => {
      expect(screen.getByTestId("graph-edge-create-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("graph-edge-create-error")).toHaveTextContent(/invalide/i);
  });
});

// ---------------------------------------------------------------------------
// 4. Delete edge
// ---------------------------------------------------------------------------

describe("GraphEdges — supprimer une liaison", () => {
  it("appelle DELETE /api/context/graph/edges/{id} et retire la ligne", async () => {
    const { calls } = buildMock({ edgesPayload: [EDGE_FROM_TOP_01], deleteStatus: 204 });
    renderEdges("top_01");

    await waitFor(() => {
      expect(screen.getByTestId("graph-edge-row-edge_01")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("btn-delete-edge-edge_01"));

    await waitFor(() => {
      const deleteCall = calls.find(
        (c) => c.method === "DELETE" && c.url.includes("edge_01")
      );
      expect(deleteCall).toBeDefined();
    });

    // Row must disappear from the list
    await waitFor(() => {
      expect(screen.queryByTestId("graph-edge-row-edge_01")).not.toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// 5. F-2/F-4 AD-5 list gap — explicit platform-alongside-project assertion
// ---------------------------------------------------------------------------

describe("GraphEdges — AD-5 : topics plateforme visibles aux côtés des topics projet (F-2/F-4)", () => {
  it("une arête plateforme (project_id=null) apparaît dans la liste projet", async () => {
    // This is the key AD-5 invariant: when the API returns both a project edge
    // and a platform edge (project_id=null) for the same node, both render.
    buildMock({
      edgesPayload: [EDGE_FROM_TOP_01, EDGE_PLATFORM],
    });
    renderEdges("top_01", "proj_1");

    await waitFor(() => {
      expect(screen.getByTestId("graph-edge-row-edge_01")).toBeInTheDocument();
    });

    expect(screen.getByTestId("graph-edge-row-edge_plat")).toBeInTheDocument();
  });
});
