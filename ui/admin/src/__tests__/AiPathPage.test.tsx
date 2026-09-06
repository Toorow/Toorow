/**
 * Story 49.6 — the `ai-path` address finally answers.
 *
 * `navigation.ts` declared `{ type: "ai-path" }` under Knowledge Graph and
 * `ContentRouter` had no branch for it. `evidence_index.py` states the same in
 * its own source: "49.6 has registered the `ai-path` route type but mounted no
 * screen for it".
 *
 * The assertions below are about what the screen refuses to blur: a recording
 * path is not evidence, an unreadable store is not an empty history, and a step
 * that reached nothing governed stays visible rather than being tidied away.
 *
 * STORY 55.1 -- the three refusals survive, their DRAWING moved. The path is now
 * rendered by `AiPathFamily`, the `ai_path` family of the shared runtime, so the
 * console and the `ai-path` lens cannot answer differently. These tests keep
 * asserting the behaviour through this screen, which is what a consumer test is
 * for; the family's own contract is asserted in
 * `ui/cards/shell/src/viz/__tests__/aiPath.test.tsx`. Several literals now appear
 * TWICE on purpose -- once in the drawing, once in the accessible table fallback
 * the family always renders -- hence `getAllByText`.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AiPathPage from "../connaissances/AiPathPage";
import branchEvidenceFixture from "../../../cards/shell/src/viz/__tests__/fixtures/aiPathBranchEvidence.json";

function serve(status: number, body: unknown) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(body),
      } as Response);
    }),
  );
  return calls;
}

const FINALIZED = {
  id: "aip_1",
  lifecycle: "finalized",
  outcome: "succeeded",
  actor: "person_1",
  started_at: "2026-07-31T08:00:00Z",
  w3c_trace_id: "4bf92f3577b34da6a3ce929d0e0e4736",
  referenceable_as_evidence: true,
  // ZERO-BASED, because that is what the wire actually carries: the API projects
  // `app.ai_path_steps.ordinal` and `append_step` allocates
  // `COALESCE(MAX(ordinal) + 1, 0)`. The fixture used to say 1 and 2, which no
  // server ever sends -- so it agreed with the screen no matter which convention
  // the screen used, and the off-by-one of AI-134 was invisible to it.
  steps: [
    { step_order: 0, step_kind: "tool_call", tool_name: "get_daily_report",
      outcome: "succeeded", owner_workspace: "analyze", owner_object_type: "semantic-view",
      owner_object_id: "sv_1" },
    { step_order: 1, step_kind: "tool_call", tool_name: "health", outcome: "succeeded" },
  ],
  assessment: { verdict: "clean" },
};

const STALE_BRANCH_EVIDENCE = {
  schema_version: "retrieval-branch-evidence.v1",
  state: "branches_not_recorded",
} as const;

const INSPECTABLE = {
  ...FINALIZED,
  steps: [
    { step_order: 0, step_kind: "knowledge_read", tool_name: "search_context",
      outcome: "succeeded", branch_evidence: STALE_BRANCH_EVIDENCE },
    { step_order: 1, step_kind: "knowledge_read", tool_name: "briefing_context_event",
      outcome: "succeeded", branch_evidence: STALE_BRANCH_EVIDENCE },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("AI Path — reading one recorded interaction", () => {
  it("asks the owner's route for the exact path", async () => {
    const calls = serve(200, FINALIZED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]).toBe("/api/projects/p1/context/ai-paths/aip_1");
  });

  it("lists the steps in order, including the one that reached nothing governed", async () => {
    serve(200, FINALIZED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    expect((await screen.findAllByText("get_daily_report")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("health").length).toBeGreaterThan(0);
    // The owner is explicit that an unrepresented call must stay visible rather
    // than have a graph object invented for it.
    expect(screen.getAllByText("nothing governed").length).toBeGreaterThan(0);
  });

  it("draws the walk through the one shared family, not a table of its own", async () => {
    serve(200, FINALIZED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    // The family, its timeline and its accessible table fallback -- all of it
    // comes from `ui/cards/shell`, so this screen owns no drawing to drift.
    expect(await screen.findByTestId("ai-path")).toBeInTheDocument();
    expect(screen.getByTestId("ai-path-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("ai-path-table")).toBeInTheDocument();
  });

  it("renders the timeline in the order of the step's own ordinal, not the array order", async () => {
    // The wire arrives ordered (`ORDER BY ordinal`); a reader that trusted the
    // array order anyway would show a shuffled walk the day that stops holding.
    serve(200, { ...FINALIZED, steps: [...FINALIZED.steps].reverse() });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const timeline = await screen.findByTestId("ai-path-timeline");
    const rows = [...timeline.querySelectorAll("[data-ai-path-step]")];
    // The reader sees the 1-based label, in walk order: `get_daily_report`
    // (ordinal 0) before `health` (ordinal 1).
    expect(rows.map((row) => row.getAttribute("data-ai-path-ordinal"))).toEqual(["1", "2"]);
    expect(rows[0]?.textContent).toContain("get_daily_report");
    expect(rows[1]?.textContent).toContain("health");
  });

  it("tells distinct step kinds apart by name and marker, never by colour alone", async () => {
    serve(200, {
      ...FINALIZED,
      steps: [
        { step_order: 0, step_kind: "skill_step", tool_name: "daily_briefing",
          outcome: "succeeded", owner_workspace: "analyze", owner_object_type: "skill",
          owner_object_id: "sk_1" },
        { step_order: 1, step_kind: "tool_call", tool_name: "health", outcome: "succeeded" },
      ],
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const timeline = await screen.findByTestId("ai-path-timeline");
    const rows = [...timeline.querySelectorAll("[data-ai-path-step]")];
    expect(rows.map((row) => row.getAttribute("data-ai-path-kind"))).toEqual([
      "skill_step",
      "tool_call",
    ]);
    // The kind is printed as text on the row...
    expect(screen.getAllByText("skill_step").length).toBeGreaterThan(0);
    expect(screen.getAllByText("tool_call").length).toBeGreaterThan(0);
    // ...and each row carries its named glyph.
    for (const row of rows) {
      expect(row.querySelector("svg")).not.toBeNull();
    }
  });

  it("says a recording path cannot be pinned as evidence", async () => {
    serve(200, { ...FINALIZED, lifecycle: "recording", referenceable_as_evidence: false });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    expect(await screen.findByText("Still recording — not evidence")).toBeInTheDocument();
    expect(screen.getByText(/cannot be pinned as evidence/)).toBeInTheDocument();
  });

  it("does not warn about recording when the path is finalized", async () => {
    serve(200, FINALIZED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    await screen.findAllByText("get_daily_report");
    expect(screen.queryByText("Still recording — not evidence")).not.toBeInTheDocument();
    expect(screen.getByText("Observed walk")).toBeInTheDocument();
  });

  it("shows two node states and no third one it never observed", async () => {
    serve(200, FINALIZED);
    const { container } = render(<AiPathPage projectId="p1" pathId="aip_1" />);

    await screen.findByTestId("ai-path");
    const states = [...container.querySelectorAll("[data-node-state]")].map((node) =>
      node.getAttribute("data-node-state"),
    );
    expect(states.length).toBe(FINALIZED.steps.length);
    // `core.ai_path_recorder.STEP_STATES` carries neither; drawing them would be
    // a progression this screen never observed.
    expect(states).not.toContain("active");
    expect(states).not.toContain("pending");
  });

  it("tells an unreadable store apart from an empty history", async () => {
    serve(503, { code: "ai_paths_unavailable" });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    expect(await screen.findByText(/not an empty history/)).toBeInTheDocument();
  });

  it("does not guess why a 404 happened, because the server hides existence", async () => {
    serve(404, { code: "not_found" });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const message = await screen.findByText(/not available in this project/);
    expect(message).toBeInTheDocument();
    // No claim about whether it exists elsewhere: that is what 404 conceals.
    expect(screen.queryByText(/does not exist/)).not.toBeInTheDocument();
  });

  it("reads its record in the product's words, keeping the stored key one hover away", async () => {
    // The header rows were reaching a person as the wire spells them.
    // `label()` de-snaked `w3c_trace_id` into `W3c trace id`, which is the
    // transport protocol talking, not the product. The key is not destroyed by
    // the translation: it stays on the row's `title`, the same disclosure
    // `FieldCatalogRail` gives its facets.
    serve(200, FINALIZED);
    const { container } = render(<AiPathPage projectId="p1" pathId="aip_1" />);

    expect(await screen.findByText("Trace across services")).toBeInTheDocument();
    expect(screen.getByText("State of the record")).toBeInTheDocument();
    expect(screen.getByText("Can be cited as evidence")).toBeInTheDocument();
    expect(screen.getByText("Who asked")).toBeInTheDocument();
    // None of the wire keys is read as a label any more.
    for (const wire of ["W3c trace id", "Model ref", "Policy snapshot hash", "Referenceable as evidence"]) {
      expect(screen.queryByText(wire)).not.toBeInTheDocument();
    }
    // The stored key survives the translation.
    expect(container.querySelector('[title="w3c_trace_id"]')).not.toBeNull();
  });

  it("reads the times as a person reads them, not as the wire spells them", async () => {
    serve(200, { ...FINALIZED, ended_at: "2026-07-31T08:04:00Z" });
    const { container } = render(<AiPathPage projectId="p1" pathId="aip_1" />);

    await screen.findByText("Started");
    expect(container.textContent).not.toContain("2026-07-31T08:00:00Z");
    expect(container.textContent).not.toContain("2026-07-31T08:04:00Z");
  });

  it("renders no assessment rather than an invented one", async () => {
    serve(200, { ...FINALIZED, assessment: null });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    expect(await screen.findByText("No assessment")).toBeInTheDocument();
  });
});

/**
 * Story 55.2 -- the console is the surface that RECORDS.
 *
 * `mcp_app_behavior` (epic 51) judges an interaction per execution, and migration
 * 168 recorded that nothing observed one. Migration 175 created the record and
 * this screen is its first writer, so the wiring is pinned here: a subtree that
 * expands without posting would ship the surface and leave it ungradable again.
 */
describe("AI Path — opening a branch subtree is recorded (Story 55.2)", () => {
  function serveWithBodies(readBody: unknown) {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve({
          ok: true,
          status: init?.method === "POST" ? 201 : 200,
          json: () => Promise.resolve(init?.method === "POST" ? { id: "evi_1" } : readBody),
        } as Response);
      }),
    );
    return calls;
  }

  it("posts which walk, which step, which state -- and no count it does not have", async () => {
    const calls = serveWithBodies(INSPECTABLE);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");

    fireEvent.click(screen.getAllByTestId("ai-path-branch-toggle")[0]);

    await waitFor(() => expect(calls.length).toBe(2));
    const post = calls[1];
    expect(post.url).toBe("/api/projects/p1/context/ai-paths/aip_1/inspections");
    expect(post.init?.method).toBe("POST");
    const body = JSON.parse(String(post.init?.body));
    expect(body).toEqual({
      kind: "branch_subtree_expanded",
      surface: "console",
      // This screen reads a PERSISTED walk and the judged candidates travel only
      // on the live stream, so the honest state is "not recorded" -- never zero.
      displayed_state: "branches_not_recorded",
      branches_listed: null,
      // The FIRST step, and its own ordinal is 0. What gets recorded is the
      // identity that `idx_evidence_inspections_path` joins on, never the label
      // the reader saw -- the screen shows "1." for this same step, asserted
      // just below. Recording 1 here designates the SECOND step (AI-134).
      step_ordinal: 0,
    });
  });

  it("records the step's own ordinal while showing a 1-based label (AI-134)", async () => {
    const calls = serveWithBodies(INSPECTABLE);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");

    // What the reader sees: the walk is numbered from 1, not from 0.
    const shown = Array.from(
      document.querySelectorAll("[data-ai-path-row]"),
    ).map((row) => row.getAttribute("data-ai-path-row"));
    expect(shown).toEqual(["1", "2"]);

    // What gets written: the second step's own ordinal is 1, not 2.
    fireEvent.click(screen.getAllByTestId("ai-path-branch-toggle")[1]);
    await waitFor(() => expect(calls.length).toBe(2));
    const body = JSON.parse(String(calls[calls.length - 1].init?.body));
    expect(body.step_ordinal).toBe(1);
  });

  it("issues no second read when a subtree opens (AC8: read-only over delivered data)", async () => {
    const calls = serveWithBodies(INSPECTABLE);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");
    expect(calls.filter((c) => (c.init?.method ?? "GET") === "GET").length).toBe(1);

    fireEvent.click(screen.getAllByTestId("ai-path-branch-toggle")[0]);
    await waitFor(() => expect(calls.length).toBe(2));

    expect(calls.filter((c) => (c.init?.method ?? "GET") === "GET").length).toBe(1);
  });

  it("keeps the subtree open when the recording call fails (AC6)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        if (init?.method === "POST") return Promise.reject(new Error("recorder down"));
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(INSPECTABLE),
        } as Response);
      }),
    );
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");

    const toggle = screen.getAllByTestId("ai-path-branch-toggle")[0];
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // SCOPED TO THE SUBTREE THIS TOGGLE CONTROLS. `INSPECTABLE` carries two
    // steps and both hold the same stale evidence; the renderer keeps every
    // panel in the DOM behind `hidden`, so a page-wide `getByText` matched twice
    // and could never have passed. What AC6 claims is about ONE subtree: it is
    // still open, and it still says what it does not know. `aria-controls` is
    // the only honest way to name that one.
    const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBeNull();
    expect(panel).not.toHaveAttribute("hidden");
    expect(within(panel as HTMLElement).getByText(
      "Branch evidence was not recorded for this step. No candidate or rejection reason is inferred.",
    )).toBeTruthy();
  });
});

describe("AI Path — the public branch evidence the record carries", () => {
  it("consumes the server-generated API detail with observed projection parity", async () => {
    serve(200, branchEvidenceFixture.api_detail);
    render(<AiPathPage projectId="p1" pathId="aip_fixture" />);
    await screen.findByTestId("ai-path");

    fireEvent.click(screen.getByTestId("ai-path-branch-toggle"));
    expect(screen.getByRole("table", { name: /branch evidence/i })).toHaveTextContent(
      "Fixture Branch Alpha 21",
    );
    expect(branchEvidenceFixture.api_detail.steps[0]?.branch_evidence).toEqual(
      branchEvidenceFixture.observed_projection.steps[0]?.branch_evidence,
    );
  });

  const BRANCH_EVIDENCE = {
    schema_version: "retrieval-branch-evidence.v1",
    state: "branches_listed",
    walk: {
      producer: "context_search",
      mode: "title>description>graph_neighbor",
      graph_hop_depth: 1,
      semantic_recall: false,
      selection_limit: 5,
      judged_count: 3,
      selected_count: 1,
      rejected_count: 2,
      listed_count: 3,
      listing_truncated: false,
      not_reached_enumerated: false,
      tier_scale: { title: 1, description: 0.7, neighbor: 0.4 },
    },
    branches: [
      { id: "top_01", kind: "topic", title: "ROAS policy", score: 0.91,
        tier: "title", matched: true, rank: 1, fate: "selected", reason: null },
      { id: "top_02", kind: "topic", title: "Attribution window", score: 0.34,
        tier: "description", matched: false, rank: 2, fate: "rejected",
        reason: "below_cutoff" },
      { id: "proc_02", kind: "procedure", title: "budget-pacing-check", score: 0.12,
        tier: "neighbor", matched: false, rank: 3, fate: "rejected",
        reason: "anti_trigger" },
    ],
  } as const;
  const JUDGED = {
    ...FINALIZED,
    steps: [
      { step_order: 0, step_kind: "knowledge_read", tool_name: "search_context",
        outcome: "succeeded", branch_evidence: BRANCH_EVIDENCE },
      FINALIZED.steps[1],
    ],
  };

  it("renders the closed public object without reconstructing producer arrays", async () => {
    serve(200, JUDGED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");
    fireEvent.click(screen.getByTestId("ai-path-branch-toggle"));

    const table = screen.getByRole("table", { name: /branch evidence/i });
    for (const branch of BRANCH_EVIDENCE.branches) {
      expect(table.textContent).toContain(branch.title);
    }
    expect(table.textContent).toContain("below_cutoff");
    expect(table.textContent).toContain("anti_trigger");
  });

  it("offers no branch control on a non-retrieval step", async () => {
    serve(200, JUDGED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");
    expect(screen.getAllByTestId("ai-path-branch-toggle")).toHaveLength(1);
  });

  it("records the exact public listed count without another read", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve({
          ok: true,
          status: init?.method === "POST" ? 201 : 200,
          json: () => Promise.resolve(init?.method === "POST" ? { id: "evi_1" } : JUDGED),
        } as Response);
      }),
    );
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");

    fireEvent.click(screen.getByTestId("ai-path-branch-toggle"));

    await waitFor(() => expect(calls.length).toBe(2));
    const body = JSON.parse(String(calls[1].init?.body));
    expect(body.displayed_state).toBe("branches_listed");
    expect(body.branches_listed).toBe(3);
  });
});


/**
 * Story 49.6 AC7/AC9, lot 4 -- THE WORKBENCH HALF OF THE TRACE LENS.
 *
 * What was measured before this: `AiPathFamily` has exposed `renderOwner` since
 * 55.1 -- "the console attaches its own owner navigation here" -- and this
 * screen passed `steps`, `lifecycle` and `onInspect` and nothing else. So every
 * owner an answer went through reached a person as PLAIN TEXT: the walk named
 * the objects and opened none of them. And a path opened directly carried no
 * Event reference at all, so the same evidence read through the Knowledge Graph
 * overlay and read here answered differently about the same walk.
 *
 * The rule these assert is the one AC7 states: the reference is COMPOSED BY THE
 * SERVER. This screen hands a complete reference to the shell resolver or it
 * says the owner cannot be opened. It never joins a workspace, a section and an
 * id into an address -- doing that would make the console a second registry of
 * owner routes, and a renamed route would then be dead on one surface only.
 */
describe("AI Path — the owner of a step, and the Events the walk crossed", () => {
  /** What `ai_paths_api._step_owner_reference` composes for a Datastream. */
  const DATASTREAM_OWNER = {
    surface: "project",
    workspace: "data",
    section: "datastreams",
    global_surface: null,
    global_section: null,
    object_type: "datastream",
    object_id: "ds_1",
    tab: "overview",
    action: null,
    version_id: null,
    evidence_id: null,
  };

  const EVENT_OWNER = {
    surface: "project",
    workspace: "data",
    section: "events",
    global_surface: null,
    global_section: null,
    object_type: "event-configuration",
    object_id: "ecfg_1",
    tab: "usage",
    action: null,
    version_id: null,
    evidence_id: null,
  };

  const WITH_OWNERS = {
    ...FINALIZED,
    steps: [
      {
        step_order: 0,
        step_kind: "tool_call",
        tool_name: "get_daily_report",
        outcome: "succeeded",
        owner_workspace: "data",
        owner_object_type: "datastream",
        owner_object_id: "ds_1",
        owner_reference: DATASTREAM_OWNER,
        owner_reference_state: "governed",
      },
      {
        step_order: 1,
        step_kind: "skill_step",
        tool_name: "get_procedure",
        outcome: "succeeded",
        owner_workspace: "context-hub",
        owner_object_type: "procedure",
        owner_object_id: "proc_1",
        // Named, governed, and no screen holds that kind of object. The server
        // says so; this screen must not turn it into a link anyway.
        owner_reference: null,
        owner_reference_state: "unavailable",
      },
      {
        step_order: 2,
        step_kind: "tool_call",
        tool_name: "health",
        outcome: "succeeded",
        owner_reference: null,
        owner_reference_state: "not_governed",
      },
    ],
    event_references: [
      {
        event_id: "evt_1",
        ordinals: [0],
        binding_state: "linked",
        event_type: "release",
        event_date: "2026-08-01",
        version_number: 3,
        owner_route: EVENT_OWNER,
      },
    ],
  };

  it("opens the owner of a step through the reference the SERVER composed", async () => {
    serve(200, WITH_OWNERS);
    const onOpenOwner = vi.fn();
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={onOpenOwner} />);

    fireEvent.click(await screen.findByTestId("ai-path-owner-open-0"));

    // THE WHOLE REFERENCE, not parts of it. Passing an id and letting the shell
    // guess the rest is the second-registry defect this asserts against.
    expect(onOpenOwner).toHaveBeenCalledTimes(1);
    expect(onOpenOwner).toHaveBeenCalledWith(DATASTREAM_OWNER);
  });

  it("does not offer a link for an owner the server could not resolve", async () => {
    serve(200, WITH_OWNERS);
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={vi.fn()} />);

    await screen.findByTestId("ai-path");
    // No control -- and the sentence instead, with the identifier kept so the
    // evidence survives the refusal.
    expect(screen.queryByTestId("ai-path-owner-open-1")).toBeNull();
    const unavailable = screen.getByTestId("ai-path-owner-unavailable-1");
    expect(unavailable.textContent).toContain("proc_1");
    expect(unavailable.textContent).toContain("no screen opens this object");
  });

  it("keeps a step that reached nothing governed visible, and offers no owner control", async () => {
    serve(200, WITH_OWNERS);
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={vi.fn()} />);

    await screen.findByTestId("ai-path");
    expect(screen.getAllByText("health").length).toBeGreaterThan(0);
    expect(screen.getAllByText("nothing governed").length).toBeGreaterThan(0);
    // Not a broken link: there is nothing to link to.
    expect(screen.queryByTestId("ai-path-owner-open-2")).toBeNull();
    expect(screen.queryByTestId("ai-path-owner-unavailable-2")).toBeNull();
  });

  it("names the owner, and never builds its address from ids", async () => {
    serve(200, WITH_OWNERS);
    const { container } = render(
      <AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={vi.fn()} />,
    );

    await screen.findByTestId("ai-path");
    // No anchor with a composed href anywhere: the shell resolves references,
    // this screen never writes a URL.
    expect(container.querySelector("a[href*='/org/']")).toBeNull();
    expect(container.querySelector("a[href*='/project/']")).toBeNull();
  });

  it("opens an Event on the path through its DATA owner, not a Context Hub copy", async () => {
    serve(200, WITH_OWNERS);
    const onOpenOwner = vi.fn();
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={onOpenOwner} />);

    fireEvent.click(await screen.findByTestId("ai-path-event-open-evt_1"));

    expect(onOpenOwner).toHaveBeenCalledWith(EVENT_OWNER);
    // The reference names the Event and points at it. Nothing of its content --
    // mapping, payload, sample, run evidence -- is on this screen.
    expect(screen.getByTestId("ai-path-event-ref-evt_1").textContent).toContain("release");
  });

  it("says an Event could not be resolved rather than linking it to an unproven owner", async () => {
    serve(200, {
      ...WITH_OWNERS,
      event_references: [
        { event_id: "evt_gone", ordinals: [0], binding_state: "unavailable" },
      ],
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={vi.fn()} />);

    expect(await screen.findByTestId("ai-path-event-unavailable-evt_gone")).toBeInTheDocument();
    expect(screen.queryByTestId("ai-path-event-open-evt_gone")).toBeNull();
  });

  it("shows no Events panel on a walk that crossed none", async () => {
    serve(200, FINALIZED);
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={vi.fn()} />);

    await screen.findByTestId("ai-path");
    // An empty "Events" panel on a path that touched no Event would invent an
    // absence the record does not claim.
    expect(screen.queryByTestId("ai-path-event-refs")).toBeNull();
  });

  it("names the owner without a control when the shell wired no handler", async () => {
    serve(200, WITH_OWNERS);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    await screen.findByTestId("ai-path");
    // A host that cannot navigate must not grow a button that does nothing --
    // and must not hide the owner either. Same decision the overlay states.
    expect(screen.queryByTestId("ai-path-owner-open-0")).toBeNull();
    expect(screen.getByTestId("ai-path-owner-unavailable-0").textContent).toContain("ds_1");
  });
});


/**
 * Story 49.6 AC8 — the +/- annotation, on the exact step.
 *
 * `context-hub.md:83-85` ("annotated with positive or negative feedback") read
 * with the amendment of 2026-08-30 (`context-hub.md:701-764`) draws the boundary
 * these assert: the control is rendered here, the COMMAND is Test's
 * (`analyze-and-test.md:193`), and the standing reaction is the latest row. So
 * the screen must post to the Test namespace, must send only the target the
 * server can re-resolve, must show the verdict the server says it recorded
 * rather than the one it sent, and must start in the recorded state when the
 * detail read already carries the caller's own reaction.
 *
 * The family draws every step twice — the timeline and the accessible table
 * fallback — and the CONTROL is mounted once, on the timeline: the table
 * fallback states the recorded polarity as text. One reaction, one state, one
 * DOM id.
 */
describe("AI Path — annotating one step through Test", () => {
  interface Routed {
    calls: { url: string; init?: RequestInit }[];
  }

  function route(feedback: { status: number; body: unknown }): Routed {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        const isFeedback = url.includes("/test/feedback/ai-path-steps");
        return Promise.resolve({
          ok: isFeedback
            ? feedback.status >= 200 && feedback.status < 300
            : true,
          status: isFeedback ? feedback.status : 200,
          json: () => Promise.resolve(isFeedback ? feedback.body : FINALIZED),
        } as Response);
      }),
    );
    return { calls };
  }

  const RECORDED = {
    schema_version: "exact-feedback-receipt.v1",
    status: "recorded",
    feedback_id: "fba_1",
    ai_path_id: "aip_1",
    target: { kind: "path_step", ordinal: 1 },
    polarity: "negative",
    comment: "the wrong Skill version",
  };

  async function timeline() {
    return within(await screen.findByTestId("ai-path-timeline"));
  }

  it("offers a named reaction on every step of the walk", async () => {
    route({ status: 201, body: RECORDED });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    // One control per step, and each thumb SAYS which step it speaks of: a pair
    // of bare glyphs repeated down a list names nothing to a screen reader.
    expect(
      walk.getByLabelText("This step was helpful — step 1"),
    ).toBeInTheDocument();
    expect(
      walk.getByLabelText("This step was not helpful — step 1"),
    ).toBeInTheDocument();
    expect(
      walk.getByLabelText("This step was not helpful — step 2"),
    ).toBeInTheDocument();
  });

  it("posts the exact step target to the Test-owned door, with an idempotency key", async () => {
    const routed = route({ status: 201, body: RECORDED });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was not helpful — step 2"));
    fireEvent.change(walk.getByLabelText(/add a comment/), {
      target: { value: "the wrong Skill version" },
    });
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-1"));

    await waitFor(() =>
      expect(
        routed.calls.some((call) => call.url.includes("/test/feedback/ai-path-steps")),
      ).toBe(true),
    );
    const posted = routed.calls.find((call) =>
      call.url.includes("/test/feedback/ai-path-steps"),
    )!;
    // The Test namespace, never a Context Hub write.
    expect(posted.url).toBe("/api/projects/p1/test/feedback/ai-path-steps");
    expect(posted.init?.method).toBe("POST");
    const headers = posted.init?.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBeTruthy();
    // Only the four fields the command accepts. A Result id or a Render pin sent
    // from here would be a caller-owned authority; the server re-resolves both.
    expect(JSON.parse(String(posted.init?.body))).toEqual({
      ai_path_id: "aip_1",
      step_ordinal: 1,
      polarity: "negative",
      comment: "the wrong Skill version",
    });
  });

  it("shows the verdict the server recorded, not the one it was sent", async () => {
    route({
      status: 200,
      body: { ...RECORDED, status: "replayed", polarity: "positive", comment: "kept" },
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was not helpful — step 2"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-1"));

    const recorded = await walk.findByTestId("ai-path-step-feedback-recorded-1");
    // Sent `negative`; the row holds `positive`, and that is what is shown.
    expect(recorded.textContent).toContain("Recorded: Helpful");
    expect(recorded.textContent).toContain("kept");
    expect(walk.queryByLabelText("This step was helpful — step 2")).toBeNull();
  });

  it("says why a refused reaction was refused, and names the gesture", async () => {
    route({
      status: 422,
      body: { code: "ai_path_still_recording", message: "unused server wording" },
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was helpful — step 1"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-0"));

    const refusal = await walk.findByTestId("ai-path-step-feedback-refused-0");
    expect(refusal.textContent).toBe(
      "This walk is still being recorded. It can be annotated once it has finished.",
    );
  });

  it("keeps the 404 non-disclosing: a foreign step reads like a missing one", async () => {
    route({ status: 404, body: { code: "not_found", message: "Not found" } });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was helpful — step 1"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-0"));

    const refusal = await walk.findByTestId("ai-path-step-feedback-refused-0");
    expect(refusal.textContent).toBe("This step is no longer available in this project.");
  });

  it("mounts ONE control per step on the whole screen, not one per render site", async () => {
    route({ status: 201, body: RECORDED });
    const { container } = render(<AiPathPage projectId="p1" pathId="aip_1" />);

    await screen.findByTestId("ai-path");
    // The family draws every step twice — the timeline and the accessible table
    // fallback. The CONTROL is mounted once: two mounts is two React states and
    // two identical `data-testid`s for one reaction, and the same person could
    // file it from either copy without the other knowing.
    for (const ordinal of [0, 1]) {
      expect(screen.getAllByTestId(`ai-path-step-feedback-${ordinal}`)).toHaveLength(1);
      expect(
        screen.getAllByLabelText(`This step was helpful — step ${ordinal + 1}`),
      ).toHaveLength(1);
      // ... and the fallback row is still informative rather than empty.
      expect(
        screen.getByTestId(`ai-path-step-feedback-table-${ordinal}`).textContent,
      ).toBe("No reaction");
    }

    // A DOM id is unique or it is not an id: `htmlFor` on a duplicated id points
    // at whichever copy the browser found first.
    fireEvent.click(screen.getByLabelText("This step was not helpful — step 1"));
    expect(container.querySelectorAll("#ai-path-step-comment-0")).toHaveLength(1);
  });

  it("states the recorded reaction in the accessible table, once it is recorded", async () => {
    route({ status: 201, body: { ...RECORDED, target: { kind: "path_step", ordinal: 0 },
      polarity: "positive", comment: null } });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was helpful — step 1"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-0"));

    await walk.findByTestId("ai-path-step-feedback-recorded-0");
    // One reaction, stated in both places, held in ONE state.
    expect(screen.getByTestId("ai-path-step-feedback-table-0").textContent).toBe("Helpful");
    expect(screen.getByTestId("ai-path-step-feedback-table-1").textContent).toBe("No reaction");
  });

  it("keys the reaction on (path, step) and on nothing else — no timestamp", async () => {
    const first = route({ status: 201, body: RECORDED });
    const one = render(<AiPathPage projectId="p1" pathId="aip_1" />);
    let walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was not helpful — step 2"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-1"));
    await waitFor(() =>
      expect(first.calls.some((c) => c.url.includes("/test/feedback/ai-path-steps"))).toBe(true),
    );
    const keyOf = (routed: Routed) =>
      (routed.calls.find((c) => c.url.includes("/test/feedback/ai-path-steps"))!.init
        ?.headers as Record<string, string>)["Idempotency-Key"];
    const firstKey = keyOf(first);
    one.unmount();

    // The reload. A key minted per mount (`Date.now()`) made this a NEW command:
    // same person, same step, a second row, and an aggregate counting one
    // judgement twice. The key is deterministic, so this is a REPLAY.
    const second = route({ status: 200, body: { ...RECORDED, status: "replayed" } });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was not helpful — step 2"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-1"));
    await waitFor(() =>
      expect(second.calls.some((c) => c.url.includes("/test/feedback/ai-path-steps"))).toBe(true),
    );

    expect(firstKey).toBe("ai-path-step-aip_1-1");
    expect(keyOf(second)).toBe(firstKey);
    expect(firstKey).not.toMatch(/\d{10,}/);
  });

  it("starts in the recorded state when the read already carries this person's reaction", async () => {
    // `my_feedback` is composed by the detail read from the Feedback owner's
    // table. Without it the control could only ever be in its "not asked yet"
    // state, so a reload re-asked a question this person had already answered.
    route({ status: 201, body: RECORDED });
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              ...FINALIZED,
              steps: [
                { ...FINALIZED.steps[0], my_feedback: { polarity: "negative",
                  recorded_at: "2026-08-30T09:00:00Z",
                  comment: "this step read the wrong Skill version" } },
                FINALIZED.steps[1],
              ],
            }),
        } as Response),
      ),
    );
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    const recorded = (await walk.findByTestId("ai-path-step-feedback-recorded-0")).textContent;
    expect(recorded).toContain("Recorded: Not helpful");
    // The sentence the person wrote survives the reload too (review 2026-08-30, N1).
    expect(recorded).toContain("this step read the wrong Skill version");
    // The question is not asked a second time.
    expect(walk.queryByLabelText("This step was helpful — step 1")).toBeNull();
    // ... and the step nobody reacted to still asks it.
    expect(walk.getByLabelText("This step was helpful — step 2")).toBeInTheDocument();
    expect(screen.getByTestId("ai-path-step-feedback-table-0").textContent).toBe("Not helpful");
  });

  it("says a second, different reaction was refused, in the person's words", async () => {
    route({ status: 409, body: { code: "idempotency_conflict", message: "wire wording" } });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);

    const walk = await timeline();
    fireEvent.click(walk.getByLabelText("This step was helpful — step 1"));
    fireEvent.click(walk.getByTestId("ai-path-step-feedback-record-0"));

    const refusal = await walk.findByTestId("ai-path-step-feedback-refused-0");
    expect(refusal.textContent).toBe("You already recorded a reaction on this step.");
  });
});


describe("what each step chose (2026-09-05)", () => {
  it("lists the recorded choice of each step that carried one, and nothing for the others", async () => {
    serve(200, {
      ...FINALIZED,
      steps: [
        { step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded",
          chose: { family: "table", "request.pivot.rows": ["mdm_date"], "request.members[].datastream_id": ["ds_A", "ds_B"] } },
        { step_order: 1, step_kind: "tool_call", tool_name: "health", outcome: "succeeded" },
      ],
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    const row = await screen.findByTestId("ai-path-chose-0");
    expect(row).toHaveTextContent("compose_analyze_pivot");
    expect(row).toHaveTextContent("request.pivot.rows");
    expect(row).toHaveTextContent("mdm_date");
    expect(row).toHaveTextContent("ds_A, ds_B");
    expect(screen.queryByTestId("ai-path-chose-1")).not.toBeInTheDocument();
  });

  it("draws no choices panel when no step recorded one", async () => {
    serve(200, FINALIZED);
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByText(/AI Path/);
    expect(screen.queryByText("What each step chose")).not.toBeInTheDocument();
  });
});


describe("the Skill followed and the rest of the interaction (2026-09-05)", () => {
  it("names each prescribed step of the served Skill with what became of it", async () => {
    serve(200, {
      ...FINALIZED,
      skill_coverage: [
        {
          skill_version: "proc_1@6",
          skill_name: "youtube-video-analyst",
          prescribed: 3,
          crossed: ["2"],
          skipped: ["3"],
          unobservable: ["1"],
          sequence_readable: true,
          steps: [
            { step: "1", label: "Read the catalogue", tool: null, action: "read", state: "unobservable" },
            { step: "2", label: "Run the pinned query", tool: "execute_analyze_query_spec", action: "analyze", state: "crossed" },
            { step: "3", label: "Read the Result", tool: "analyze_result", action: "read", state: "skipped" },
          ],
        },
      ],
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    const panel = await screen.findByTestId("ai-path-skill-proc_1@6");
    expect(panel).toHaveTextContent("youtube-video-analyst");
    expect(panel).toHaveTextContent("1 of 3 steps crossed, 1 skipped, 1 not observable");
    expect(within(panel).getByTestId("ai-path-skill-step-2")).toHaveTextContent("Crossed");
    expect(within(panel).getByTestId("ai-path-skill-step-3")).toHaveTextContent("Skipped");
    expect(within(panel).getByTestId("ai-path-skill-step-1")).toHaveTextContent("Not observable");
  });

  it("routes to the other paths of the same interaction through the owner resolver", async () => {
    const opened: unknown[] = [];
    serve(200, {
      ...FINALIZED,
      same_interaction: [
        { path_id: "aip_session", state: "recording", outcome: null,
          owner_route: { surface: "project", workspace: "context-hub", section: "knowledge-graph", object_type: "ai-path", object_id: "aip_session" } },
      ],
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={(owner) => opened.push(owner)} />);
    const list = await screen.findByTestId("ai-path-same-interaction");
    expect(list).toHaveTextContent("aip_session");
    expect(list).toHaveTextContent("recording");
    within(list).getByTestId("ai-path-sibling-aip_session").click();
    expect(opened).toEqual([expect.objectContaining({ object_type: "ai-path", object_id: "aip_session" })]);
  });
});


describe("the reader's words and the whole interaction (2026-09-05)", () => {
  it("names the ids a step chose and reads the interaction as one timeline", async () => {
    serve(200, {
      ...FINALIZED,
      steps: [
        { step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded",
          observed_at: "2026-09-05T10:00:01Z", chose: { "request.pivot.rows": ["mdm_DATE"], family: "table" } },
      ],
      names: { mdm_DATE: "date" },
      interaction: {
        paths: [{ path_id: "aip_1", state: "finalized" }, { path_id: "aip_exec", state: "finalized" }],
        steps: [
          { step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded",
            observed_at: "2026-09-05T10:00:01Z", path_id: "aip_1" },
          { step_order: 1, step_kind: "tool_call", tool_name: "execute_analyze_query_spec", outcome: "succeeded",
            observed_at: "2026-09-05T10:00:02Z", path_id: "aip_exec", path_step_order: 0, owner_workspace: "analyze",
            owner_object_type: "query-spec", owner_object_id: "qs_1", owner_label: "Kardinal crossing v1",
            owner_reference: { surface: "project", workspace: "analyze", section: "query-specs", object_type: "query-spec", object_id: "qs_1" } },
        ],
      },
    });
    const opened: unknown[] = [];
    render(<AiPathPage projectId="p1" pathId="aip_1" onOpenOwner={(owner) => opened.push(owner)} />);
    const chose = await screen.findByTestId("ai-path-chose-0");
    expect(chose).toHaveTextContent("date (mdm_DATE)");
    expect(screen.queryByText(/execute_analyze_query_spec/)).not.toBeInTheDocument();
    screen.getByTestId("ai-path-scope-interaction").click();
    expect(await screen.findAllByText(/execute_analyze_query_spec/)).not.toHaveLength(0);
    expect(screen.getAllByText(/Kardinal crossing v1/).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("ai-path-step-feedback-table-0")).not.toBeInTheDocument();
    // Round 5, B3: a merged row is routed like an own step -- the sibling's owner OPENS.
    screen.getByTestId("ai-path-owner-open-1").click();
    expect(opened).toEqual([expect.objectContaining({ object_type: "query-spec", object_id: "qs_1" })]);
    screen.getByTestId("ai-path-scope-path").click();
    await waitFor(() => expect(screen.queryByText(/execute_analyze_query_spec/)).not.toBeInTheDocument());
  });
});


describe("an inspection from the merged timeline is addressed to the owning path (round 5)", () => {
  it("maps the merged rank to the row's path and its ordinal there, or to nothing", async () => {
    const { interactionInspectionTarget } = await import("../connaissances/AiPathPage");
    const rows = [
      { step_order: 0, step_kind: "tool_call", path_id: "aip_1", path_step_order: 0 },
      { step_order: 1, step_kind: "tool_call", path_id: "aip_exec", path_step_order: 0 },
      { step_order: 2, step_kind: "tool_call", path_id: "aip_1", path_step_order: 1 },
    ] as never[];
    expect(interactionInspectionTarget(rows, 1)).toEqual({ pathId: "aip_exec", stepOrdinal: 0 });
    expect(interactionInspectionTarget(rows, 2)).toEqual({ pathId: "aip_1", stepOrdinal: 1 });
    expect(interactionInspectionTarget(rows, 7)).toBeNull();
    expect(interactionInspectionTarget(rows, null)).toBeNull();
    expect(interactionInspectionTarget(null, 0)).toBeNull();
  });
});


describe("an inspection from « Whole interaction » reaches the owning path (round 6)", () => {
  it("POSTs to the sibling's path with the sibling's own ordinal, never to the page's path", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const body = {
      ...FINALIZED,
      steps: [
        { step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded",
          observed_at: "2026-09-05T10:00:01Z" },
      ],
      interaction: {
        paths: [{ path_id: "aip_1", state: "finalized" }, { path_id: "aip_exec", state: "recording" }],
        steps: [
          { step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded",
            observed_at: "2026-09-05T10:00:01Z", path_id: "aip_1", path_step_order: 0 },
          { step_order: 1, step_kind: "knowledge_read", tool_name: "search_context", outcome: "succeeded",
            observed_at: "2026-09-05T10:00:02Z", path_id: "aip_exec", path_step_order: 3, branch_evidence: STALE_BRANCH_EVIDENCE },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        return Promise.resolve({
          ok: true,
          status: init?.method === "POST" ? 201 : 200,
          json: () => Promise.resolve(init?.method === "POST" ? { id: "evi_1" } : body),
        } as Response);
      }),
    );
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    await screen.findByTestId("ai-path");
    screen.getByTestId("ai-path-scope-interaction").click();
    const toggles = await screen.findAllByTestId("ai-path-branch-toggle");
    fireEvent.click(toggles[0]);
    await waitFor(() => expect(calls.filter((c) => c.init?.method === "POST").length).toBe(1));
    const post = calls.find((c) => c.init?.method === "POST")!;
    expect(post.url).toBe("/api/projects/p1/context/ai-paths/aip_exec/inspections");
    expect(JSON.parse(String(post.init?.body)).step_ordinal).toBe(3);
  });
});


describe("the drawing bound is said to the reader (round 6)", () => {
  it("names how many rows are shown of how many, and that the judgement reads them all", async () => {
    serve(200, {
      ...FINALIZED,
      steps: [{ step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded", observed_at: "2026-09-05T10:00:01Z" }],
      interaction: {
        paths: [{ path_id: "aip_1", state: "finalized" }, { path_id: "aip_exec", state: "finalized" }],
        steps: [
          { step_order: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded", observed_at: "2026-09-05T10:00:01Z", path_id: "aip_1", path_step_order: 0 },
          { step_order: 1, step_kind: "tool_call", tool_name: "execute_analyze_query_spec", outcome: "succeeded", observed_at: "2026-09-05T10:00:02Z", path_id: "aip_exec", path_step_order: 0 },
        ],
        truncated: true,
        total_steps: 250,
      },
    });
    render(<AiPathPage projectId="p1" pathId="aip_1" />);
    const toggle = await screen.findByTestId("ai-path-scope-interaction");
    expect(toggle).toHaveTextContent("2 steps, the last of 250; the judgement reads them all");
  });
});
