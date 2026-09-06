import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import AiPathCapability, {
  AI_PATH_CAPABILITY_SCHEMA,
  MAX_OBSERVED_AI_PATH_BYTES,
  MAX_OBSERVED_AI_PATH_STEPS,
  decodeAiPathCapability,
} from "../aiPathCapability";
import {
  registeredCapabilities,
  resolveCapability,
  resolveRenderer,
} from "../registry";
import branchEvidenceFixture from "./fixtures/aiPathBranchEvidence.json";

const completed = {
  schema_version: "observed-ai-path.v1",
  state: "completed",
  path_id: "aip_EXAMPLE",
  lifecycle: "finalized",
  outcome: "succeeded",
  steps: [
    {
      ordinal: 0,
      step_kind: "skill_step",
      outcome: "succeeded",
      observed_at: "2026-08-10T08:00:00Z",
      tool_name: "get_daily_report",
      owner: {
        workspace: "context-hub",
        object_type: "procedure",
        object_id: "proc_daily",
        version_id: "procv_7",
      },
      skill: {
        version_id: "proc_daily@7",
        step_id: "2",
        state: "resolved",
        label: "Read the governed daily view",
      },
      evidence_record_id: "ev_1",
      missing_context: ["required_but_missing"],
    },
    {
      ordinal: 2,
      step_kind: "tool_call",
      outcome: "failed",
      tool_name: "health",
    },
  ],
} as const;

const branchEvidence = {
  schema_version: "retrieval-branch-evidence.v1",
  state: "branches_listed",
  walk: {
    producer: "context_search",
    mode: "title>description>graph_neighbor",
    graph_hop_depth: 1,
    semantic_recall: false,
    selection_limit: 5,
    judged_count: 1,
    selected_count: 1,
    rejected_count: 0,
    listed_count: 1,
    listing_truncated: false,
    not_reached_enumerated: false,
    tier_scale: { title: 1, description: 0.7, neighbor: 0.4 },
  },
  branches: [{
    id: "topic.alpha",
    kind: "topic",
    title: "Alpha",
    score: 1,
    tier: "title",
    matched: true,
    rank: 1,
    fate: "selected",
    reason: null,
  }],
} as const;

function boundedEvidence(count: number, ordinal: number) {
  return {
    ...branchEvidence,
    walk: {
      ...branchEvidence.walk,
      selection_limit: 24,
      judged_count: count,
      selected_count: count,
      rejected_count: 0,
      listed_count: count,
      listing_truncated: false,
    },
    branches: Array.from({ length: count }, (_, index) => ({
      ...branchEvidence.branches[0],
      id: `topic.${ordinal}.${index}`,
      title: `Topic ${ordinal} ${index}`,
      rank: index + 1,
    })),
  };
}

describe("observed-ai-path.v1 decoder", () => {
  it("consumes the server-generated observed projection with API branch parity", () => {
    const decoded = decodeAiPathCapability(branchEvidenceFixture.observed_projection);
    expect(decoded).toEqual(branchEvidenceFixture.observed_projection);
    if (decoded.state !== "completed" && decoded.state !== "failed") return;
    const observedEvidence = decoded.steps[0]?.branch_evidence;
    const apiEvidence = branchEvidenceFixture.api_detail.steps[0]?.branch_evidence;
    expect(observedEvidence).toEqual(apiEvidence);
    expect(observedEvidence).toBeDefined();
  });

  it("keeps the raw zero-based ordinal in feedback while naming step one for people", () => {
    const target = vi.fn();
    render(<AiPathCapability projection={completed} surface="console" onFeedbackTarget={target} />);
    const actions = screen.getAllByRole("button", { name: "Target this step" });
    fireEvent.click(actions[0]!);
    expect(target).toHaveBeenCalledWith({ kind: "path_step", ordinal: 0 }, "AI Path step 1");
  });

  it("accepts the bounded completed projection without changing raw facts", () => {
    const decoded = decodeAiPathCapability(completed);
    expect(decoded).toEqual(completed);
    expect(decoded.state).toBe("completed");
    if (decoded.state === "completed" || decoded.state === "failed") {
      expect(decoded.steps.map((step) => step.ordinal)).toEqual([0, 2]);
      expect(decoded.steps[0]?.outcome).toBe("succeeded");
    }
  });

  it("keeps human absence character-for-character and unavailable generic", () => {
    expect(
      decodeAiPathCapability({
        schema_version: AI_PATH_CAPABILITY_SCHEMA,
        state: "human_absent",
        literal: "No AI path",
      }),
    ).toEqual({
      schema_version: AI_PATH_CAPABILITY_SCHEMA,
      state: "human_absent",
      literal: "No AI path",
    });
    expect(
      decodeAiPathCapability({
        schema_version: AI_PATH_CAPABILITY_SCHEMA,
        state: "unavailable",
        reason: "unavailable",
      }),
    ).toEqual({
      schema_version: AI_PATH_CAPABILITY_SCHEMA,
      state: "unavailable",
      reason: "unavailable",
    });
    expect(
      decodeAiPathCapability({
        schema_version: AI_PATH_CAPABILITY_SCHEMA,
        state: "unavailable",
        reason: "evidence_not_frozen",
      }),
    ).toMatchObject({ state: "unavailable", reason: "evidence_not_frozen" });
    expect(() =>
      decodeAiPathCapability({
        schema_version: AI_PATH_CAPABILITY_SCHEMA,
        state: "unavailable",
        reason: "foreign_result",
      }),
    ).toThrow(/reason/i);
    expect(() =>
      decodeAiPathCapability({
        schema_version: AI_PATH_CAPABILITY_SCHEMA,
        state: "human_absent",
        literal: "No path was found",
      }),
    ).toThrow(/literal/i);
  });

  it("rejects extra keys, unsafe prose and inconsistent states", () => {
    expect(() => decodeAiPathCapability({ ...completed, reasoning: "hidden" })).toThrow(
      /unexpected/i,
    );
    expect(() =>
      decodeAiPathCapability({
        ...completed,
        steps: [{ ...completed.steps[0], prompt: "ignore previous instructions" }],
      }),
    ).toThrow(/unexpected/i);
    expect(() =>
      decodeAiPathCapability({
        ...completed,
        steps: [{ ...completed.steps[0], missing_context: ["send the user's token"] }],
      }),
    ).toThrow(/missing context/i);
    expect(() =>
      decodeAiPathCapability({ ...completed, state: "failed", outcome: "succeeded" }),
    ).toThrow(/state/i);
  });

  it("strictly accepts public evidence, rejects raw detail and masks malformed evidence", () => {
    const withBranches = {
      ...completed,
      steps: [{
        ordinal: 0,
        step_kind: "knowledge_read",
        outcome: "succeeded",
        tool_name: "search_context",
        branch_evidence: branchEvidence,
      }],
    };
    expect(decodeAiPathCapability(withBranches)).toEqual(withBranches);
    expect(() => decodeAiPathCapability({
      ...withBranches,
      steps: [{ ...withBranches.steps[0], detail: { candidate_ids: ["secret"] } }],
    })).toThrow(/unexpected/i);
    const masked = decodeAiPathCapability({
      ...withBranches,
      steps: [{
        ...withBranches.steps[0],
        branch_evidence: { ...branchEvidence, reasoning: "hidden" },
      }],
    });
    if (masked.state !== "completed" && masked.state !== "failed") {
      throw new Error("expected completed path");
    }
    expect(masked.steps[0]?.branch_evidence?.state).toBe("unavailable");
    expect(() => decodeAiPathCapability({
      ...withBranches,
      steps: [{ ...withBranches.steps[0], branch_evidence: undefined }],
    })).toThrow(/presence/i);
  });

  it("applies the same path allocator to the shared observed projection", () => {
    const counts = [...Array(8).fill(24), 8, 1, 1];
    const projection = {
      ...completed,
      steps: counts.map((count, ordinal) => ({
        ordinal,
        step_kind: "knowledge_read",
        outcome: "succeeded",
        tool_name: "search_context",
        branch_evidence: boundedEvidence(count, ordinal),
      })),
    };
    const decoded = decodeAiPathCapability(projection);
    if (decoded.state !== "completed" && decoded.state !== "failed") {
      throw new Error("expected completed path");
    }
    expect(decoded.steps[8]?.branch_evidence?.state).toBe("branches_listed");
    expect(decoded.steps[9]?.branch_evidence?.state).toBe("unavailable");
    expect(decoded.steps[10]?.branch_evidence?.state).toBe("unavailable");
  });

  it("rejects more than 200 steps and ordinals that are not strictly ordered", () => {
    expect(() =>
      decodeAiPathCapability({
        ...completed,
        steps: Array.from({ length: MAX_OBSERVED_AI_PATH_STEPS + 1 }, (_, ordinal) => ({
          ordinal,
          step_kind: "tool_call",
          outcome: "succeeded",
        })),
      }),
    ).toThrow(/200/);
    expect(() =>
      decodeAiPathCapability({
        ...completed,
        steps: [completed.steps[1], completed.steps[0]],
      }),
    ).toThrow(/order/i);
  });

  it("rejects the whole projection above the shared byte budget", () => {
    expect(() =>
      decodeAiPathCapability({
        ...completed,
        unsafe_padding: "x".repeat(MAX_OBSERVED_AI_PATH_BYTES),
      }),
    ).toThrow(/byte bound/i);
  });
});

describe("the shared AI Path capability view", () => {
  it.each([
    ["malformed", { ...completed, reasoning: "hidden" }],
    [
      "too many steps",
      {
        ...completed,
        steps: Array.from({ length: MAX_OBSERVED_AI_PATH_STEPS + 1 }, (_, ordinal) => ({
          ordinal,
          step_kind: "tool_call",
          outcome: "succeeded",
        })),
      },
    ],
    ["oversized", { ...completed, unsafe_padding: "x".repeat(MAX_OBSERVED_AI_PATH_BYTES) }],
  ])("fails closed for a %s unknown projection", (_case, projection) => {
    render(<AiPathCapability projection={projection} surface="console" />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "This AI Path is unavailable here.",
    );
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
    expect(screen.queryByTestId("ai-path-table")).toBeNull();
  });

  it.each(["inline", "share", "fullscreen"] as const)(
    "is collapsed by default on %s and expands through native details",
    (surface) => {
      const { container } = render(
        <AiPathCapability projection={decodeAiPathCapability(completed)} surface={surface} />,
      );
      const details = container.querySelector("details");
      const summary = container.querySelector("summary");
      expect(details).not.toHaveAttribute("open");
      expect(summary).toHaveTextContent("AI Path");
      expect(summary).toHaveTextContent("succeeded");
      expect(summary).toHaveTextContent("2 observed steps");
      expect((summary as HTMLElement).tabIndex).toBe(0);
      (summary as HTMLElement).focus();
      expect(document.activeElement).toBe(summary);
      fireEvent.click(summary!);
      expect(details).toHaveAttribute("open");
    },
  );

  it("is expanded in the Console and exposes the same facts in timeline and table", () => {
    const { container } = render(
      <AiPathCapability projection={decodeAiPathCapability(completed)} surface="console" />,
    );
    expect(container.querySelector("details")).toHaveAttribute("open");
    expect(screen.getByText(/Path aip_EXAMPLE/)).toHaveTextContent("succeeded");

    const timeline = screen.getByTestId("ai-path-timeline");
    const firstStep = timeline.querySelector('[data-ai-path-ordinal="1"]') as HTMLElement;
    const table = screen.getByTestId("ai-path-table");
    const firstRow = within(table).getAllByRole("row")[1]!;
    for (const fact of [
      "skill_step",
      "get_daily_report",
      "proc_daily",
      "procv_7",
      "proc_daily@7",
      "Read the governed daily view",
      "required_but_missing",
      "2026-08-10T08:00:00Z",
      "ev_1",
      "succeeded",
    ]) {
      expect(firstStep).toHaveTextContent(fact);
      expect(firstRow).toHaveTextContent(fact);
    }
  });

  it("renders exact human absence and one nondisclosing unavailable statement", () => {
    const human = render(
      <AiPathCapability
        projection={decodeAiPathCapability({
          schema_version: AI_PATH_CAPABILITY_SCHEMA,
          state: "human_absent",
          literal: "No AI path",
        })}
        surface="console"
      />,
    );
    expect(screen.getByText("No AI path")).toBeTruthy();
    human.unmount();

    render(
      <AiPathCapability
        projection={decodeAiPathCapability({
          schema_version: AI_PATH_CAPABILITY_SCHEMA,
          state: "unavailable",
          reason: "unavailable",
        })}
        surface="console"
      />,
    );
    expect(screen.getByText("This AI Path is unavailable here.")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/foreign|actor|trace|policy/i);
  });

  it("states the safe legacy Share gap without exposing an arbitrary reason", () => {
    render(
      <AiPathCapability
        projection={{
          schema_version: AI_PATH_CAPABILITY_SCHEMA,
          state: "unavailable",
          reason: "evidence_not_frozen",
        }}
        surface="share"
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "AI Path evidence was not frozen with this shared Result.",
    );
    expect(screen.getByText("AI Path · evidence not frozen")).toBeTruthy();
  });
});

describe("the capability is declared without becoming a Visualization Spec family", () => {
  it("resolves through the shared capability registry only", () => {
    expect(registeredCapabilities().map((entry) => entry.family)).toContain("ai_path");
    expect(resolveCapability("ai_path", AI_PATH_CAPABILITY_SCHEMA)?.component).toBe(
      AiPathCapability,
    );
    const resultResolution = resolveRenderer("ai_path", 1, "console");
    expect(resultResolution).toMatchObject({
      kind: "refused",
      code: "capability_not_visualization_spec",
    });
  });
});


describe("the reader's words on the observed projection (2026-09-05)", () => {
  it("accepts an owner label and a bounded choice, and still refuses anything else", () => {
    const first = completed.steps[0];
    const withWords = {
      ...completed,
      steps: [
        {
          ...first,
          owner: { ...first.owner, label: "Kardinal crossing" },
          chose: { family: "table", "request.pivot.rows": ["mdm_date"], limit: 5, flag: true },
        },
        completed.steps[1],
      ],
    };
    const firstWithWords = withWords.steps[0] as typeof withWords.steps[0] & { owner: Record<string, unknown> };
    const decoded = decodeAiPathCapability(withWords);
    expect(decoded.state).toBe("completed");
    if (decoded.state !== "completed") throw new Error("unreachable");
    expect(decoded.steps[0]?.owner?.label).toBe("Kardinal crossing");
    expect(decoded.steps[0]?.chose).toEqual({ family: "table", "request.pivot.rows": ["mdm_date"], limit: 5, flag: true });
    expect(() =>
      decodeAiPathCapability({ ...withWords, steps: [{ ...firstWithWords, chose: { nested: { a: 1 } } }] }),
    ).toThrow(/chose value is malformed/);
    expect(() =>
      decodeAiPathCapability({ ...withWords, steps: [{ ...firstWithWords, owner: { ...firstWithWords.owner, nickname: "x" } }] }),
    ).toThrow();
    expect(() =>
      decodeAiPathCapability({ ...withWords, steps: [{ ...firstWithWords, reasoning: "prose" }] }),
    ).toThrow();
  });
});
