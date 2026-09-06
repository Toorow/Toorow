import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import AiPathBranchSubtree, {
  BRANCHES_NOT_RECORDED,
  MAX_BRANCHES_PER_PATH,
  MAX_BRANCH_EVIDENCE_BYTES_PER_PATH,
  NOT_REACHED_NOTICE,
  RETRIEVAL_BRANCH_EVIDENCE_SCHEMA,
  SELECTED_MEANING,
  allocateAiPathBranchEvidence,
  aiPathBranchesText,
  branchEvidenceCanonicalBytes,
  decodeAiPathBranchEvidence,
  pythonFloatJson,
  safeInspect,
  type AiPathBranchEvidence,
} from "../renderers/aiPathBranches";

const EVIDENCE: AiPathBranchEvidence = {
  schema_version: "retrieval-branch-evidence.v1",
  state: "branches_listed",
  walk: {
    producer: "context_search",
    mode: "title>description>graph_neighbor",
    graph_hop_depth: 1,
    semantic_recall: false,
    selection_limit: 5,
    judged_count: 3,
    selected_count: 2,
    rejected_count: 1,
    listed_count: 2,
    listing_truncated: true,
    not_reached_enumerated: false,
    tier_scale: { title: 1, description: 0.7, neighbor: 0.4 },
  },
  branches: [
    {
      id: "topic.alpha",
      kind: "topic",
      title: "Alpha",
      score: 1,
      tier: "title",
      matched: true,
      rank: 1,
      fate: "selected",
      reason: null,
    },
    {
      id: "procedure.beta",
      kind: "procedure",
      title: "Beta",
      score: 0.6,
      tier: "description",
      matched: false,
      rank: 2,
      fate: "rejected",
      reason: "below_cutoff",
    },
  ],
};

describe("retrieval-branch-evidence.v1 decoder", () => {
  it.each([
    [1.0, "1.0"],
    [-0.0, "-0.0"],
    [1e-7, "1e-07"],
    [1e-5, "1e-05"],
    [1e15, "1000000000000000.0"],
    [1e16, "1e+16"],
    [1e20, "1e+20"],
    [1e21, "1e+21"],
    [0.1 + 0.2, "0.30000000000000004"],
    [1.2345678901234567, "1.2345678901234567"],
    [1.0000000000000002, "1.0000000000000002"],
    [2.2250738585072014e-308, "2.2250738585072014e-308"],
    [5e-324, "5e-324"],
    [1.7976931348623157e308, "1.7976931348623157e+308"],
    [0.00012345678901234567, "0.00012345678901234567"],
    [999999999999999.9, "999999999999999.9"],
  ])("formats %s exactly like Python float repr", (value, expected) => {
    expect(pythonFloatJson(value)).toBe(expected);
  });

  it("accepts the closed public object without changing its facts", () => {
    expect(RETRIEVAL_BRANCH_EVIDENCE_SCHEMA).toBe("retrieval-branch-evidence.v1");
    expect(decodeAiPathBranchEvidence(EVIDENCE)).toEqual(EVIDENCE);
  });

  it.each([
    ["raw detail", { ...EVIDENCE, candidate_ids: ["secret"] }],
    ["unknown root key", { ...EVIDENCE, reasoning: "hidden" }],
    ["not reached candidate", {
      ...EVIDENCE,
      branches: [{ ...EVIDENCE.branches[0], fate: "not_reached" }],
    }],
    ["bad count", { ...EVIDENCE, walk: { ...EVIDENCE.walk, judged_count: 4 } }],
    ["bad rank", {
      ...EVIDENCE,
      branches: [EVIDENCE.branches[0], { ...EVIDENCE.branches[1], rank: 1 }],
    }],
    ["bad producer metadata", {
      ...EVIDENCE,
      walk: { ...EVIDENCE.walk, producer: "briefing_context_event", tier_scale: null },
    }],
    ["unsafe id", {
      ...EVIDENCE,
      branches: [{ ...EVIDENCE.branches[0], id: "topic/secret" }],
    }],
    ["non NFC title", {
      ...EVIDENCE,
      branches: [{ ...EVIDENCE.branches[0], title: "Cafe\u0301" }],
    }],
    ["Unicode format control", {
      ...EVIDENCE,
      branches: [{ ...EVIDENCE.branches[0], title: "Alpha\u0600" }],
    }],
  ])("rejects %s", (_case, value) => {
    expect(() => decodeAiPathBranchEvidence(value)).toThrow(/branch evidence/i);
  });

  it("accepts exact stale states and refuses facts attached to them", () => {
    const stale = {
      schema_version: RETRIEVAL_BRANCH_EVIDENCE_SCHEMA,
      state: "branches_not_recorded",
    };
    expect(decodeAiPathBranchEvidence(stale)).toEqual(stale);
    expect(() => decodeAiPathBranchEvidence({ ...stale, branches: [] })).toThrow();
  });

  it("enforces the public per-step candidate and byte walls", () => {
    const branches = Array.from({ length: 25 }, (_, index) => ({
      ...EVIDENCE.branches[0],
      id: `topic.${index + 1}`,
      rank: index + 1,
    }));
    expect(() => decodeAiPathBranchEvidence({
      ...EVIDENCE,
      walk: {
        ...EVIDENCE.walk,
        judged_count: 25,
        selected_count: 25,
        rejected_count: 0,
        listed_count: 25,
        listing_truncated: false,
      },
      branches,
    })).toThrow(/bound/i);
    expect(() => decodeAiPathBranchEvidence({
      ...EVIDENCE,
      padding: "x".repeat(8_192),
    })).toThrow(/byte bound/i);
  });

  it("rejects selection counts above the retriever limit and non-canonical mode whitespace", () => {
    expect(() => decodeAiPathBranchEvidence({
      ...EVIDENCE,
      walk: { ...EVIDENCE.walk, selection_limit: 1, selected_count: 2 },
    })).toThrow(/selection limit/i);
    for (const mode of [" lexical", "lexical ", "lexical  match", "lexical\tmatch"]) {
      expect(() => decodeAiPathBranchEvidence({
        ...EVIDENCE,
        walk: { ...EVIDENCE.walk, mode },
      })).toThrow(/walk mode/i);
    }
  });

  it("counts Unicode code points like Python and rejects isolated surrogates", () => {
    const withTitle = (title: string) => ({
      ...EVIDENCE,
      branches: [{ ...EVIDENCE.branches[0], title }],
      walk: {
        ...EVIDENCE.walk,
        judged_count: 1,
        selected_count: 1,
        rejected_count: 0,
        listed_count: 1,
        listing_truncated: false,
      },
    });
    expect(decodeAiPathBranchEvidence(withTitle("😀".repeat(200)))).toBeTruthy();
    expect(() => decodeAiPathBranchEvidence(withTitle("😀".repeat(201)))).toThrow(
      /branch title/i,
    );
    expect(() => decodeAiPathBranchEvidence(withTitle("bad\ud800title"))).toThrow(
      /branch title/i,
    );
  });

  it("allocates candidates in ordinal order and closes at 200 candidates", () => {
    expect(MAX_BRANCHES_PER_PATH).toBe(200);
    const evidenceWith = (count: number, ordinal: number): AiPathBranchEvidence => ({
      schema_version: RETRIEVAL_BRANCH_EVIDENCE_SCHEMA,
      state: "branches_listed",
      walk: {
        ...EVIDENCE.walk,
        selection_limit: 24,
        judged_count: count,
        selected_count: count,
        rejected_count: 0,
        listed_count: count,
        listing_truncated: false,
      },
      branches: Array.from({ length: count }, (_, index) => ({
        ...EVIDENCE.branches[0],
        id: `topic.${ordinal}.${index}`,
        title: `Topic ${ordinal} ${index}`,
        rank: index + 1,
      })),
    });
    const inputs = [
      { ordinal: 10, eligible: true, evidence: evidenceWith(1, 10) },
      { ordinal: 9, eligible: true, evidence: evidenceWith(1, 9) },
      ...Array.from({ length: 8 }, (_, ordinal) => ({
        ordinal,
        eligible: true,
        evidence: evidenceWith(24, ordinal),
      })),
      { ordinal: 8, eligible: true, evidence: evidenceWith(8, 8) },
    ];
    const allocated = allocateAiPathBranchEvidence(inputs);
    const byOrdinal = new Map(inputs.map((input, index) => [input.ordinal, allocated[index]]));
    expect(byOrdinal.get(8)?.state).toBe("branches_listed");
    expect(byOrdinal.get(9)?.state).toBe("unavailable");
    expect(byOrdinal.get(10)?.state).toBe("unavailable");
  });

  it("accepts exactly 65,536 bytes and closes the crossing at 65,537", () => {
    expect(MAX_BRANCH_EVIDENCE_BYTES_PER_PATH).toBe(65_536);
    const evidenceWith = (modeLength: number, titleLength: number): AiPathBranchEvidence => ({
      ...EVIDENCE,
      walk: {
        ...EVIDENCE.walk,
        mode: "m".repeat(modeLength),
        judged_count: 1,
        selected_count: 1,
        rejected_count: 0,
        listed_count: 1,
        listing_truncated: false,
      },
      branches: [{ ...EVIDENCE.branches[0], title: "x".repeat(titleLength) }],
    });
    const base = evidenceWith(1, 1);
    const baseBytes = branchEvidenceCanonicalBytes(base);
    const count = Math.floor(MAX_BRANCH_EVIDENCE_BYTES_PER_PATH / baseBytes);
    expect(count).toBeLessThanOrEqual(MAX_BRANCHES_PER_PATH);
    const lengths = Array.from({ length: count }, () => ({ mode: 1, title: 1 }));
    let remainder = MAX_BRANCH_EVIDENCE_BYTES_PER_PATH - baseBytes * count;
    for (const length of lengths) {
      const titleExtra = Math.min(199, remainder);
      length.title += titleExtra;
      remainder -= titleExtra;
      const modeExtra = Math.min(127, remainder);
      length.mode += modeExtra;
      remainder -= modeExtra;
    }
    expect(remainder).toBe(0);
    const exact = lengths.map(({ mode, title }) => evidenceWith(mode, title));
    expect(exact.reduce((total, evidence) => total + branchEvidenceCanonicalBytes(evidence), 0))
      .toBe(MAX_BRANCH_EVIDENCE_BYTES_PER_PATH);
    const exactResult = allocateAiPathBranchEvidence(exact.map((evidence, ordinal) => ({
      ordinal,
      eligible: true,
      evidence,
    })));
    expect(exactResult.every((value) => value?.state === "branches_listed")).toBe(true);

    const adjustable = lengths.findIndex((length) => length.mode < 128 || length.title < 200);
    expect(adjustable).toBeGreaterThanOrEqual(0);
    const overflow = exact.map((evidence, index) => {
      if (index !== adjustable) return evidence;
      const length = lengths[index]!;
      return evidenceWith(
        length.mode < 128 ? length.mode + 1 : length.mode,
        length.mode < 128 ? length.title : length.title + 1,
      );
    });
    expect(overflow.reduce((total, evidence) => total + branchEvidenceCanonicalBytes(evidence), 0))
      .toBe(MAX_BRANCH_EVIDENCE_BYTES_PER_PATH + 1);
    const overflowResult = allocateAiPathBranchEvidence([
      ...overflow.map((evidence, ordinal) => ({ ordinal, eligible: true, evidence })),
      { ordinal: overflow.length, eligible: true, evidence: base },
    ]);
    expect(overflowResult[overflow.length - 1]?.state).toBe("unavailable");
    expect(overflowResult[overflow.length]?.state).toBe("unavailable");
  });
});

describe("public branch disclosure", () => {
  it("uses one native keyboard button tied to its panel and states exact counts", () => {
    render(<AiPathBranchSubtree evidence={EVIDENCE} stepOrdinal={4} />);
    const toggle = screen.getByRole("button", { name: /AI Path step 5/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const panelId = toggle.getAttribute("aria-controls");
    expect(panelId).toBeTruthy();
    expect(document.getElementById(panelId!)).toHaveAttribute("hidden");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById(panelId!)).toBeTruthy();
    expect(document.getElementById(panelId!)).not.toHaveAttribute("hidden");
    expect(screen.getByText("3 judged · 2 selected · 1 rejected · 2 shown.")).toBeTruthy();
  });

  it("discloses all candidate facts as a table and never claims exhaustive reach", () => {
    render(<AiPathBranchSubtree evidence={EVIDENCE} defaultOpen />);
    const table = screen.getByRole("table", { name: /branch evidence/i });
    const scroller = table.parentElement;
    expect(scroller).toHaveAttribute("role", "region");
    expect(scroller).toHaveAttribute("aria-label", "Branch evidence table");
    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(scroller?.className).toContain("focus-visible:");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("Alpha");
    expect(rows[1]).toHaveTextContent("selected");
    expect(rows[2]).toHaveTextContent("Beta");
    expect(rows[2]).toHaveTextContent("below_cutoff");
    expect(screen.getByText(NOT_REACHED_NOTICE)).toBeTruthy();
    expect(screen.getByText(SELECTED_MEANING)).toBeTruthy();
  });

  it("uses the exact nondisclosing stale copy", () => {
    const evidence = decodeAiPathBranchEvidence({
      schema_version: RETRIEVAL_BRANCH_EVIDENCE_SCHEMA,
      state: "branches_not_recorded",
    });
    render(<AiPathBranchSubtree evidence={evidence} defaultOpen />);
    expect(BRANCHES_NOT_RECORDED).toBe(
      "Branch evidence was not recorded for this step. No candidate or rejection reason is inferred.",
    );
    expect(screen.getByText(BRANCHES_NOT_RECORDED)).toBeTruthy();
  });

  it("keeps inspection identity and failures isolated from disclosure", () => {
    const inspect = vi.fn();
    render(<AiPathBranchSubtree evidence={EVIDENCE} stepOrdinal={7} onInspect={inspect} />);
    const toggle = screen.getByRole("button", { name: /AI Path step 8/i });
    fireEvent.click(toggle);
    expect(inspect).toHaveBeenCalledWith({
      kind: "branch_subtree_expanded",
      stepOrdinal: 7,
      displayedState: "branches_listed",
      branchesListed: 2,
    });
    expect(safeInspect(() => { throw new Error("sink down"); }, inspect.mock.calls[0]![0])).toBe(
      false,
    );
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("renders the same facts in text-only fallback and records that state", () => {
    const inspect = vi.fn();
    render(
      <AiPathBranchSubtree
        evidence={EVIDENCE}
        stepLabel="search_context"
        stepOrdinal={0}
        textOnly
        onInspect={inspect}
      />,
    );
    const fallback = screen.getByTestId("ai-path-branch-fallback");
    expect(fallback).toHaveTextContent("3 judged · 2 selected · 1 rejected · 2 shown.");
    expect(fallback).toHaveTextContent("Alpha");
    expect(aiPathBranchesText({ evidence: EVIDENCE })).toContain(NOT_REACHED_NOTICE);
    expect(inspect).toHaveBeenCalledWith(expect.objectContaining({
      kind: "text_fallback_shown",
      branchesListed: 2,
    }));
  });
});
