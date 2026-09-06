/**
 * Adopting a preconfigured metric — what the dialog SENDS, and what it refuses.
 *
 * WHAT THIS FILE EXISTS TO CATCH, and it is one class of defect with two faces.
 * The server resolves a preset against this Project and hands back a
 * `create_concept` intent whose operands are already pinned to a concept id AND
 * a version id. A browser that RE-COMPOSED that intent — from a name, from a
 * label, from anything but the tree it was given — would be a second answer to
 * "what does roas mean", written on the one side that cannot check it. So these
 * tests assert the REQUEST BODY: the change set the dialog posts after taking a
 * preset must carry exactly the pinned tree the read returned.
 *
 * The other face is the refusal. A preset whose operands this Project cannot
 * read is not offered as a broken option: it is listed with the Concept to
 * declare first, and it can never be selected into the form.
 *
 * Nothing here mocks the dialog's own contract helpers: `buildExpression` and
 * `conceptFormulaSeed` are the two halves that must round-trip, and mocking
 * either would assert that they agree by construction.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewConceptDialog from "../governance/NewConceptDialog";

const PROJECT = "proj_EXAMPLE";
const CHANGE_SET = "scs_01EXAMPLE0000000000000000";

const REVENUE = { id: "sc_REVENUE", version: "scv_REVENUE" };
const COST = { id: "sc_COST", version: "scv_COST" };

/** The offer exactly as `presets_for_project` composes it — adoptable first,
 *  then blocked, then what this Project already reads. */
const CATALOGUE = {
  source: "dbt/seeds/dim_metric.csv",
  counts: { adoptable: 2, blocked: 1, already_declared: 1 },
  not_offered: [],
  presets: [
    {
      name: "roas",
      label: "Roas",
      value_type: "ratio",
      additivity_class: "non_additive",
      aggregation: null,
      calculated: true,
      dependencies: ["revenue", "cost"],
      missing_dependencies: [],
      state: "adoptable",
      gesture: null,
      declared_scope: null,
      source: "dbt/seeds/dim_metric.csv",
      intent: {
        action: "create_concept",
        concept: {
          kind: "metric",
          name: "roas",
          label: "Roas",
          value_type: "ratio",
          definition: "revenue divided by cost, as the delivered metric catalogue declares it.",
          business_domain_refs: [],
          expression: {
            op: "ratio",
            zero_denominator: "null",
            numerator: { op: "concept_ref", concept_id: REVENUE.id, version_id: REVENUE.version },
            denominator: { op: "concept_ref", concept_id: COST.id, version_id: COST.version },
          },
          aggregation: null,
          additivity_class: "non_additive",
          non_additive_dimensions: [],
        },
      },
    },
    {
      name: "conversions_value",
      label: "Conversions value",
      value_type: "money",
      additivity_class: "additive",
      aggregation: { function: "sum" },
      calculated: false,
      dependencies: [],
      missing_dependencies: [],
      state: "adoptable",
      gesture: null,
      declared_scope: null,
      source: "dbt/seeds/dim_metric.csv",
      intent: {
        action: "create_concept",
        concept: {
          kind: "metric",
          name: "conversions_value",
          label: "Conversions value",
          value_type: "money",
          definition: "Preconfigured metric from the delivered catalogue.",
          business_domain_refs: [],
          expression: { op: "source_measure", concept: "conversions_value" },
          aggregation: { function: "sum" },
          additivity_class: "additive",
          non_additive_dimensions: [],
        },
      },
    },
    {
      name: "cpa",
      label: "Cpa",
      value_type: "ratio",
      additivity_class: "non_additive",
      aggregation: null,
      calculated: true,
      dependencies: ["cost", "conversions"],
      missing_dependencies: ["conversions"],
      state: "blocked",
      gesture:
        "Declare conversions first: this metric divides by it, and a formula pins a Concept " +
        "at an exact version rather than a name.",
      declared_scope: null,
      source: "dbt/seeds/dim_metric.csv",
      intent: null,
    },
    {
      name: "cost",
      label: "Cost",
      value_type: "money",
      additivity_class: "additive",
      aggregation: { function: "sum" },
      calculated: false,
      dependencies: [],
      missing_dependencies: [],
      state: "already_declared",
      gesture: null,
      declared_scope: "platform",
      source: "dbt/seeds/dim_metric.csv",
      intent: null,
    },
  ],
};

interface Posted {
  url: string;
  body: Record<string, unknown>;
}

function conceptItem(id: string, versionId: string, label: string) {
  return {
    object_ref: {
      type: "semantic-concept",
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope: "platform",
    owner: {},
    lifecycle_status: "published",
    active_version_ref: { object_type: "semantic-concept-version", id: versionId, version: 2 },
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

function stubFetch(options: { presetsFail?: boolean } = {}) {
  const posted: Posted[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const address = String(url);
    if (init?.method === "POST") {
      posted.push({ url: address, body: JSON.parse(String(init.body ?? "{}")) });
    }
    if (address.includes("/metric-presets")) {
      if (options.presetsFail) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({
            code: "governance_unavailable",
            message: "Governance is unavailable",
          }),
          text: async () => "unavailable",
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => CATALOGUE });
    }
    if (address.includes("/business-domains")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => ({ items: [] }) });
    }
    // PRECISE, and the imprecision is a trap: every change-set address also
    // contains `/governance/semantic-model`, so a substring match here swallows
    // the three POSTs this file exists to inspect.
    if (
      address.includes("/governance/semantic-model?") ||
      address.endsWith("/governance/semantic-model")
    ) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          schema_version: "governance-collection.v1",
          project_ref: { object_type: "project", id: PROJECT },
          organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
          section: "semantic-model",
          generated_at: "2026-08-25T00:00:00Z",
          evidence_as_of: null,
          lens: "concepts",
          default_lens: "concepts",
          available_lenses: ["concepts"],
          items: [
            conceptItem(REVENUE.id, REVENUE.version, "Revenue"),
            conceptItem(COST.id, COST.version, "Cost"),
          ],
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
          confirmation_token: "token-EXAMPLE",
          refusals: [],
          validation: { publishable: true, refusals: [], test_gate: { state: "pass" } },
        }),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ result_version_id: "scv_1" }),
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return posted;
}

afterEach(() => vi.unstubAllGlobals());

function open() {
  render(<NewConceptDialog open projectId={PROJECT} onClose={() => {}} onCreated={() => {}} />);
}

async function chooser(): Promise<HTMLSelectElement> {
  return (await screen.findByLabelText("Start from")) as HTMLSelectElement;
}

function changeSetConcept(posted: Posted[]): Record<string, unknown> {
  const call = posted.find((entry) => entry.url.endsWith("/change-sets"));
  if (!call) throw new Error("the dialog posted no change set");
  return (call.body.intent as Record<string, unknown>).concept as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// The offer
// ---------------------------------------------------------------------------

it("offers the adoptable presets and starts on none of them", async () => {
  stubFetch();
  open();
  const select = await chooser();
  await waitFor(() => expect(select.options.length).toBeGreaterThan(1));

  // The default answers no question: a preselected preset would fill the form
  // before anyone chose anything.
  expect(select.value).toBe("");
  const values = Array.from(select.options).map((option) => option.value);
  expect(values).toEqual(["", "roas", "conversions_value"]);
  // Neither the blocked one nor the one this Project already reads is offerable.
  expect(values).not.toContain("cpa");
  expect(values).not.toContain("cost");
});

it("lists a blocked preset with the Concept to declare, never as an option", async () => {
  stubFetch();
  open();
  const blockedPanel = await screen.findByTestId("metric-preset-blocked");
  expect(blockedPanel.textContent).toContain("Cpa");
  expect(blockedPanel.textContent).toContain("Declare conversions first");
});

it("says the offer could not be read rather than showing an empty chooser", async () => {
  stubFetch({ presetsFail: true });
  open();
  expect(
    await screen.findByText(/The preconfigured metrics could not be read/i),
  ).toBeTruthy();
  // And the person is not stuck: writing a formula by hand never needed this read.
  expect(screen.queryByLabelText("Formula")).toBeTruthy();
});

// ---------------------------------------------------------------------------
// The adoption
// ---------------------------------------------------------------------------

it("posts the EXACT pinned tree the server resolved, never a rebuilt one", async () => {
  const posted = stubFetch();
  open();
  const select = await chooser();
  await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
  await userEvent.selectOptions(select, "roas");

  await userEvent.click(screen.getByRole("button", { name: /create concept/i }));
  await waitFor(() => expect(posted.some((call) => call.url.endsWith("/confirm"))).toBe(true));

  const concept = changeSetConcept(posted);
  expect(concept.name).toBe("roas");
  expect(concept.value_type).toBe("ratio");
  expect(concept.additivity_class).toBe("non_additive");
  // The whole point: both operands carry BOTH ids, and they are the ids the
  // read returned — not a name, and not a version this browser chose.
  expect(concept.expression).toEqual({
    op: "ratio",
    numerator: { op: "concept_ref", concept_id: REVENUE.id, version_id: REVENUE.version },
    denominator: { op: "concept_ref", concept_id: COST.id, version_id: COST.version },
    zero_denominator: "null",
    as_percent: false,
  });
});

it("adopts a source-measure preset without inventing a formula", async () => {
  const posted = stubFetch();
  open();
  const select = await chooser();
  await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
  await userEvent.selectOptions(select, "conversions_value");

  await userEvent.click(screen.getByRole("button", { name: /create concept/i }));
  await waitFor(() => expect(posted.some((call) => call.url.endsWith("/confirm"))).toBe(true));

  const concept = changeSetConcept(posted);
  expect(concept.name).toBe("conversions_value");
  expect(concept.expression).toEqual({ op: "source_measure", concept: "conversions_value" });
  expect(concept.aggregation).toEqual({ function: "sum" });
  expect(concept.additivity_class).toBe("additive");
});

it("fills the fields a preset answers, and leaves them editable", async () => {
  const posted = stubFetch();
  open();
  const select = await chooser();
  await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
  await userEvent.selectOptions(select, "roas");

  // The three questions the preset answered are visibly answered.
  expect((screen.getByLabelText("Canonical Name (ID/Code)") as HTMLInputElement).value).toBe("roas");
  expect((screen.getByLabelText("Aggregation behaviour") as HTMLSelectElement).value).toBe(
    "non_additive",
  );
  expect((screen.getByLabelText("Formula") as HTMLSelectElement).value).toBe("ratio");

  // A starting point, not a contract: changing the label still travels.
  const label = screen.getByLabelText("Display Label") as HTMLInputElement;
  await userEvent.clear(label);
  await userEvent.type(label, "Return on ad spend");
  await userEvent.click(screen.getByRole("button", { name: /create concept/i }));
  await waitFor(() => expect(posted.some((call) => call.url.endsWith("/confirm"))).toBe(true));
  expect(changeSetConcept(posted).label).toBe("Return on ad spend");
});

it("stops claiming a preset was taken once it is unchosen", async () => {
  stubFetch();
  open();
  const select = await chooser();
  await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
  await userEvent.selectOptions(select, "roas");
  expect(screen.getByText(/pinned to revenue and cost at their exact versions/i)).toBeTruthy();

  await userEvent.selectOptions(select, "");
  // Unchoosing leaves the filled fields alone rather than wiping work someone
  // may have started editing — but it must stop saying a preset is in force.
  expect(select.value).toBe("");
  expect(screen.queryByText(/pinned to revenue and cost at their exact versions/i)).toBeNull();
});
