/**
 * The Golden Question client boundary — the document it assembles (Story 51.1).
 *
 * These are the rules a form cannot be trusted to enforce by hand, and each one
 * exists because getting it wrong would look like a working screen:
 *
 *   - an UNDECLARED tolerance leaves the key absent, so the server refuses it by
 *     name. Defaulting to `exact` here would silently decide, for the author,
 *     the one thing `analyze-and-test.md:257` asks them to decide;
 *   - a declared `null` tolerance means exact and must survive the round trip;
 *   - an empty optional node field is OMITTED, never sent as `""`: an empty
 *     string would read as a pin naming nothing;
 *   - `expected_render_ref` is never sent at all — the Render object does not
 *     exist (Stories 50.4/50.5/50.7).
 */
import {
  definitionPayload,
  draftFromVersion,
  emptyDefinitionDraft,
  emptyPathNodeDraft,
  emptyV2AssertionDraft,
  refusalsOf,
  v2AssertionPayload,
  type GoldenQuestionVersion,
} from "../test/goldenQuestionClient";

const LINK_KINDS = ["source", "pull", "mapping", "semantic_view", "citation"];

const VERSION: GoldenQuestionVersion = {
  id: "gqv_EXAMPLE",
  golden_question_id: "gq_EXAMPLE",
  version_number: 1,
  business_domain_id: "dom_EXAMPLE",
  business_domain_version_number: 2,
  business_classification_id: null,
  semantic_view_id: "sv_EXAMPLE",
  semantic_view_version_id: "svv_EXAMPLE",
  semantic_view_version_role: "baseline",
  question: "What did paid media cost by market last complete month?",
  time_boundary: { as_of: "2026-06-30", timezone: "UTC" },
  contract_version: "golden-question.v1",
  expected_result: [
    { assertion_type: "value", tolerance: { kind: "numeric", value: 0.01 } },
    { assertion_type: "cardinality", tolerance: null },
  ],
  required_provenance: [
    { link_kind: "source", required: true },
    { link_kind: "citation", required: false },
  ],
  expected_ai_path: {
    grammar_version: 1,
    required_nodes: [{ key: "analyze/semantic-view/sv_EXAMPLE", step_kind: "semantic_query" }],
    forbidden_nodes: [{ tool_name: "free_text_sql" }],
    alternatives: [
      {
        name: "declared alternative paths",
        branches: [
          {
            name: "via a Skill",
            required_nodes: [{ key: "context-hub/skill/sk_EXAMPLE", step_kind: "skill_step" }],
          },
          {
            name: "via the Semantic View",
            required_nodes: [{ key: "analyze/semantic-view/sv_EXAMPLE" }],
          },
        ],
      },
    ],
  },
  result_type: "breakdown",
  capability_tags: ["spend-breakdown"],
  severity: "critical",
  expected_render_ref: null,
  content_hash: "a".repeat(64),
  predecessor_version_id: null,
  created_by: "owner@example.com",
  created_at: "2026-07-31T09:00:00Z",
  reference_paths: [{ ordinal: 0, query_spec_version_id: "qsv_EXAMPLE", role: "canonical" }],
};

it("omits a tolerance nobody declared, and keeps a declared exact one", () => {
  const draft = emptyDefinitionDraft(LINK_KINDS);
  draft.assertions = [
    { assertion_type: "value", description: "", tolerance_kind: "", tolerance_value: "" },
    { assertion_type: "cardinality", description: "", tolerance_kind: "exact", tolerance_value: "" },
    { assertion_type: "value", description: "", tolerance_kind: "numeric", tolerance_value: "0.05" },
  ];
  const payload = definitionPayload(draft) as { expected_result: Record<string, unknown>[] };
  expect(payload.expected_result[0]).not.toHaveProperty("tolerance");
  expect(payload.expected_result[1].tolerance).toBeNull();
  expect(payload.expected_result[2].tolerance).toEqual({ kind: "numeric", value: 0.05 });
});

it("sends only the provenance links the author declared, with their required flag", () => {
  const draft = emptyDefinitionDraft(LINK_KINDS);
  draft.provenance = [
    { link_kind: "source", state: "required", expected_ref: "" },
    { link_kind: "pull", state: "absent", expected_ref: "" },
    { link_kind: "citation", state: "optional", expected_ref: "" },
  ];
  const payload = definitionPayload(draft) as { required_provenance: unknown[] };
  expect(payload.required_provenance).toEqual([
    { link_kind: "source", required: true },
    { link_kind: "citation", required: false },
  ]);
});

it("omits every empty node field rather than sending a pin that names nothing", () => {
  const draft = emptyDefinitionDraft(LINK_KINDS);
  draft.required_nodes = [
    {
      ...emptyPathNodeDraft(),
      step_kind: "tool_call",
      owner_workspace: "data",
      owner_object_type: "connector",
      owner_object_id: "google_ads",
      tool_name: "get_card",
    },
  ];
  const payload = definitionPayload(draft) as {
    expected_ai_path: { required_nodes: Record<string, unknown>[] };
  };
  expect(payload.expected_ai_path.required_nodes[0]).toEqual({
    key: "data/connector/google_ads",
    step_kind: "tool_call",
    tool: { tool_name: "get_card" },
  });
});

it("sends an incomplete owner triple as the key it is, so the server names it", () => {
  // Dropping the node here would make the screen quietly forget what the author
  // typed; sending `//` gets `node_without_owner` back, which names the gesture.
  const draft = emptyDefinitionDraft(LINK_KINDS);
  draft.required_nodes = [{ ...emptyPathNodeDraft(), step_kind: "tool_call", tool_name: "get_card" }];
  const payload = definitionPayload(draft) as {
    expected_ai_path: { required_nodes: Record<string, unknown>[] };
  };
  expect(payload.expected_ai_path.required_nodes[0].key).toBe("//");
});

it("never sends a Render pin", () => {
  const payload = definitionPayload(emptyDefinitionDraft(LINK_KINDS));
  expect(payload).not.toHaveProperty("expected_render_ref");
});

it("reads a stored version back and rebuilds an equivalent document", () => {
  const draft = draftFromVersion(VERSION, LINK_KINDS);
  expect(draft.time_boundary_kind).toBe("as_of");
  expect(draft.capability_tags).toBe("spend-breakdown");
  expect(draft.provenance.find((entry) => entry.link_kind === "source")?.state).toBe("required");
  expect(draft.provenance.find((entry) => entry.link_kind === "citation")?.state).toBe("optional");
  expect(draft.provenance.find((entry) => entry.link_kind === "mapping")?.state).toBe("absent");

  const payload = definitionPayload(draft) as Record<string, unknown>;
  expect(payload.business_domain_version_number).toBe(2);
  expect(payload.semantic_view_id).toBe("sv_EXAMPLE");
  expect(payload.semantic_view_version_id).toBe("svv_EXAMPLE");
  expect(payload.time_boundary).toEqual({ as_of: "2026-06-30", timezone: "UTC" });
  expect(payload.reference_paths).toEqual([
    { query_spec_version_id: "qsv_EXAMPLE", role: "canonical" },
  ]);
  // A declared alternative is carried through unchanged: this screen does not
  // edit them, so an edit elsewhere must not drop them.
  expect(
    (payload.expected_ai_path as { alternatives: unknown[] }).alternatives,
  ).toEqual(VERSION.expected_ai_path.alternatives);
});

it("reads the server's refusal list without rewriting it", () => {
  expect(
    refusalsOf({
      refusals: [
        { code: "missing_tolerance", message: "declare null for exact", subject: "expected_result[0]" },
      ],
    }),
  ).toEqual([
    { code: "missing_tolerance", message: "declare null for exact", subject: "expected_result[0]" },
  ]);
  expect(refusalsOf({ code: "not_found" })).toEqual([]);
  expect(refusalsOf(null)).toEqual([]);
});

it("serializes one closed v2 value assertion with selectors and numeric tolerance", () => {
  const draft = {
    ...emptyV2AssertionDraft("value"),
    selectors: [{ field: "market", operator: "eq" as const, value_json: '"FR"' }],
    field: "spend",
    expected_json: "12.5",
    tolerance_kind: "numeric" as const,
    tolerance_mode: "relative" as const,
    tolerance_amount: "0.01",
  };

  expect(v2AssertionPayload(draft)).toEqual({
    assertion_type: "value",
    selectors: [{ field: "market", operator: "eq", value: "FR" }],
    field: "spend",
    expected: 12.5,
    operator: "equals",
    tolerance: { kind: "numeric", mode: "relative", amount: 0.01 },
  });
});

it("refuses v2 bounds before a malformed union member reaches the API", () => {
  expect(() => v2AssertionPayload({
    ...emptyV2AssertionDraft("cardinality"),
    expected_json: "-1",
  })).toThrow(/non-negative/);
  expect(() => v2AssertionPayload({
    ...emptyV2AssertionDraft("row_set"),
    fields: Array.from({ length: 17 }, (_, index) => `field_${index}`).join(","),
    expected_rows_json: "[]",
  })).toThrow(/at most 16 fields/);
  expect(() => v2AssertionPayload({
    ...emptyV2AssertionDraft("row_set"),
    fields: "market",
    expected_rows_json: "[1]",
  })).toThrow(/array of at most 100 objects/);
});

it("serializes row-set and state assertions without leaking fields from another union member", () => {
  const rowSet = {
    ...emptyV2AssertionDraft("row_set"),
    fields: "market, spend",
    expected_rows_json: '[{"market":"FR","spend":12.5}]',
    tolerance_kind: "set" as const,
    tolerance_max_missing: "0",
    tolerance_max_extra: "1",
  };
  expect(v2AssertionPayload(rowSet)).toEqual({
    assertion_type: "row_set",
    selectors: [],
    fields: ["market", "spend"],
    expected_rows: [{ market: "FR", spend: 12.5 }],
    operator: "equals",
    tolerance: { kind: "set", max_missing: 0, max_extra: 1 },
  });

  const refused = {
    ...emptyV2AssertionDraft("refused"),
    field: "must-not-travel",
    expected_json: '"must-not-travel"',
    fields: "must-not-travel",
  };
  expect(v2AssertionPayload(refused)).toEqual({
    assertion_type: "refused",
    selectors: [],
    operator: "is",
    tolerance: null,
  });
});
