/**
 * KnowledgeGraphPage — the AI Path overlay (Story 49.6 AC7).
 *
 * `context-hub.md` says a path can be "aggregated into Knowledge Graph
 * overlays". AC7 says what that overlay must be, and every test here is one of
 * its clauses:
 *
 *   - it DECORATES the canonical graph and does not mutate it — so the bundle
 *     is fetched once and never again when a path is selected;
 *   - used, missing and version-mismatched are DISTINGUISHABLE — and by their
 *     name on the card, not by a hue;
 *   - a step that reached no drawable node stays in the path detail instead of
 *     having a graph object invented for it — so the canvas reports the count;
 *   - the read is server-composed: the client asks for a state per node and
 *     never derives one.
 *
 * The failure this file exists to catch is the quiet one. A decoration that
 * silently draws nothing looks exactly like a path that deviated from nothing,
 * and that is the reading a governance surface must never allow.
 */
import { configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import KnowledgeGraphPage, { type GraphEdgeRow, type GraphNodeRow } from "../KnowledgeGraphPage";

// --- jsdom shims React Flow requires (same shapes as KnowledgeGraphPage.test)

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
  // The page runs React Flow with `onlyRenderVisibleElements`, so the size this
  // shim reports for the PANE is the size of the test's viewport. Returning one
  // card's dimensions for every element made that viewport smaller than a single
  // card, and every node but the first was culled before it could be asserted on
  // — a decoration on node two looked missing when it was merely off-screen.
  // Same shape as KnowledgeGraphPage.test.tsx: styled height/width when the
  // element has one, a real container otherwise.
  Object.defineProperties(globalThis.HTMLElement.prototype, {
    offsetHeight: {
      configurable: true,
      get(this: HTMLElement) { return Number.parseFloat(this.style.height) || 800; },
    },
    offsetWidth: {
      configurable: true,
      get(this: HTMLElement) { return Number.parseFloat(this.style.width) || 1200; },
    },
  });
  (globalThis.SVGElement.prototype as unknown as { getBBox: () => DOMRect }).getBBox = () =>
    ({ x: 0, y: 0, width: 0, height: 0 }) as DOMRect;
});

// --- Fixtures ---------------------------------------------------------------

const TOPIC: GraphNodeRow = {
  id: "top_1",
  node_type: "topic",
  title: "ROAS calculation policy",
  excerpt: "Return on ad spend is computed on deduplicated conversions.",
  owner: "ann@example.com",
  version_number: 2,
  scope: "project",
  status: "active",
};

const PROCEDURE: GraphNodeRow = {
  id: "proc_1",
  node_type: "procedure",
  title: "Weekly performance review",
  excerpt: "Pull the last 7 days, compare against the previous period.",
  owner: "bob@example.com",
  version_number: 1,
  scope: "platform",
  status: "active",
};

const EDGE: GraphEdgeRow = {
  id: "edge_1",
  from_id: "top_1",
  to_id: "proc_1",
  from_type: "topic",
  to_type: "procedure",
  edge_type: "explains",
  created_by: "ann@example.com",
  created_at: "2026-07-20T10:00:00+00:00",
};

const BUNDLE = { nodes: [TOPIC, PROCEDURE], edges: [EDGE] };

const PATHS = {
  schema_version: "ai-path-collection.v1",
  paths: [
    { id: "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV", lifecycle: "finalized", outcome: "succeeded",
      steps: 7, tools: ["get_procedure", "discover_analyze_matches", "compose_analyze_pivot", "render_analyze_result"] },
  ],
  next_cursor: null,
};

/** As `core.ai_paths.graph_overlay` composes it, server-side. */
const OVERLAY = {
  schema_version: "ai-path-graph-overlay.v1",
  path_id: "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV",
  lifecycle: "finalized",
  outcome: "succeeded",
  verdict: "fail",
  nodes: [
    { node_type: "topic", node_id: "top_1", state: "used", ordinals: [0] },
    {
      node_type: "procedure",
      node_id: "proc_1",
      state: "version_mismatch",
      ordinals: [1],
      expected_version: "pv_1",
      observed_version: "pv_9",
    },
    // Required, never reached, and NOT on this canvas: the deviation that only
    // the notice can carry.
    { node_type: "topic", node_id: "top_absent", state: "missing", ordinals: [] },
  ],
  // AC9 — Events travel BESIDE the canvas, as references resolved by their own
  // owner. One proven, one that could not be, because both must be renderable.
  event_references: [
    {
      event_id: "evt_linked",
      ordinals: [2],
      binding_state: "linked",
      event_type: "release",
      event_date: "2026-08-01",
      datastream_id: "ds_1",
      version_number: 3,
      owner_route: {
        surface: "project",
        workspace: "data",
        section: "events",
        global_surface: null,
        global_section: null,
        object_type: "event-configuration",
        object_id: "ecfg_1",
        tab: "usage",
        action: null,
        version_id: "ecv_1",
      },
    },
    { event_id: "evt_unbound", ordinals: [3], binding_state: "unavailable" },
  ],
  unrepresented_steps: 2,
};

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

const handler = (url: string) => {
  if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
  if (url.includes("/graph-overlay")) return resp(200, OVERLAY);
  if (url.includes("/context/ai-paths")) return resp(200, PATHS);
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
};

async function selectPath(calls: string[]) {
  const picker = await screen.findByTestId("kg-ai-path");
  // Focus is what loads the list — the page owes exactly ONE call per load and
  // a lens nobody opened must not spend a request.
  fireEvent.focus(picker);
  await waitFor(() => {
    expect(calls.some((u) => u.includes("/context/ai-paths?"))).toBe(true);
  });
  fireEvent.change(picker, { target: { value: OVERLAY.path_id } });
  return picker;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("KnowledgeGraphPage — AI Path overlay", () => {
  it(
    "does not spend a request until the picker is reached for",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");

      // The file's own contract: ONE call per load. The lens is not part of it.
      expect(calls.filter((u) => u.includes("/context/ai-paths"))).toHaveLength(0);
    },
    TEST_TIMEOUT,
  );

  it(
    "decorates the canonical graph and never refetches it",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      await screen.findByTestId("kg-path-state-top_1");
      // AC7: it decorates, it does not mutate. One bundle read, before and
      // after — a lens that refetched the graph could return a different one.
      expect(calls.filter((u) => u.startsWith("/api/context/graph?"))).toHaveLength(1);
    },
    TEST_TIMEOUT,
  );

  it(
    "names used and version-mismatched on the card, so colour is not the carrier",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      const used = await screen.findByTestId("kg-path-state-top_1");
      expect(used).toHaveTextContent("Used");
      expect(used).toHaveTextContent("step 0");

      const mismatch = await screen.findByTestId("kg-path-state-proc_1");
      expect(mismatch).toHaveTextContent("Wrong version");
      // The two are distinguishable in text, which is what AC7 asks for.
      expect(mismatch.textContent).not.toEqual(used.textContent);
    },
    TEST_TIMEOUT,
  );

  it(
    "reports the steps it could not draw instead of inventing nodes for them",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      const notice = await screen.findByTestId("kg-overlay-notice");
      expect(notice).toHaveTextContent("2 step(s) reached nothing this map can draw");
      // And no node was fabricated for them.
      expect(screen.queryByTestId("kg-node-top_absent")).toBeNull();
    },
    TEST_TIMEOUT,
  );

  it(
    "says a required node was not reached even though it is off the canvas",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      const notice = await screen.findByTestId("kg-overlay-notice");
      // The whole point: a deviation the mindmap cannot draw must still be
      // stated. Absorbing it silently is how a failing path renders clean.
      expect(notice).toHaveTextContent(/1 the policy REQUIRED and no step reached/i);
      expect(notice).toHaveTextContent("Assessment: fail");
    },
    TEST_TIMEOUT,
  );

  it(
    "clears every decoration when the lens is switched off",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      const picker = await selectPath(calls);
      await screen.findByTestId("kg-path-state-top_1");

      fireEvent.change(picker, { target: { value: "" } });
      await waitFor(() => {
        expect(screen.queryByTestId("kg-path-state-top_1")).toBeNull();
      });
      expect(screen.queryByTestId("kg-overlay-notice")).toBeNull();
    },
    TEST_TIMEOUT,
  );

  it(
    "says the overlay is unavailable rather than leaving the graph quietly undecorated",
    async () => {
      const calls = stubFetch((url) => {
        if (url.includes("/graph-overlay")) return resp(503, { code: "ai_paths_unavailable" });
        return handler(url);
      });
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      const notice = await screen.findByTestId("kg-overlay-notice");
      expect(notice).toHaveTextContent("Overlay unavailable");
      // The graph is still there. An unreadable lens is not a missing mindmap.
      expect(screen.getByTestId("kg-node-top_1")).toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );

  it(
    "shows an Event as a reference beside the canvas, never as a node on it",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      const row = await screen.findByTestId("kg-event-ref-evt_linked");
      expect(row).toHaveTextContent("release");
      expect(row).toHaveTextContent("2026-08-01");
      // AC9 + data.md: the Event belongs to its Datastream. The graph
      // vocabulary carries no Event type, so drawing one would mean inventing
      // the node the target withholds.
      expect(screen.queryByTestId("kg-node-evt_linked")).toBeNull();
    },
    TEST_TIMEOUT,
  );

  it(
    "hands the Event over to its owner through the reference the SERVER composed",
    async () => {
      const calls = stubFetch(handler);
      const onOpenEvent = vi.fn();
      render(<KnowledgeGraphPage projectId="p1" onOpenEvent={onOpenEvent} />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      fireEvent.click(await screen.findByTestId("kg-event-open-evt_linked"));
      // Verbatim. A route rebuilt from parts here would be Context Hub
      // deciding where an Event lives — exactly what AC9 forbids.
      expect(onOpenEvent).toHaveBeenCalledWith(OVERLAY.event_references[0].owner_route);
    },
    TEST_TIMEOUT,
  );

  it(
    "shows an unresolvable Event as unavailable rather than dropping or linking it",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" onOpenEvent={vi.fn()} />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      // Present — dropping it would hide that the path went through an Event.
      const row = await screen.findByTestId("kg-event-unavailable-evt_unbound");
      expect(row).toHaveTextContent(/could not be resolved to its owner/i);
      // And no link: another Project's Event, a deleted one and an unproven
      // binding all read the same, so the list cannot be used to probe.
      expect(screen.queryByTestId("kg-event-open-evt_unbound")).toBeNull();
    },
    TEST_TIMEOUT,
  );

  it(
    "still names the Events when no navigation handler is wired",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);

      // AC9 forbids Context Hub from OWNING the Event, not from naming it.
      // Hiding the row because the shell wired no handler would hide evidence
      // to avoid a dead button.
      expect(await screen.findByTestId("kg-event-ref-evt_linked")).toHaveTextContent("release");
      expect(screen.queryByTestId("kg-event-open-evt_linked")).toBeNull();
    },
    TEST_TIMEOUT,
  );

  it(
    "offers no way to create, edit or delete an Event from the graph",
    async () => {
      const calls = stubFetch(handler);
      render(<KnowledgeGraphPage projectId="p1" onOpenEvent={vi.fn()} />);
      await screen.findByTestId("kg-node-top_1");
      await selectPath(calls);
      await screen.findByTestId("kg-overlay-events");

      // AC9: "There is no create/edit/delete Event action in Context Hub."
      const events = screen.getByTestId("kg-overlay-events");
      const actions = Array.from(events.querySelectorAll("button")).map(
        (b) => b.textContent ?? "",
      );
      expect(actions.some((label) => /new|add|edit|delete|remove/i.test(label))).toBe(false);
    },
    TEST_TIMEOUT,
  );

  it(
    "refuses to call it an empty history when the server says recording FAILED",
    async () => {
      stubFetch((url) => {
        if (url.includes("/context/ai-paths?")) {
          // A 200 with no paths — and a server that swallowed 4 observations.
          return resp(200, { paths: [], recording_failures_this_instance: 4 });
        }
        return handler(url);
      });
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");

      const picker = await screen.findByTestId("kg-ai-path");
      fireEvent.focus(picker);
      // "No AI Path recorded yet" was FALSE for a whole day while the recorder
      // refused every context walk. An empty list is only absence when nothing
      // failed — `context-hub.md` refuses the confusion for the sibling store.
      await waitFor(() => {
        expect(picker).toHaveTextContent(/Recording is failing/i);
      });
      expect(picker).not.toHaveTextContent("No AI Path recorded yet");
    },
    TEST_TIMEOUT,
  );

  it(
    "still says the history is empty when nothing failed",
    async () => {
      const calls = stubFetch((url) => {
        if (url.includes("/context/ai-paths?")) {
          return resp(200, { paths: [], recording_failures_this_instance: 0 });
        }
        return handler(url);
      });
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");

      const picker = await screen.findByTestId("kg-ai-path");
      fireEvent.focus(picker);
      await waitFor(() => {
        expect(picker).toHaveTextContent("No AI Path recorded yet");
      });
      expect(calls.length).toBeGreaterThan(0);
    },
    TEST_TIMEOUT,
  );

  it(
    "keeps an unreadable path history apart from an empty one",
    async () => {
      const calls = stubFetch((url) => {
        if (url.includes("/context/ai-paths?")) return resp(503, { code: "ai_paths_unavailable" });
        return handler(url);
      });
      render(<KnowledgeGraphPage projectId="p1" />);
      await screen.findByTestId("kg-node-top_1");

      const picker = await screen.findByTestId("kg-ai-path");
      fireEvent.focus(picker);
      await waitFor(() => {
        expect(calls.some((u) => u.includes("/context/ai-paths?"))).toBe(true);
      });
      // "unavailable", never "none recorded" — the same distinction AiPathPage
      // makes with "This is not an empty history".
      await waitFor(() => {
        expect(picker).toHaveTextContent("AI Paths unavailable");
      });
      expect(picker).not.toHaveTextContent("No AI Path recorded yet");
    },
    TEST_TIMEOUT,
  );
});


// ---------------------------------------------------------------------------
// The drift aggregate line (GET /context/ai-paths/stats)
// ---------------------------------------------------------------------------

const STATS = {
  schema_version: "ai-path-stats.v1",
  project_id: "p1",
  window_days: 30,
  outcomes: { succeeded: 12, failed: 2, refused: 1 },
  recording: 1,
  verdicts: { pass: 8, fail: 2, unverifiable: 1 },
  verdict_window: 200,
  verdicts_assessed: 11,
  recording_failures_this_instance: 0,
};

const handlerWithStats = (url: string) => {
  if (url.startsWith("/api/context/graph?")) return resp(200, BUNDLE);
  if (url.includes("/graph-overlay")) return resp(200, OVERLAY);
  if (url.includes("/context/ai-paths/stats")) return resp(200, STATS);
  if (url.includes("/context/ai-paths")) return resp(200, PATHS);
  return resp(500, { code: "unexpected", message: `unexpected call: ${url}` });
};

it("shows the outcome/verdict aggregate with its stated window once the picker loads", async () => {
  // La question « la derive monte-t-elle ? » ne se repond pas en ouvrant les
  // parcours un par un : l'agregat est la, borne, et dit sa fenetre.
  stubFetch(handlerWithStats);
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  fireEvent.focus(await screen.findByTestId("kg-ai-path"));

  const line = await screen.findByTestId("kg-ai-path-stats");
  expect(line).toHaveTextContent("Last 30 days: 12 succeeded · 2 failed · 1 refused");
  expect(line).toHaveTextContent("verdicts over the 11 most recent: 8 pass · 2 fail · 1 unverifiable");
}, TEST_TIMEOUT);

it("never shows a stats line when the aggregate could not be read", async () => {
  // Un agregat illisible n'est pas « pas de derive » : pas de ligne, et le
  // picker garde ses propres etats honnetes.
  const handlerFailingStats = (url: string) => {
    if (url.includes("/context/ai-paths/stats")) {
      return resp(503, { code: "ai_paths_unavailable", message: "AI Paths are unavailable" });
    }
    return handlerWithStats(url);
  };
  stubFetch(handlerFailingStats);
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  fireEvent.focus(await screen.findByTestId("kg-ai-path"));
  await waitFor(() => {
    expect(screen.getByTestId("kg-ai-path")).toHaveTextContent("aip_01ARZ3NDEKTSV4RRFFQ69G5FAV");
  });

  expect(screen.queryByTestId("kg-ai-path-stats")).not.toBeInTheDocument();
}, TEST_TIMEOUT);

// ---------------------------------------------------------------------------
// The usage aggregate (GET /context/ai-paths/inspections/summary)
//
// `app.evidence_inspections` was written by two surfaces and SELECTed by none:
// the Console posted every subtree expansion into a table no screen read. This
// line is the reader — and its failure rule is the drift line's, because a
// zero and an unreadable measurement must never look alike.
// ---------------------------------------------------------------------------

const INSPECTIONS = {
  schema_version: "evidence-inspection-summary.v1",
  project_id: "p1",
  window_days: 30,
  inspections_scanned: 9,
  inspections_total: 9,
  window_truncated: false,
  scan_row_limit: 5000,
  kinds: { branch_subtree_expanded: 7, evidence_drilldown_opened: 2 },
  displayed_states: {
    branches_listed: 5,
    no_branch_judged: 2,
    branches_not_recorded: 1,
    unavailable: 1,
  },
  top_steps: [
    { ai_path_id: "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV", step_ordinal: 2, inspections: 4 },
  ],
  top_steps_limit: 10,
};

const handlerWithInspections = (url: string) => {
  if (url.includes("/context/ai-paths/inspections/summary")) return resp(200, INSPECTIONS);
  return handlerWithStats(url);
};

it("shows who opened the evidence, with the three emptinesses kept apart", async () => {
  stubFetch(handlerWithInspections);
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  fireEvent.focus(await screen.findByTestId("kg-ai-path"));

  const line = await screen.findByTestId("kg-ai-path-inspections");
  expect(line).toHaveTextContent("Last 30 days: 9 branch openings");
  // "had none to show", "were not kept" and "could not be read" are THREE
  // facts. A single "8 empty" would say "nothing was considered" about
  // something nobody can know.
  expect(line).toHaveTextContent("5 showed branches");
  expect(line).toHaveTextContent("2 had none to show");
  expect(line).toHaveTextContent("1 not kept");
  expect(line).toHaveTextContent("1 could not be read");
  // And the product question itself: which branch is opened most.
  expect(line).toHaveTextContent("most opened: step 2 of aip_01ARZ3NDEKTSV4RRFFQ69G5FAV");
}, TEST_TIMEOUT);

it("names the gesture when nobody has opened a branch yet", async () => {
  const empty = {
    ...INSPECTIONS,
    inspections_scanned: 0,
    inspections_total: 0,
    kinds: {},
    displayed_states: {
      branches_listed: 0,
      no_branch_judged: 0,
      branches_not_recorded: 0,
      unavailable: 0,
    },
    top_steps: [],
  };
  stubFetch((url) => {
    if (url.includes("/context/ai-paths/inspections/summary")) return resp(200, empty);
    return handlerWithStats(url);
  });
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  fireEvent.focus(await screen.findByTestId("kg-ai-path"));

  const line = await screen.findByTestId("kg-ai-path-inspections");
  // An empty list that names no gesture is a dead end.
  expect(line).toHaveTextContent("No evidence branch opened yet");
  expect(line).toHaveTextContent("expand a step");
}, TEST_TIMEOUT);

it("says at least N rather than a total when the window was truncated", async () => {
  stubFetch((url) => {
    if (url.includes("/context/ai-paths/inspections/summary")) {
      return resp(200, { ...INSPECTIONS, inspections_total: null, window_truncated: true });
    }
    return handlerWithStats(url);
  });
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  fireEvent.focus(await screen.findByTestId("kg-ai-path"));

  const line = await screen.findByTestId("kg-ai-path-inspections");
  // A ceiling shown as a total is a number that reads as complete.
  expect(line).toHaveTextContent("at least 9 branch openings");
}, TEST_TIMEOUT);

it("never shows a usage line when the aggregate could not be read", async () => {
  // The failure this whole line exists to prevent: "nobody opened anything" and
  // "we could not count" rendering identically.
  stubFetch((url) => {
    if (url.includes("/context/ai-paths/inspections/summary")) {
      return resp(503, { code: "evidence_inspection_unavailable" });
    }
    return handlerWithStats(url);
  });
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  fireEvent.focus(await screen.findByTestId("kg-ai-path"));
  // The drift line still renders, so the picker HAS loaded: the absence below
  // is the usage line's own, not a test that asserted before anything arrived.
  await screen.findByTestId("kg-ai-path-stats");

  expect(screen.queryByTestId("kg-ai-path-inspections")).not.toBeInTheDocument();
}, TEST_TIMEOUT);

// ---------------------------------------------------------------------------
// The door out of the lens (AI Path picker -> AiPathPage)
//
// The picker listed every path this project recorded and navigated NOWHERE:
// `objectSurfaces.tsx` has served `AiPathPage` for `{ workspace: "context-hub",
// objectType: "ai-path" }` since Story 49.6, and the only way to reach it was to
// type the address. The overlay decorates the canvas; it carries neither the
// steps, nor the branch evidence, nor the assessment.
// ---------------------------------------------------------------------------

it("opens the selected path on its own screen, through the router's own route", async () => {
  const opened: string[] = [];
  const calls = stubFetch(handler);
  render(<KnowledgeGraphPage projectId="p1" onOpenAiPath={(id) => opened.push(id)} />);
  await screen.findByTestId("kg-node-top_1");

  // No path chosen, no door: the affordance belongs to a path, not to the lens.
  expect(screen.queryByTestId("kg-ai-path-open")).not.toBeInTheDocument();

  await selectPath(calls);

  const open = await screen.findByTestId("kg-ai-path-open");
  expect(open).toHaveAccessibleName(`Open AI Path ${OVERLAY.path_id}`);
  fireEvent.click(open);

  // The EXACT path that was listed and selected — the page hands the id over
  // and composes no address of its own.
  expect(opened).toEqual([OVERLAY.path_id]);
}, TEST_TIMEOUT);

it("offers no door when the shell wired no handler, rather than a dead button", async () => {
  // Same decision `onOpenEvent` states: a reference is still SHOWN without its
  // link. A button that does nothing is worse than no button.
  const calls = stubFetch(handler);
  render(<KnowledgeGraphPage projectId="p1" />);
  await screen.findByTestId("kg-node-top_1");

  await selectPath(calls);

  expect(screen.queryByTestId("kg-ai-path-open")).not.toBeInTheDocument();
}, TEST_TIMEOUT);


it("names a path by what it did, not by its id alone (2026-09-05)", async () => {
  stubFetch(handler);
  render(<KnowledgeGraphPage projectId="p1" />);
  const picker = (await screen.findByTestId("kg-ai-path")) as HTMLSelectElement;
  fireEvent.focus(picker); // focus is what loads the list (one call per load, none for a lens nobody reached for)
  await waitFor(() =>
    expect(Array.from(picker.options).some((o) => o.value === "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV")).toBe(true),
  );
  const option = Array.from(picker.options).find((o) => o.value === "aip_01ARZ3NDEKTSV4RRFFQ69G5FAV");
  expect(option?.textContent).toContain("succeeded");
  expect(option?.textContent).toContain("7 steps");
  expect(option?.textContent).toContain("get_procedure, discover_analyze_matches, compose_analyze_pivot, …");
});
