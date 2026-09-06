/**
 * Story 75-2 — what "Propose as governed field" actually sends, and what it says
 * back.
 *
 * THESE ASSERT THE REQUEST BODY, not a rendering. The whole point of the rail is
 * that the console composes the SAME typed tree `core.semantic_expressions`
 * validates: a dialog that looks right and posts a shape the server refuses is
 * exactly the defect story 60.2 already repaired once, and only the body
 * distinguishes the two. The pins asserted below are the pins the Result's
 * Definitions lens carried — a reference without its version follows `latest`
 * and is not a reference.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ProposeCalculatedFieldDialog from "../governance/ProposeCalculatedFieldDialog";

const PROJECT = "proj_EXAMPLE";
const RESULT = "qr_01EXAMPLE0000000000000000";
const SPEC_VERSION = "qsv_01EXAMPLE0000000000000000";
const SPEND = { concept: "sc_spend_EXAMPLE", version: "scv_spend_EXAMPLE" };
const CLICKS = { concept: "sc_clicks_EXAMPLE", version: "scv_clicks_EXAMPLE" };

interface Posted {
  url: string;
  body: Record<string, unknown>;
}

function member(pin: { concept: string; version: string }, label: string) {
  return {
    kind: "measure",
    concept_id: pin.concept,
    version_id: pin.version,
    label,
    definition: null,
    expression: null,
    aggregation: "sum",
    additivity_class: "additive",
    unit: null,
    value_type: "integer",
    allowed_grains: [],
    resolved: true,
    unresolved_reason: null,
    owner_ref: {
      workspace: "governance",
      section: "semantic-model",
      object_type: "semantic-concept",
      object_id: pin.concept,
      tab: null,
      version_id: pin.version,
    },
  };
}

function stubFetch(options: {
  members?: unknown[];
  lensFails?: boolean;
  refusal?: { status: number; code: string; message: string; refusals?: unknown[] };
}) {
  const posted: Posted[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const address = String(url);
    if (address.includes("/lens/definitions")) {
      if (options.lensFails) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({ code: "unavailable", message: "The lens did not answer." }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          schema_version: "analyze-result-lens.v1",
          result_id: RESULT,
          project_id: PROJECT,
          lens: "definitions",
          outcome: "success",
          content_hash: "a".repeat(64),
          query_spec_id: "qs_EXAMPLE",
          query_spec_version_id: SPEC_VERSION,
          query_spec_version_number: 1,
          semantic_view_id: "sv_EXAMPLE",
          semantic_view_version_id: "svv_EXAMPLE",
          semantic_view_label: "Fixture view",
          semantic_view_version_number: 1,
          started_at: null,
          ended_at: null,
          lenses: ["definitions"],
          definitions: {
            semantic_view: {
              id: "sv_EXAMPLE",
              version_id: "svv_EXAMPLE",
              label: "Fixture view",
              version_number: 1,
              status: "published",
              resolved: true,
              owner_ref: {},
            },
            members: options.members ?? [member(SPEND, "Spend"), member(CLICKS, "Clicks")],
            classification_pins: [],
            source_comparison: { requested: null, executed: false, meaning: null },
          },
        }),
      });
    }
    if (address.includes("/governance/semantic-model")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          schema_version: "governance-collection.v1",
          project_ref: { object_type: "project", id: PROJECT },
          organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
          section: "semantic-model",
          generated_at: "2026-09-05T00:00:00Z",
          evidence_as_of: null,
          lens: "concepts",
          default_lens: "concepts",
          available_lenses: ["concepts"],
          items: [],
          coverage: {},
          unavailable_reasons: [],
        }),
      });
    }
    if (address.includes("/calculated-field-proposals")) {
      posted.push({ url: address, body: JSON.parse(String(init?.body ?? "{}")) });
      if (options.refusal) {
        return Promise.resolve({
          ok: false,
          status: options.refusal.status,
          json: async () => ({
            code: options.refusal!.code,
            message: options.refusal!.message,
            ...(options.refusal!.refusals ? { refusals: options.refusal!.refusals } : {}),
          }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 201,
        json: async () => ({
          proposal: {
            id: "cfp_01EXAMPLE0000000000000000",
            org_id: "org_EXAMPLE",
            project_id: PROJECT,
            status: "open",
            origin: "human",
            name: "cost_per_click",
            description: null,
            expression: {},
            value_type: "ratio",
            unit: null,
            currency: null,
            dependencies: [],
            provenance: { origin: "exploration", result_id: RESULT },
            requested_by: "owner@example.com",
            created_at: "2026-09-05T10:00:00Z",
            resolved_by: null,
            resolved_at: null,
            applied_ref: null,
          },
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return posted;
}

afterEach(() => vi.unstubAllGlobals());

function mount(queueHref: string | null = "/org/org_EXAMPLE/project/proj_EXAMPLE/governance/semantic-model/lens/concepts") {
  return render(
    <ProposeCalculatedFieldDialog
      open
      onClose={() => {}}
      projectId={PROJECT}
      resultId={RESULT}
      querySpecVersionId={SPEC_VERSION}
      queueHref={queueHref}
    />,
  );
}

test("posts the exact tree the server validates, with both ids on every reference", async () => {
  const posted = stubFetch({});
  mount();

  // The pre-fill is what the Result PINNED: the ratio opens on its first two
  // measures, at the versions this execution used.
  await waitFor(() =>
    expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio"),
  );
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), "cost_per_click");
  await userEvent.click(screen.getByRole("button", { name: "Propose" }));

  await waitFor(() => expect(posted).toHaveLength(1));
  expect(posted[0].url).toBe(`/api/projects/${PROJECT}/calculated-field-proposals`);
  expect(posted[0].body).toEqual({
    name: "cost_per_click",
    description: null,
    expression: {
      op: "ratio",
      numerator: { op: "concept_ref", concept_id: SPEND.concept, version_id: SPEND.version },
      denominator: { op: "concept_ref", concept_id: CLICKS.concept, version_id: CLICKS.version },
      zero_denominator: "null",
      as_percent: false,
    },
    provenance: { result_id: RESULT, query_spec_version_id: SPEC_VERSION },
  });
  // NEVER `origin`: the door stamps `human` on anything the console files.
  expect(Object.keys(posted[0].body)).not.toContain("origin");
});

test("an exploration with no pinned Concept says so instead of proposing an unpinned reference", async () => {
  stubFetch({ members: [] });
  mount();

  await waitFor(() => expect(screen.getByTestId("propose-no-pins")).toBeInTheDocument());
  expect(screen.getByTestId("propose-no-pins").textContent).toContain(
    "a reference without a version is not a reference",
  );
});

test("a named refusal is rendered as the gesture that repairs it", async () => {
  stubFetch({
    refusal: {
      status: 422,
      code: "unresolved_reference",
      message: "This formula still refers to spend by name.",
    },
  });
  mount();

  await waitFor(() =>
    expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio"),
  );
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), "cost_per_click");
  await userEvent.click(screen.getByRole("button", { name: "Propose" }));

  await waitFor(() => expect(screen.getByTestId("propose-refusal")).toBeInTheDocument());
  expect(screen.getByTestId("propose-refusal-gesture").textContent).toContain(
    "Pin each measure to a version",
  );
});

test("each named refusal of the expression contract gets its own gesture", async () => {
  stubFetch({
    refusal: {
      status: 422,
      code: "invalid_expression",
      message: "This formula is not expressible in the governed contract.",
      refusals: [
        { code: "unknown_operation", message: "raw_sql is not an operation.", path: "$.op" },
        { code: "incompatible_types", message: "money and integer.", path: "$.operands[1]" },
      ],
    },
  });
  mount();

  await waitFor(() =>
    expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio"),
  );
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), "cost_per_click");
  await userEvent.click(screen.getByRole("button", { name: "Propose" }));

  await waitFor(() =>
    expect(screen.getByTestId("propose-refusal-unknown_operation")).toBeInTheDocument(),
  );
  expect(screen.getByTestId("propose-refusal-incompatible_types").textContent).toContain(
    "Make the two sides comparable",
  );
});

test("a filed proposal names where it now waits, and offers the queue", async () => {
  stubFetch({});
  mount();

  await waitFor(() =>
    expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio"),
  );
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), "cost_per_click");
  await userEvent.click(screen.getByRole("button", { name: "Propose" }));

  await waitFor(() => expect(screen.getByTestId("propose-filed")).toBeInTheDocument());
  const where = screen.getByTestId("propose-filed-where").textContent ?? "";
  expect(where).toContain("Governance");
  expect(where).toContain("Proposed calculated fields");
  expect(screen.getByTestId("propose-queue-link")).toHaveAttribute(
    "href",
    "/org/org_EXAMPLE/project/proj_EXAMPLE/governance/semantic-model/lens/concepts",
  );
});

test("without an address for the queue it names the queue in words and offers no link", async () => {
  stubFetch({});
  mount(null);

  await waitFor(() =>
    expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio"),
  );
  await userEvent.type(screen.getByLabelText("Canonical Name (ID/Code)"), "cost_per_click");
  await userEvent.click(screen.getByRole("button", { name: "Propose" }));

  await waitFor(() => expect(screen.getByTestId("propose-filed")).toBeInTheDocument());
  expect(screen.queryByTestId("propose-queue-link")).toBeNull();
});
