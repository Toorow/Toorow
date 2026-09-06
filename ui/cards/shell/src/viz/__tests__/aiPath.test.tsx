/**
 * Story 55.1 -- the `ai_path` family: one drawing, two honest states, and a
 * reading grid that cannot drift from its owner.
 *
 * The parity tests read `server/core/ai_path_recorder.py` and
 * `server/core/visualization_families.py` from the Python source, the way
 * `responsive.test.ts` reads the profile enum. Restating the vocabulary a second
 * time in TypeScript is exactly how the two halves part ways silently.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import AiPathFamily, {
  AI_PATH_FALLBACK_COLUMNS,
  AI_PATH_LEVELS,
  AI_PATH_NODE_STATES,
  AI_PATH_STATE_STATEMENT,
  NO_AI_PATH,
  aiPathFallbackRows,
  aiPathRungs,
  aiPathStepsFromWire,
  levelOfStep,
  nodeStateOfStep,
  type AiPathStep,
} from "../renderers/aiPath";
import { aiPathFallbackRows as _rows_unused, describeChoice } from "../renderers/aiPath";
import { registeredCapabilities, resolveRenderer } from "../registry";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../..");
const RECORDER_SOURCE = resolve(REPO_ROOT, "server/core/ai_path_recorder.py");
const FAMILIES_SOURCE = resolve(REPO_ROOT, "server/core/visualization_families.py");

const WALK: AiPathStep[] = [
  {
    id: "s1",
    ordinal: 1,
    step_kind: "skill_step",
    tool_name: "daily_briefing",
    outcome: "succeeded",
    owner_workspace: "analyze",
    owner_object_type: "skill",
    owner_object_id: "sk_example",
  },
  {
    id: "s2",
    ordinal: 2,
    step_kind: "tool_call",
    tool_name: "get_procedure",
    outcome: "succeeded",
    owner_object_type: "procedure",
    owner_object_id: "pr_example",
  },
  {
    id: "s3",
    ordinal: 3,
    step_kind: "knowledge_read",
    tool_name: "search_context",
    outcome: "unavailable",
    owner_object_type: "knowledge",
    owner_object_id: "kn_example",
  },
  // Reached nothing governed. It stays drawn (AC8).
  { id: "s4", ordinal: 4, step_kind: "tool_call", tool_name: "health", outcome: "succeeded" },
  // A kind that names no rung -- `level_of` returns None for `handoff`.
  { id: "s5", ordinal: 5, step_kind: "handoff", outcome: "succeeded" },
];

function publicEvidence(count: number, ordinal: number) {
  return {
    schema_version: "retrieval-branch-evidence.v1",
    state: "branches_listed",
    walk: {
      producer: "context_search",
      mode: "lexical",
      graph_hop_depth: 1,
      semantic_recall: false,
      selection_limit: 24,
      judged_count: count,
      selected_count: count,
      rejected_count: 0,
      listed_count: count,
      listing_truncated: false,
      not_reached_enumerated: false,
      tier_scale: { title: 3, description: 2, neighbor: 1 },
    },
    branches: Array.from({ length: count }, (_, index) => ({
      id: `topic.${ordinal}.${index}`,
      kind: "topic",
      title: `Topic ${ordinal} ${index}`,
      score: 3,
      tier: "title",
      matched: true,
      rank: index + 1,
      fate: "selected",
      reason: null,
    })),
  };
}

// ---------------------------------------------------------------------------
// The reading grid mirrors its owner.
// ---------------------------------------------------------------------------

describe("the reading grid has one definition site", () => {
  it("declares the same five rungs the recorder declares", () => {
    const source = readFileSync(RECORDER_SOURCE, "utf8");
    const match = source.match(/^LEVELS\s*=\s*\(([^)]*)\)/m);
    expect(match, "LEVELS moved in ai_path_recorder.py; this test must follow it").toBeTruthy();
    const names = [...match![1]!.matchAll(/LEVEL_([A-Z]+)/g)].map((m) => m[1]!);
    expect(names).toEqual([...AI_PATH_LEVELS]);
  });

  it("classifies every step the way `level_of` does, including the tie-breaks", () => {
    expect(levelOfStep(WALK[0]!)).toBe("SKILL");
    // PROCEDURE and CONTEXT share a step_kind: a get_procedure IS a tool_call.
    expect(levelOfStep(WALK[1]!)).toBe("PROCEDURE");
    expect(levelOfStep(WALK[2]!)).toBe("CONTEXT");
    expect(levelOfStep(WALK[3]!)).toBe("TOOL");
    // The standing null: a recorded kind that names no rung.
    expect(levelOfStep(WALK[4]!)).toBeNull();
    expect(levelOfStep({ step_kind: null })).toBeNull();
  });

  it("routes an owner_object_type of `procedure` to PROCEDURE even without the tool name", () => {
    expect(levelOfStep({ step_kind: "tool_call", owner_object_type: "procedure" })).toBe(
      "PROCEDURE",
    );
  });

  it("normalizes the existing owner wire Skill pin without exposing a second shape", () => {
    const steps = aiPathStepsFromWire([
      {
        step_order: 0,
        step_kind: "skill_step",
        outcome: "succeeded",
        skill: {
          skill_version_id: "proc_daily@7",
          skill_step_id: "2",
          state: "resolved",
          label: "Read the governed daily view",
        },
      },
    ]);
    expect(steps[0]?.skill).toEqual({
      version_id: "proc_daily@7",
      step_id: "2",
      state: "resolved",
      label: "Read the governed daily view",
    });
  });

  it("maps only closed public branch evidence and drops raw producer detail", () => {
    const steps = aiPathStepsFromWire([{
      step_order: 0,
      step_kind: "knowledge_read",
      tool_name: "search_context",
      outcome: "succeeded",
      branch_evidence: {
        schema_version: "retrieval-branch-evidence.v1",
        state: "branches_not_recorded",
      },
      detail: { candidate_ids: ["must-not-reach-ui"] },
    } as never]);
    expect(steps[0]?.branch_evidence).toEqual({
      schema_version: "retrieval-branch-evidence.v1",
      state: "branches_not_recorded",
    });
    expect(steps[0]).not.toHaveProperty("detail");
  });

  it("fails closed when an eligible API step omits or contradicts public evidence", () => {
    const [missing, mismatched] = aiPathStepsFromWire([
      { step_order: 0, step_kind: "knowledge_read", tool_name: "search_context" },
      {
        step_order: 1,
        step_kind: "knowledge_read",
        tool_name: "search_context",
        branch_evidence: {
          schema_version: "retrieval-branch-evidence.v1",
          state: "no_branch_judged",
          walk: {
            producer: "briefing_context_event",
            mode: "event_pairing",
            graph_hop_depth: 0,
            semantic_recall: false,
            selection_limit: 1,
            judged_count: 0,
            selected_count: 0,
            rejected_count: 0,
            listed_count: 0,
            listing_truncated: false,
            not_reached_enumerated: false,
            tier_scale: null,
          },
          branches: [],
        },
      },
    ]);
    for (const step of [missing, mismatched]) {
      expect(step?.branch_evidence).toEqual({
        schema_version: "retrieval-branch-evidence.v1",
        state: "unavailable",
      });
    }
  });
});

// ---------------------------------------------------------------------------
// AC5 -- two states, and the screen says so.
// ---------------------------------------------------------------------------

describe("AC5 -- the states are what the data carries", () => {
  it("declares exactly two node states", () => {
    expect([...AI_PATH_NODE_STATES]).toEqual(["completed", "failed"]);
  });

  it("mirrors `STEP_STATES`, which carries no pending and no active", () => {
    const source = readFileSync(RECORDER_SOURCE, "utf8");
    const match = source.match(/^STEP_STATES\s*=\s*\(([^)]*)\)/m);
    expect(match).toBeTruthy();
    const emitted = match![1]!;
    expect(emitted).not.toContain("PENDING");
    expect(emitted).not.toContain("ACTIVE");
  });

  it("draws succeeded as completed and every other outcome as failed", () => {
    expect(nodeStateOfStep({ outcome: "succeeded" })).toBe("completed");
    for (const outcome of ["failed", "refused", "unavailable", null]) {
      expect(nodeStateOfStep({ outcome })).toBe("failed");
    }
  });

  it("renders no node as active or pending on a finished path, and says why", () => {
    const { container } = render(<AiPathFamily steps={WALK} lifecycle="finalized" />);
    const states = [...container.querySelectorAll("[data-node-state]")].map(
      (node) => node.getAttribute("data-node-state"),
    );
    expect(states.length).toBe(WALK.length);
    expect(states).not.toContain("active");
    expect(states).not.toContain("pending");
    expect(new Set(states)).toEqual(new Set(["completed", "failed"]));
    // The reduction is stated, not hidden: a third state would be theatre.
    expect(screen.getByTestId("ai-path-state-statement").textContent).toBe(
      AI_PATH_STATE_STATEMENT,
    );
  });
});

// ---------------------------------------------------------------------------
// AC6 -- a recording path is not evidence.
// ---------------------------------------------------------------------------

describe("AC6 -- recording is not evidence, and the header says so", () => {
  it("renders a different header for a recording path than for a finalized one", () => {
    const recording = render(<AiPathFamily steps={WALK} lifecycle="recording" />);
    expect(screen.getByText("Still recording — not evidence")).toBeTruthy();
    expect(
      recording.container.querySelector('[data-ai-path-lifecycle="recording"]'),
    ).toBeTruthy();
    expect(screen.getByText(/cannot be pinned as evidence/)).toBeTruthy();
    recording.unmount();

    const finalized = render(<AiPathFamily steps={WALK} lifecycle="finalized" />);
    expect(screen.getByText("Observed walk")).toBeTruthy();
    expect(screen.queryByText("Still recording — not evidence")).toBeNull();
    expect(finalized.container.querySelector('[data-ai-path-lifecycle="final"]')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// AC7 -- the literal.
// ---------------------------------------------------------------------------

describe("AC7 -- a human-only result states `No AI path`", () => {
  it("renders the exact literal and no drawing", () => {
    const { container } = render(<AiPathFamily steps={[]} humanOnly />);
    expect(screen.getByText(NO_AI_PATH)).toBeTruthy();
    expect(NO_AI_PATH).toBe("No AI path");
    // An empty drawing would read as a path that was lost.
    expect(container.querySelector('[data-testid="ai-path-timeline"]')).toBeNull();
    expect(container.querySelector('[data-testid="ai-path-table"]')).toBeNull();
  });

  it("is not the same screen as a path with no step", () => {
    render(<AiPathFamily steps={[]} lifecycle="finalized" />);
    expect(screen.getByTestId("ai-path-empty").textContent).toContain("No step recorded");
    expect(screen.queryByText(NO_AI_PATH)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// AC8 -- a node that reached nothing governed stays visible.
// ---------------------------------------------------------------------------

describe("AC8 -- nothing governed is drawn, never dropped", () => {
  it("keeps the step and states what it reached, without inventing an object", () => {
    const { container } = render(<AiPathFamily steps={WALK} />);
    const unreached = container.querySelectorAll('[data-node-reached="none"]');
    expect(unreached.length).toBe(2); // the `health` call and the `handoff`
    expect(within(unreached[0] as HTMLElement).getByText("nothing governed")).toBeTruthy();
  });

  it("keeps a step whose kind names no rung, labelled as naming none", () => {
    const { container } = render(<AiPathFamily steps={WALK} />);
    expect(container.querySelector('[data-ai-path-rung="none"]')).toBeTruthy();
    // The rung is now a PER-ROW label on the timeline, in walk order -- for
    // WALK each step sits on a different rung, so the sequence is unchanged.
    const rungs = [...container.querySelectorAll("[data-ai-path-rung]")].map((n) =>
      n.getAttribute("data-ai-path-rung"),
    );
    expect(rungs).toEqual(["SKILL", "PROCEDURE", "CONTEXT", "TOOL", "none"]);
  });

  it("draws the JOB rung only when the caller has a job identity", () => {
    expect(aiPathRungs(WALK).some((rung) => rung.level === "JOB")).toBe(false);
    expect(
      aiPathRungs(WALK, { label: "render_analyze_result", outcome: "succeeded" }).some(
        (rung) => rung.level === "JOB",
      ),
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// The timeline: one row per step, in the walk's own order, kinds told apart
// by name and marker -- never by colour alone.
// ---------------------------------------------------------------------------

describe("the timeline is the walk, in the walk's own order", () => {
  it("orders rows by the step's own ordinal, not the array order", () => {
    // Ordinals 4 then 1: the drawing must put `daily_briefing` first anyway.
    const shuffled: AiPathStep[] = [WALK[3]!, WALK[0]!];
    const { container } = render(<AiPathFamily steps={shuffled} lifecycle="finalized" />);
    const rows = [...container.querySelectorAll("[data-ai-path-step]")];
    // What the reader sees is the 1-based label; what is recorded stays the
    // step's own ordinal (AI-134) -- asserted at the page level.
    expect(rows.map((row) => row.getAttribute("data-ai-path-ordinal"))).toEqual(["2", "5"]);
    expect(within(rows[0] as HTMLElement).getAllByText("daily_briefing").length).toBeGreaterThan(
      0,
    );
    const tableRows = screen.getByTestId("ai-path-table").querySelectorAll("tbody tr");
    expect(tableRows[0]).toHaveTextContent("daily_briefing");
    expect(tableRows[1]).toHaveTextContent("health");
  });

  it("tells kinds apart by a named marker and the printed kind, never colour alone", () => {
    const { container } = render(<AiPathFamily steps={WALK} />);
    const rows = [...container.querySelectorAll("[data-ai-path-step]")];
    expect(rows.map((row) => row.getAttribute("data-ai-path-kind"))).toEqual([
      "skill_step",
      "tool_call",
      "knowledge_read",
      "tool_call",
      "handoff",
    ]);
    // The kind is also printed as text on the row.
    expect(screen.getAllByText("skill_step").length).toBeGreaterThan(0);
    expect(screen.getAllByText("knowledge_read").length).toBeGreaterThan(0);
    expect(screen.getAllByText("handoff").length).toBeGreaterThan(0);
    // And every row carries its named glyph.
    for (const row of rows) {
      expect(row.querySelector("svg")).toBeTruthy();
    }
  });
});

// ---------------------------------------------------------------------------
// AC9 -- the accessible table fallback.
// ---------------------------------------------------------------------------

describe("AC9 -- the declared fallback wells produce a real table", () => {
  it("uses exactly the wells `visualization_families.py` declares, in order", () => {
    const source = readFileSync(FAMILIES_SOURCE, "utf8");
    const family = source.slice(source.indexOf("_AI_PATH = VisualFamily("));
    const match = family.match(/table_fallback_wells=\(([^)]*)\)/);
    expect(match, "the ai_path fallback tuple moved; this test must follow it").toBeTruthy();
    const wells = [...match![1]!.matchAll(/"([^"]+)"/g)].map((m) => m[1]!);
    expect(wells).toEqual(AI_PATH_FALLBACK_COLUMNS.map((c) => c.well));
    expect(wells.length).toBeGreaterThan(0);
  });

  it("renders one row per step with the four declared columns", () => {
    render(<AiPathFamily steps={WALK} />);
    const table = screen.getByTestId("ai-path-table");
    const headers = [...table.querySelectorAll("th[data-fallback-well]")].map((th) =>
      th.getAttribute("data-fallback-well"),
    );
    expect(headers).toEqual(AI_PATH_FALLBACK_COLUMNS.map((c) => c.well));
    expect(table.querySelectorAll("tbody tr").length).toBe(WALK.length);
    const scroller = table.parentElement;
    expect(scroller).toHaveAttribute("role", "region");
    expect(scroller).toHaveAttribute("aria-label", "AI Path step table");
    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(scroller?.className).toContain("focus-visible:");
  });

  it("applies the path allocator to API steps in persisted ordinal order", () => {
    const wire = [
      { step_order: 10, step_kind: "knowledge_read", tool_name: "search_context",
        branch_evidence: publicEvidence(1, 10) },
      { step_order: 9, step_kind: "knowledge_read", tool_name: "search_context",
        branch_evidence: publicEvidence(1, 9) },
      ...Array.from({ length: 8 }, (_, ordinal) => ({
        step_order: ordinal,
        step_kind: "knowledge_read",
        tool_name: "search_context",
        branch_evidence: publicEvidence(24, ordinal),
      })),
      { step_order: 8, step_kind: "knowledge_read", tool_name: "search_context",
        branch_evidence: publicEvidence(8, 8) },
    ];
    const decoded = aiPathStepsFromWire(wire);
    const byOrdinal = new Map(decoded.map((step) => [step.ordinal, step.branch_evidence]));
    expect(byOrdinal.get(8)?.state).toBe("branches_listed");
    expect(byOrdinal.get(9)?.state).toBe("unavailable");
    expect(byOrdinal.get(10)?.state).toBe("unavailable");
  });

  it("carries the same values the drawing carries, outcome word by outcome word", () => {
    const rows = aiPathFallbackRows(WALK);
    expect(rows.map((r) => r.dimension)).toEqual([
      "Skill · skill_step",
      "Procedure · tool_call",
      "Context · knowledge_read",
      "Tool · tool_call",
      "No rung · handoff",
    ]);
    // `unavailable` is not merged into `failed` here: the drawing groups the two
    // node states, the table keeps the recorded word.
    expect(rows.map((r) => r.color)).toEqual([
      "succeeded", "succeeded", "unavailable", "succeeded", "succeeded",
    ]);
    expect(rows[3]!.detail).toBe("nothing governed");
  });
});

// ---------------------------------------------------------------------------
// The console's owner navigation is a slot, not a second drawing.
// ---------------------------------------------------------------------------

describe("the owner affordance is offered, never assumed", () => {
  it("renders plain text when no navigation is supplied", () => {
    render(<AiPathFamily steps={[WALK[0]!]} />);
    // Twice on purpose: once in the drawing, once in the table fallback, which
    // carries the same values rather than a summary of them.
    expect(screen.getAllByText(/skill sk_example/).length).toBe(2);
  });

  it("lets the console attach its own owner link", () => {
    render(
      <AiPathFamily
        steps={[WALK[0]!]}
        renderOwner={(step) => <a href={`#${step.owner_object_id}`}>open owner</a>}
      />,
    );
    expect(screen.getByText("open owner")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// AC2 / AC4 -- the substrate contract.
// ---------------------------------------------------------------------------

describe("AC2 -- the component obeys the visual vocabulary", () => {
  const source = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "../renderers/aiPath.tsx"),
    "utf8",
  );

  it("carries no hard-coded colour", () => {
    expect(source.match(/#[0-9A-Fa-f]{6}/g)).toBeNull();
  });

  it("imports no stylesheet and no legacy framework", () => {
    expect(source.match(/^import .*\.css/m)).toBeNull();
    // Split so this assertion does not itself become a hit for `guards.test.ts`,
    // which greps every file under `viz/` for the literal.
    expect(source).not.toContain(["@m", "ui/"].join(""));
  });

  it("takes its colours from the palette contract", () => {
    expect(source).toContain("VizPalette");
    expect(source).toContain("colours.accent");
    expect(source).toContain("colours.hairline");
  });
});

describe("the spec route stays shut, and says why", () => {
  it("refuses `ai_path` from `resolveRenderer` with a message that is true", () => {
    const resolution = resolveRenderer("ai_path", 1, "console");
    expect(resolution.kind).toBe("refused");
    if (resolution.kind !== "refused") return;
    expect(resolution.message).toContain("bounded capability");
    // It must NOT claim the renderer is unbuilt: it is built, right here.
    expect(resolution.message).not.toContain("not built in this release");
  });

  it("keeps the family declared as a capability, not a false unbuilt entry", () => {
    expect(registeredCapabilities().map((entry) => entry.family)).toContain("ai_path");
  });
});


describe("a fallback row says the name and the choice (2026-09-05)", () => {
  it("prefers the owner's label to its id and appends the choice, bounded", () => {
    const rows = aiPathFallbackRows([
      {
        ordinal: 0,
        step_kind: "tool_call",
        tool_name: "compose_analyze_pivot",
        outcome: "succeeded",
        owner_workspace: "governance",
        owner_object_type: "semantic-view",
        owner_object_id: "sv_1",
        owner_version_id: "svv_1",
        owner_label: "Kardinal crossing",
        chose: { family: "table", "request.pivot.rows": ["mdm_date"], a: 1, b: 2, c: 3 },
        path_id: "aip_session",
      },
    ]);
    expect(rows[0]?.detail).toContain("semantic-view Kardinal crossing (sv_1)");
    expect(rows[0]?.detail).toContain("version svv_1");
    expect(rows[0]?.detail).toContain("chose family=table, request.pivot.rows=mdm_date, a=1, b=2, +1 more");
    expect(rows[0]?.detail).toContain("path aip_session");
    expect(describeChoice(null)).toBeNull();
    expect(describeChoice({})).toBeNull();
    // The timeline item itself says what was chosen (round 5), not only the fallback table.
    const { container } = render(
      <AiPathFamily
        steps={[{ ordinal: 0, step_kind: "tool_call", tool_name: "compose_analyze_pivot", outcome: "succeeded", chose: { family: "table" } }]}
        lifecycle="finalized"
      />,
    );
    expect(container.querySelector("[data-ai-path-chose]")?.textContent).toBe("chose family=table");
    // The server left five keys out and said so: the reader is told six more, not one.
    expect(describeChoice({ a: 1, b: 2, c: 3, d: 4, e: 5, "…": "5 more" })).toBe("chose a=1, b=2, c=3, d=4, +6 more");
    expect(describeChoice({ a: 1, "…": "nonsense" })).toBe("chose a=1");
    // Only the count survived the byte budget: the reader is told, not shown an empty choice.
    expect(describeChoice({ "…": "2 more" })).toBe("chose 2 values not shown");
    expect(describeChoice({ "…": "1 more" })).toBe("chose 1 value not shown");
  });
});
