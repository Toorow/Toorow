/**
 * Story 60.2 — the only surface that writes a formula, and what it now sends.
 *
 * WHAT WAS MEASURED BEFORE THIS FILE EXISTED. `NewConceptDialog` posted
 * `expression: {op: "formula", text}`; `"formula"` is not an operation of
 * `semantic-expression.v1`, so every metric it submitted came back
 * `unknown_operation`. With the field left empty it posted `expression: null`,
 * which is `malformed_node`. Its aggregations were `SUM/AVG/...` where the
 * server matches lowercase names and has `average`, not `avg`. And it sent
 * neither `additivity_class` nor `non_additive_dimensions`, the two keys
 * `semantic_model.py:961-962` reads. A Concept of kind `metric` could not be
 * created from this screen, with a formula or without one.
 *
 * These tests assert the REQUEST BODY, not a rendering. A dialog that looks
 * right and posts an operation the server refuses is the exact defect being
 * repaired, and only the body distinguishes the two.
 *
 * `ALLOWED_OPERATIONS` is not importable from TypeScript, so the palette lives
 * in `formulaContract.ts` and `server/tests/core/test_semantic_dialog_paths.py`
 * compares that file to the Python module. Here it is imported rather than
 * copied, so a value asserted below is the value the dialog actually offers.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewConceptDialog from "../governance/NewConceptDialog";
import {
  ADDITIVITY_CLASSES,
  AGGREGATION_FUNCTIONS,
  FORMULA_OPERATIONS,
  OPERAND_OPERATIONS,
} from "../governance/formulaContract";

const PROJECT = "proj_EXAMPLE";
const CHANGE_SET = "scs_01EXAMPLE0000000000000000";

interface Posted {
  url: string;
  body: Record<string, unknown>;
}

/** The three calls the dialog makes, in the order it makes them. `prepare` is
 *  configurable because "refused" and "published" differ ONLY in its answer —
 *  and because `prepare` always mints a token whether it refused or not, which
 *  is what the old guard read as success. */
function stubFetch(options: {
  publishable?: boolean;
  refusals?: Array<{ code: string; message: string; path: string }>;
  conceptsFail?: boolean;
  concepts?: unknown[];
}) {
  const posted: Posted[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const address = String(url);
    if (init?.method === "POST") {
      posted.push({ url: address, body: JSON.parse(String(init.body ?? "{}")) });
    }
    if (address.includes("/business-domains")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ items: [] }) });
    }
    if (address.includes("/governance/semantic-model?") || address.endsWith("/governance/semantic-model")) {
      if (options.conceptsFail) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({ code: "unavailable", message: "The Semantic Model could not be read." }),
          text: async () => "unavailable",
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          schema_version: "governance-collection.v1",
          project_ref: { object_type: "project", id: PROJECT },
          organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
          section: "semantic-model",
          generated_at: "2026-08-08T00:00:00Z",
          evidence_as_of: null,
          lens: "concepts",
          default_lens: "concepts",
          available_lenses: ["concepts"],
          items: options.concepts ?? [],
          coverage: {},
          unavailable_reasons: [],
        }),
      });
    }
    if (address.endsWith("/change-sets")) {
      return Promise.resolve({
        ok: true,
        status: 201,
        json: async () => ({ change_set_id: CHANGE_SET }),
      });
    }
    if (address.endsWith("/prepare")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          // ALWAYS minted, refusal or not. This is the shape the previous guard
          // misread as "it worked".
          confirmation_token: "token-EXAMPLE",
          refusals: options.refusals ?? [],
          validation: {
            publishable: options.publishable ?? true,
            refusals: options.refusals ?? [],
            test_gate: { state: "pass" },
          },
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ result_version_id: "scv_1" }) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return posted;
}

afterEach(() => vi.unstubAllGlobals());

function conceptItem(id: string, versionId: string, label: string) {
  return {
    object_ref: {
      type: "semantic-concept",
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope: "project",
    owner: {},
    lifecycle_status: "published",
    active_version_ref: { object_type: "semantic-concept-version", id: versionId, version: 3 },
    selected_version_ref: null,
    available_tabs: [],
    default_tab: "definition",
    allowed_actions: [],
    used_by: { state: "empty", count: 0, refs: [], truncated: false },
    versions: { state: "empty", count: 0, refs: [], truncated: false },
    evidence: { state: "empty", count: 0, refs: [], truncated: false },
    summary: {},
    evidence_as_of: null,
  };
}

function open() {
  render(
    <NewConceptDialog open projectId={PROJECT} onClose={() => {}} onCreated={() => {}} />,
  );
}

/** The same dialog, seeded from an existing object. `summary` is the object's
 *  summary exactly as `_semantic_concept` composes it — including the fact that
 *  it carries NO `name` key, which is why the canonical name below is recovered
 *  from the published expression. */
function openEdit(summary: Record<string, unknown>) {
  render(
    <NewConceptDialog
      open
      projectId={PROJECT}
      onClose={() => {}}
      onCreated={() => {}}
      edit={{
        objectId: "sc_EXAMPLE",
        baseVersionId: "scv_EXAMPLE",
        label: "Gross revenue",
        summary,
      }}
    />,
  );
}

function changeSetBody(posted: Posted[]): Record<string, unknown> {
  const call = posted.find((entry) => entry.url.endsWith("/change-sets"));
  if (!call) throw new Error("the dialog posted no change set");
  const intent = call.body.intent as Record<string, unknown>;
  return intent.concept as Record<string, unknown>;
}

async function fillIdentity(name = "efficiency_index") {
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), name);
}

// ---------------------------------------------------------------------------
// The behaviour is required, and it is never defaulted
// ---------------------------------------------------------------------------

it("refuses to submit a metric whose aggregation behaviour was not declared", async () => {
  const posted = stubFetch({});
  open();
  await fillIdentity();
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  expect(await screen.findByText(/Aggregation behaviour is required/i)).toBeInTheDocument();
  // Nothing was opened server-side: a change set created and then abandoned is
  // still a row somebody has to explain.
  expect(posted.filter((entry) => entry.url.endsWith("/change-sets"))).toHaveLength(0);
});

it("offers the three stored classes under their three labels and no fourth", async () => {
  stubFetch({});
  open();
  const select = screen.getByLabelText("Aggregation behaviour") as HTMLSelectElement;
  const offered = Array.from(select.options).map((option) => option.value).filter(Boolean);
  expect(offered).toEqual([...ADDITIVITY_CLASSES]);
  expect(screen.getByRole("option", { name: "Semi-additive" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Non-Additive Ratio/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /Snapshot/i })).not.toBeInTheDocument();
});

it("asks a semi-additive metric which dimensions it may not cross, and blocks without them", async () => {
  const posted = stubFetch({});
  open();
  await fillIdentity("stored_balance");
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "semi_additive");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  expect(
    await screen.findByText(/names the dimensions it may NOT be summed across/i),
  ).toBeInTheDocument();
  expect(posted.filter((entry) => entry.url.endsWith("/change-sets"))).toHaveLength(0);
});

// ---------------------------------------------------------------------------
// What travels on the wire
// ---------------------------------------------------------------------------

it("posts an allowlisted operation, never the invented one", async () => {
  const posted = stubFetch({});
  open();
  await fillIdentity();
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "non_additive");
  await userEvent.selectOptions(screen.getByLabelText("Formula"), "ratio");
  await userEvent.selectOptions(screen.getByLabelText("Numerator kind"), "literal");
  await userEvent.type(screen.getByLabelText("Numerator value"), "119");
  await userEvent.selectOptions(screen.getByLabelText("Denominator kind"), "literal");
  await userEvent.type(screen.getByLabelText("Denominator value"), "10");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  await waitFor(() => expect(changeSetBody(posted)).toBeTruthy());
  const concept = changeSetBody(posted);
  const expression = concept.expression as Record<string, unknown>;
  expect(expression.op).toBe("ratio");
  expect(FORMULA_OPERATIONS).toContain(expression.op as never);
  expect(expression.op).not.toBe("formula");
  // A ratio declares what a zero denominator means; no default is chosen for it.
  expect(expression.zero_denominator).toBe("null");
  const numerator = expression.numerator as Record<string, unknown>;
  expect(OPERAND_OPERATIONS).toContain(numerator.op as never);
  expect(numerator.value).toBe(119);
});

it("carries the two keys the server reads, and the aggregation in the case it matches", async () => {
  const posted = stubFetch({});
  open();
  await fillIdentity("stored_balance");
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "semi_additive");
  await userEvent.type(
    screen.getByLabelText("Dimensions it must not be summed across"),
    "date, account",
  );
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  await waitFor(() => expect(changeSetBody(posted)).toBeTruthy());
  const concept = changeSetBody(posted);
  expect(concept.additivity_class).toBe("semi_additive");
  expect(concept.non_additive_dimensions).toEqual(["date", "account"]);
  // Lowercase, and one of the server's own names.
  expect(AGGREGATION_FUNCTIONS).toContain((concept.aggregation as Record<string, string>).function as never);
  expect((concept.aggregation as Record<string, string>).function).toBe("sum");
});

it("offers only lowercase aggregations, and average rather than avg", async () => {
  stubFetch({});
  open();
  const select = screen.getByLabelText("Aggregation function") as HTMLSelectElement;
  const offered = Array.from(select.options).map((option) => option.value).filter(Boolean);
  expect(new Set(offered)).toEqual(new Set(AGGREGATION_FUNCTIONS));
  expect(offered).not.toContain("AVG");
  expect(offered).not.toContain("SUM");
});

it("pins BOTH ids when an operand references another Concept", async () => {
  const posted = stubFetch({ concepts: [conceptItem("sc_COST", "scv_COST", "Cost")] });
  open();
  await fillIdentity("cost_share");
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "additive");
  await userEvent.selectOptions(screen.getByLabelText("Formula"), "add");
  await userEvent.selectOptions(await screen.findByLabelText("Operand 1 kind"), "concept_ref");
  await userEvent.selectOptions(
    await screen.findByLabelText("Operand 1 concept"),
    "sc_COST|scv_COST",
  );
  await userEvent.selectOptions(screen.getByLabelText("Operand 2 kind"), "literal");
  await userEvent.type(screen.getByLabelText("Operand 2 value"), "1");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  await waitFor(() => expect(changeSetBody(posted)).toBeTruthy());
  const expression = changeSetBody(posted).expression as Record<string, unknown>;
  const operands = expression.operands as Array<Record<string, unknown>>;
  expect(operands[0]).toEqual({ op: "concept_ref", concept_id: "sc_COST", version_id: "scv_COST" });
});

// ---------------------------------------------------------------------------
// Empty is not broken, and refused is not unreachable
// ---------------------------------------------------------------------------

it("says a Concept without a formula reads its mapped source measure", async () => {
  stubFetch({});
  open();
  expect(
    screen.getByText(/This Concept has no formula: its value comes from its mapped source measure\./i),
  ).toBeInTheDocument();
});

it("distinguishes an unreadable Concept list from an empty one", async () => {
  stubFetch({ conceptsFail: true });
  open();
  await userEvent.selectOptions(screen.getByLabelText("Formula"), "ratio");
  await userEvent.selectOptions(await screen.findByLabelText("Numerator kind"), "concept_ref");

  expect(await screen.findByText(/The Concept list could not be read/i)).toBeInTheDocument();
  expect(screen.queryByText(/No published Concept to reference yet/i)).not.toBeInTheDocument();
});

it("says there is nothing to reference when the Project truly has no Concept", async () => {
  stubFetch({ concepts: [] });
  open();
  await userEvent.selectOptions(screen.getByLabelText("Formula"), "ratio");
  await userEvent.selectOptions(await screen.findByLabelText("Numerator kind"), "concept_ref");

  expect(await screen.findByText(/No published Concept to reference yet/i)).toBeInTheDocument();
  expect(screen.queryByText(/could not be read/i)).not.toBeInTheDocument();
});

it("renders a server refusal with its code and its JSON path, and claims nothing", async () => {
  const posted = stubFetch({
    publishable: false,
    refusals: [
      {
        code: "additivity_contradiction",
        message: "average is declared additive, but rolling up an average of averages does not reproduce it.",
        path: "$.additivity_class",
      },
    ],
  });
  open();
  await fillIdentity();
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "additive");
  await userEvent.selectOptions(screen.getByLabelText("Aggregation function"), "average");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  expect(await screen.findByText("additivity_contradiction", { exact: false })).toBeInTheDocument();
  expect(screen.getByText(/\$\.additivity_class/)).toBeInTheDocument();
  expect(screen.getByText(/The formula could not be validated/i)).toBeInTheDocument();
  // The token was minted, and the dialog did NOT take it as consent to publish.
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);
});

it("does not confirm when preparation returned no refusal but did not clear it either", async () => {
  const posted = stubFetch({ publishable: false, refusals: [] });
  open();
  await fillIdentity();
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "non_additive");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  expect(await screen.findByText(/was not published/i)).toBeInTheDocument();
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);
});

it("confirms only when preparation says it is publishable", async () => {
  const posted = stubFetch({ publishable: true });
  open();
  await fillIdentity();
  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "non_additive");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  await waitFor(() =>
    expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(1),
  );
  const confirm = posted.find((entry) => entry.url.endsWith("/confirm"))!;
  expect(confirm.body.confirmation_token).toBe("token-EXAMPLE");
});

// ---------------------------------------------------------------------------
// The dimension half of the same dialog
// ---------------------------------------------------------------------------

it("sends a semantic type for a dimension, which it never did", async () => {
  const posted = stubFetch({});
  open();
  await userEvent.selectOptions(screen.getByLabelText("Concept Kind"), "dimension");
  await fillIdentity("reporting_week");
  await userEvent.selectOptions(screen.getByLabelText("Semantic type"), "time");
  await userEvent.click(screen.getByRole("button", { name: "Create Concept" }));

  await waitFor(() => expect(changeSetBody(posted)).toBeTruthy());
  const concept = changeSetBody(posted);
  expect(concept.semantic_type).toBe("time");
  // A dimension needs no formula and declares no additivity.
  expect(concept.expression).toBeUndefined();
  expect(concept.additivity_class).toBeUndefined();
});

// ---------------------------------------------------------------------------
// Editing what was published — 2026-08-18
//
// `edit_concept` has been in `_SUPPORTED_INTENTS` since the change set existed
// and no screen ever sent it, so a Concept published with the wrong additivity
// class was permanent. An edit is not a rewrite: it carries the EXACT object and
// the EXACT base version, and `_apply_concept` appends version N+1.
// ---------------------------------------------------------------------------

/** A published metric, as the Governance read model composes it: `label` and no
 *  `name`, the additivity class, the aggregation function alone, and the
 *  expression tree whose `source_measure` leaf is the one place the canonical
 *  name survives. */
const PUBLISHED_METRIC = {
  concept_kind: "metric",
  value_type: "money",
  additivity_class: "semi_additive",
  non_additive_dimensions: ["date"],
  aggregation: "sum",
  description: "Revenue before deductions.",
  business_domain_refs: [],
  expression: { op: "source_measure", concept: "gross_revenue" },
};

it("seeds the edit from what the published version carries", async () => {
  stubFetch({});
  openEdit(PUBLISHED_METRIC);

  expect(screen.getByText("Edit Semantic Concept")).toBeInTheDocument();
  expect(screen.getByText(/Editing from version scv_EXAMPLE/)).toBeInTheDocument();
  expect((screen.getByLabelText("Canonical Name (ID/Code)") as HTMLInputElement).value).toBe("gross_revenue");
  expect((screen.getByLabelText("Aggregation behaviour") as HTMLSelectElement).value).toBe("semi_additive");
  expect((screen.getByLabelText("Dimensions it must not be summed across") as HTMLInputElement).value).toBe("date");
  expect((screen.getByLabelText("Value Type") as HTMLSelectElement).value).toBe("money");
  expect((screen.getByLabelText("Aggregation function") as HTMLSelectElement).value).toBe("sum");
  // The identity is shown as published and cannot be retyped: the object row
  // keeps its name when a version is appended.
  expect(screen.getByLabelText("Canonical Name (ID/Code)")).toHaveAttribute("readonly");
  expect(screen.getByLabelText("Concept Kind")).toBeDisabled();
});

it("sends edit_concept from the exact base, with the class the person corrected", async () => {
  const posted = stubFetch({});
  openEdit(PUBLISHED_METRIC);

  await userEvent.selectOptions(screen.getByLabelText("Aggregation behaviour"), "additive");
  await userEvent.click(screen.getByRole("button", { name: "Edit Concept" }));

  await waitFor(() => expect(changeSetBody(posted)).toBeTruthy());
  const call = posted.find((entry) => entry.url.endsWith("/change-sets"))!;
  expect(call.body.object_id).toBe("sc_EXAMPLE");
  expect(call.body.base_version_id).toBe("scv_EXAMPLE");
  expect((call.body.intent as Record<string, unknown>).action).toBe("edit_concept");
  const concept = changeSetBody(posted);
  expect(concept.name).toBe("gross_revenue");
  expect(concept.additivity_class).toBe("additive");
  expect(concept.expression).toEqual({ op: "source_measure", concept: "gross_revenue" });
  // Prepared and confirmed, exactly like a creation.
  await waitFor(() => expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(1));
});

it("never defaults the additivity class of an object that has none", () => {
  stubFetch({});
  openEdit({ concept_kind: "metric", expression: { op: "source_measure", concept: "gross_revenue" } });
  expect((screen.getByLabelText("Aggregation behaviour") as HTMLSelectElement).value).toBe("");
});

it("refuses the edit when the canonical name is carried nowhere", async () => {
  const posted = stubFetch({});
  // A dimension: no expression, and no binding row to read a name from. The
  // read model composes `label` from `label or name`, so the machine name is
  // simply not on the wire.
  openEdit({ concept_kind: "dimension", semantic_type: "time" });

  expect(screen.getByTestId("concept-edit-block")).toHaveTextContent(/canonical name is not carried/i);
  expect(screen.getByRole("button", { name: "Edit Concept" })).toBeDisabled();
  await waitFor(() => expect(posted.filter((entry) => entry.url.endsWith("/change-sets"))).toHaveLength(0));
});

it("refuses the edit rather than replacing a formula it cannot compose", () => {
  stubFetch({});
  openEdit({
    ...PUBLISHED_METRIC,
    // `conditional` is in the server allowlist and NOT in this editor's palette.
    // Seeding it to the `source_measure` default would publish a formula nobody
    // wrote, one click later.
    expression: { op: "conditional", branches: [] },
    source_bindings: { rows: [{ concept_name: "gross_revenue" }] },
  });

  expect(screen.getByTestId("concept-edit-block")).toHaveTextContent(/the operation conditional/i);
  expect(screen.getByRole("button", { name: "Edit Concept" })).toBeDisabled();
});

it("recovers the name from the binding rows when the formula carries none", async () => {
  const posted = stubFetch({});
  openEdit({
    concept_kind: "metric",
    additivity_class: "non_additive",
    aggregation: "",
    expression: {
      op: "ratio",
      numerator: { op: "concept_ref", concept_id: "sc_COST", version_id: "scv_COST" },
      denominator: { op: "concept_ref", concept_id: "sc_CLICKS", version_id: "scv_CLICKS" },
      zero_denominator: "null",
      as_percent: false,
    },
    source_bindings: { rows: [{ concept_name: "cost_per_click" }, { concept_name: null }] },
  });

  expect((screen.getByLabelText("Canonical Name (ID/Code)") as HTMLInputElement).value).toBe("cost_per_click");
  expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio");
  expect((screen.getByLabelText("Numerator kind") as HTMLSelectElement).value).toBe("concept_ref");

  await userEvent.click(screen.getByRole("button", { name: "Edit Concept" }));
  await waitFor(() => expect(changeSetBody(posted)).toBeTruthy());
  const expression = changeSetBody(posted).expression as Record<string, unknown>;
  expect(expression.op).toBe("ratio");
  expect(expression.numerator).toEqual({ op: "concept_ref", concept_id: "sc_COST", version_id: "scv_COST" });
});

it("renders the refusals of an edit one per line, and confirms nothing", async () => {
  const posted = stubFetch({
    publishable: false,
    refusals: [
      {
        code: "additivity_contradiction",
        message: "average is declared additive, but rolling up an average of averages does not reproduce it.",
        path: "$.additivity_class",
      },
    ],
  });
  openEdit(PUBLISHED_METRIC);

  await userEvent.click(screen.getByRole("button", { name: "Edit Concept" }));

  expect(await screen.findByText("additivity_contradiction", { exact: false })).toBeInTheDocument();
  expect(screen.getByText(/\$\.additivity_class/)).toBeInTheDocument();
  expect(posted.filter((entry) => entry.url.endsWith("/confirm"))).toHaveLength(0);
});
