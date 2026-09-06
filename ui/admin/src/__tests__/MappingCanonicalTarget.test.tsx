/**
 * Mapping tab — a column reaches its concept, and a declaration says what it does.
 *
 * THREE AMENDMENTS OF THE 2026-08-11 REVIEW, pinned where they can rot.
 *
 * 5 — `Canonical / MDM target` was dead text and no control anywhere in the
 * console wrote `mdm_target`. The cell now picks a concept and mints one from
 * the row, through the routes `canonical_field_registry.py` already owned.
 *
 * 10 — a bound field led nowhere: the tab stated `22 / 22` and offered not one
 * address. The concept is a control now — and the address is built by asking
 * `navigation.ts` whether the router admits it, never by composing a path. A
 * control the router refuses is the `Add a check` defect, which did nothing on
 * every Datastream of every Project until somebody measured it.
 *
 * D2 — a join or a split written into a mapping version is executed by NOTHING:
 * `grep -rn "produced_columns|column_treatments" dbt/` returns 0, and
 * `import_landing.py::_apply_governed_mapping` binds one source to one
 * `canonical_target` and never reads `column_treatments`. The screen says so,
 * from ONE sentence, in the composer and on the rows it names.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WorkbenchMappingPage from "../datastreams/workbench/pages/WorkbenchMappingPage";
import { canonicalFieldOwner } from "../datastreams/workbench/mapping/canonicalFields";
import { TREATMENT_NOT_APPLIED } from "../datastreams/workbench/mapping/mappingModel";
import { findObjectContract } from "../shell/navigation";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

/** Registry identities, in the exact shape `ck_mdm_canonical_fields_id` accepts.
 *  A name would not be an address, and the cell must not offer one as if it were. */
const SESSIONS = "mdm_6D13WZ6E18GSZTTEH1JPZBACYW";
const REVENUE = "mdm_7E24X08F29HT0VVFJ2KQ0CBDZX";

/** Semantic Model identities. A `concept_ref` pins a Concept AND its exact
 *  version, so both sides of the pair exist here — a Concept alone would follow
 *  `latest` and is not a reference. */
const COST = "sc_COST0000000000000000000";
const COST_V = "scv_COST000000000000000000";
const CONVERSIONS = "sc_CONV0000000000000000000";
const CONVERSIONS_V = "scv_CONV000000000000000000";

function fields(names: string[] = ["col_0", "col_1", "col_2"]) {
  return [
    {
      field_id: names[0],
      source_identity: names[0],
      physical_type: "string",
      semantic_type: "string",
      aggregation: "none",
      sensitivity: "none",
      binding: { status: "confirmed", confidence: "high", canonical_target: null, mdm_target: SESSIONS },
    },
    {
      field_id: names[1],
      source_identity: names[1],
      physical_type: "string",
      semantic_type: "string",
      aggregation: "none",
      sensitivity: "none",
      binding: { status: "suggested", confidence: "low", canonical_target: null, mdm_target: null },
    },
    {
      field_id: names[2],
      source_identity: names[2],
      physical_type: "string",
      semantic_type: "string",
      aggregation: "none",
      sensitivity: "none",
      binding: { status: "suggested", confidence: "low", canonical_target: null, mdm_target: null },
    },
  ];
}

function columns(names?: string[]) {
  return fields(names).map((field) => ({
    field_id: field.field_id,
    treatment: "Direct",
    canonical_target: null,
    mdm_target: field.binding.mdm_target,
    binding_status: field.binding.status,
    confidence: 0.9,
    sample_value: "a value",
    contributes_to: [],
    joined_with: [],
  }));
}

function payload(
  activeVersion: string | null = "mv_live",
  names?: string[],
): WorkbenchTabPayload {
  const grain = [(names ?? ["col_0"])[0]];
  return {
    evidence: {
      active_version: activeVersion,
      versions: [
        {
          id: "mv_live",
          blocking_count: 0,
          executable: true,
          mapping_payload: { fields: fields(names), joint_grain: grain, grain },
          columns: columns(names),
        },
      ],
      capabilities: [],
    },
  } as unknown as WorkbenchTabPayload;
}

const header = {
  identity: { mode: "connector_pull" },
  versions: {
    active_plan: "pv_1",
    active_mapping: "mv_live",
    proposed_plan: null,
    proposed_mapping: null,
    ready_mapping_proposal: null,
  },
} as unknown as WorkbenchHeader;

/** One Concept as the Semantic Model lens serves it — the only registry
 *  `_op_concept_ref` resolves against, and the only thing a formula may pin. */
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

/** The catalog the project can see, plus whatever a declaration mints. */
function stubApi(options: {
  catalog?: unknown[];
  minted?: unknown;
  /** The Semantic Model concepts a formula may pin. */
  concepts?: unknown[];
  publishable?: boolean;
  refusals?: Array<{ code: string; message: string; path: string }>;
} = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const catalog = options.catalog ?? [
    { id: SESSIONS, canonical_name: "Sessions", concept_kind: "dimension", unit: null, aggregation: null, description: null },
    { id: REVENUE, canonical_name: "Revenue", concept_kind: "metric", unit: null, aggregation: "sum", description: null },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      if (url.includes("canonical-fields")) {
        if ((init.method ?? "GET") === "GET") {
          return { ok: true, status: 200, json: async () => ({ fields: catalog }) };
        }
        return { ok: true, status: 201, json: async () => ({ fields: [options.minted] }) };
      }
      if (url.includes("/governance/semantic-model?")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            schema_version: "governance-collection.v1",
            project_ref: { object_type: "project", id: "proj_EXAMPLE" },
            organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
            section: "semantic-model",
            generated_at: "2026-08-12T00:00:00Z",
            evidence_as_of: null,
            lens: "concepts",
            default_lens: "concepts",
            available_lenses: ["concepts"],
            items: options.concepts ?? [],
            coverage: {},
            unavailable_reasons: [],
          }),
        };
      }
      if (url.endsWith("/change-sets")) {
        return { ok: true, status: 201, json: async () => ({ change_set_id: "scs_EXAMPLE" }) };
      }
      if (url.endsWith("/prepare")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            // ALWAYS minted, refusal or not — the shape a token-only guard reads
            // as "it worked".
            confirmation_token: "token-EXAMPLE",
            refusals: options.refusals ?? [],
            validation: {
              publishable: options.publishable ?? true,
              refusals: options.refusals ?? [],
              test_gate: { state: "pass" },
            },
          }),
        };
      }
      if (url.endsWith("/confirm")) {
        return { ok: true, status: 200, json: async () => ({ result_version_id: "scv_NEW" }) };
      }
      return {
        ok: true,
        status: 201,
        json: async () => ({
          preparation_id: "dscp_1",
          confirmation_secret: "opaque",
          review_hash: "h",
          expires_in_seconds: 900,
          review: {
            diff: [{ path: "$.fields", before_hash: "a", after_hash: "b" }],
            consequence: "Append immutable non-live versions and dispatch one candidate",
            expected_plan_version_id: "pv_1",
            expected_mapping_version_id: "mv_live",
          },
        }),
      };
    }),
  );
  return calls;
}

function mount(
  tab: WorkbenchTabPayload = payload(),
  onOpenOwner?: (owner: unknown) => void,
) {
  return render(
    <WorkbenchMappingPage onRetry={() => {}}
      header={header}
      payload={tab}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      onConfirmed={() => undefined}
      onOpenOwner={onOpenOwner as never}
    />,
  );
}

/** Drive the ratified door and hand back the contract it sent. */
async function sentPayload(calls: Array<{ url: string; init: RequestInit }>) {
  await userEvent.click(screen.getByTestId("prepare-exclusion-change"));
  await userEvent.click(screen.getByTestId("prepare-exact-review"));
  const prepared = calls.find((call) => call.url.endsWith("/mapping/changes"));
  expect(prepared).toBeDefined();
  return (JSON.parse(String(prepared!.init.body)) as {
    proposed_payload: {
      fields: Array<{ field_id: string; binding: Record<string, unknown> }>;
    };
  }).proposed_payload;
}

afterEach(() => vi.unstubAllGlobals());

describe("Mapping tab — binding a column to its concept", () => {
  it("offers the project vocabulary in the row, naming concepts and not identities", async () => {
    stubApi();
    mount();

    const select = await screen.findByTestId("bind-concept-col_1");
    await waitFor(() =>
      expect(within(select).getByRole("option", { name: /Revenue/ })).toBeInTheDocument());
    // The opaque identity is never what a person picks from.
    expect(within(select).queryByRole("option", { name: REVENUE })).not.toBeInTheDocument();
  });

  it("refuses a concept another binding already produces, before the append does", async () => {
    stubApi();
    mount();

    const select = await screen.findByTestId("bind-concept-col_1");
    await waitFor(() =>
      expect(within(select).getByRole("option", { name: /Sessions/ })).toBeInTheDocument());
    // `col_0` already produces Sessions, and two rules claiming one concept
    // cannot both win — `dispatch_mapping_collision` refuses it while rows land.
    expect(within(select).getByRole("option", { name: /Sessions/ })).toBeDisabled();
    // The row that HOLDS it keeps it selectable, or it could never be changed.
    const owner = await screen.findByTestId("bind-concept-col_0");
    expect(within(owner).getByRole("option", { name: /Sessions/ })).toBeEnabled();
  });

  it("writes the pick to binding.mdm_target through the prepared change, never at the click", async () => {
    const calls = stubApi();
    mount();

    const select = await screen.findByTestId("bind-concept-col_1");
    await waitFor(() =>
      expect(within(select).getByRole("option", { name: /Revenue/ })).toBeInTheDocument());
    await userEvent.selectOptions(select, REVENUE);

    // Nothing was written by selecting: the row says so, and the scope names the
    // concept in the words of the person, not the identity of the registry.
    expect(screen.getByTestId("bind-pending-col_1")).toHaveTextContent("Not written yet");
    expect(screen.getByTestId("exclusion-review")).toHaveTextContent("col_1 binds to Revenue.");
    expect(calls.some((call) => call.url.endsWith("/mapping/changes"))).toBe(false);

    const proposed = await sentPayload(calls);
    const bound = proposed.fields.find((field) => field.field_id === "col_1")!;
    expect(bound.binding.mdm_target).toBe(REVENUE);
    // Exactly one binding moved. And the row that was already bound is untouched.
    expect(proposed.fields.find((f) => f.field_id === "col_0")!.binding.mdm_target).toBe(SESSIONS);
    expect(proposed.fields.find((f) => f.field_id === "col_2")!.binding.mdm_target).toBeNull();
  });

  it("clears a concept, and says the column stops naming one", async () => {
    const calls = stubApi();
    mount();

    const select = await screen.findByTestId("bind-concept-col_0");
    await waitFor(() =>
      expect(within(select).getByRole("option", { name: /Sessions/ })).toBeInTheDocument());
    await userEvent.selectOptions(select, "");

    expect(screen.getByTestId("exclusion-review")).toHaveTextContent(
      "col_0 stops naming a canonical field.",
    );
    const proposed = await sentPayload(calls);
    expect(proposed.fields.find((f) => f.field_id === "col_0")!.binding.mdm_target).toBeNull();
  });

  it("declares a project concept from the row and binds the column that made it exist", async () => {
    const minted = {
      id: REVENUE,
      canonical_name: "Revenue",
      concept_kind: "metric",
      unit: null,
      aggregation: "sum",
      description: null,
    };
    const calls = stubApi({ catalog: [], minted });
    mount();

    await userEvent.click(await screen.findByTestId("declare-concept-col_1"));
    await userEvent.clear(screen.getByTestId("declare-concept-name"));
    await userEvent.type(screen.getByTestId("declare-concept-name"), "Revenue");
    await userEvent.selectOptions(screen.getByTestId("declare-concept-kind"), "metric");
    await userEvent.click(screen.getByTestId("declare-concept-submit"));

    // ONE registry, one route: the door `FileSourceSamplePanel` hid behind a
    // file upload, called from here without a second writer being invented.
    const declared = calls.find(
      (call) => call.url.includes("canonical-fields") && call.init.method === "POST",
    );
    expect(declared).toBeDefined();
    const body = JSON.parse(String(declared!.init.body)) as {
      datastream_id: string;
      fields: Array<Record<string, unknown>>;
    };
    expect(body.datastream_id).toBe("ds_EXAMPLE");
    // `object_kind` is DERIVED by the server from the Datastream (story 64.15);
    // sending one would hang this column on another object's definition.
    expect(body.fields[0]).toEqual({
      canonical_name: "Revenue",
      concept_kind: "metric",
      value_type: "string",
      aggregation: "sum",
    });

    // The concept is born AND binds the row — but the BINDING is still prepared.
    await waitFor(() =>
      expect(screen.getByTestId("exclusion-review")).toHaveTextContent("col_1 binds to Revenue."));
    const proposed = await sentPayload(calls);
    expect(proposed.fields.find((f) => f.field_id === "col_1")!.binding.mdm_target).toBe(REVENUE);
  });

  it("offers no selector on a version nothing may edit", async () => {
    stubApi();
    const tab = payload("mv_other");
    mount(tab);

    expect(await screen.findByText("Physical bindings · mv_live")).toBeInTheDocument();
    expect(screen.queryByTestId("bind-concept-col_1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("declare-concept-col_1")).not.toBeInTheDocument();
  });
});

describe("Mapping tab — a field leads to the concept that governs it", () => {
  it("opens the concept through the shell's resolver, at an address the router admits", async () => {
    stubApi();
    const opened: unknown[] = [];
    mount(payload(), (owner) => opened.push(owner));

    await userEvent.click(await screen.findByTestId("open-concept-col_0"));

    expect(opened).toHaveLength(1);
    const owner = opened[0] as Record<string, unknown>;
    expect(owner.object_type).toBe("canonical-field");
    expect(owner.object_id).toBe(SESSIONS);
    // THE THREE THINGS `ContentRouter.openOwner` CHECKS, asserted against the
    // registry itself rather than against a copy of it: the section declares the
    // object type, the id is present (an object type without one is dropped in
    // silence), and no tab is named (the router canonicalizes to the declared
    // default; naming one it does not declare is a silent refusal).
    expect(
      findObjectContract(String(owner.workspace), String(owner.section), "canonical-field"),
    ).not.toBeNull();
    expect(owner.tab).toBeNull();
    expect(owner.action).toBeNull();
    expect(owner.version_id).toBeNull();
  });

  it("names the concept inertly, with its reason, when no resolver is wired", async () => {
    stubApi();
    mount(payload(), undefined);

    // Never a control that lands on the unknown-route screen: the name is shown,
    // and the row says why it is not a link.
    expect(await screen.findByTestId("concept-inert-col_0")).toHaveTextContent(
      /named here rather than linked/i,
    );
    expect(screen.queryByTestId("open-concept-col_0")).not.toBeInTheDocument();
  });

  it("builds no address at all while the router declares no such object", () => {
    // The negative half of the same guard, on the builder rather than the cell:
    // an object type this shell does not declare yields NO reference, so nothing
    // downstream can turn one into a control.
    expect(canonicalFieldOwner("")).toBeNull();
    expect(findObjectContract("governance", "semantic-model", "not-an-object-type")).toBeNull();
  });
});

/**
 * THE THREE ACTS OF A ROW LIVE IN ONE MENU — finding D-2 of the 2026-08-12
 * visual review, where `Split…` was cut off at the right edge of the frame on
 * every row at 1600px. Same gestures, same hooks, one door.
 *
 * `pointerEventsCheck: 0`: a Radix menu opens on POINTERDOWN and puts
 * `pointer-events: none` on the body while it is open.
 */
const user = userEvent.setup({ pointerEventsCheck: 0 });

async function act(id: string, hook: string) {
  await user.click(await screen.findByTestId(`acts-${id}`));
  await user.click(await screen.findByTestId(`${hook}-${id}`));
}

describe("Mapping tab — a composed transformation says what it does", () => {
  it("says a join is recorded and not applied, in the composer", async () => {
    stubApi();
    mount();

    await act("col_1", "join-pick");
    await act("col_2", "join-pick");

    expect(screen.getByTestId("join-not-applied")).toHaveTextContent(TREATMENT_NOT_APPLIED);
    // No date, no plan, no promise about when it will apply.
    expect(screen.getByTestId("join-not-applied").textContent).not.toMatch(/soon|will be|202\d/);
  });

  it("says the same of a split, from the same sentence", async () => {
    stubApi();
    mount();

    await act("col_1", "split");

    expect(screen.getByTestId("split-not-applied")).toHaveTextContent(TREATMENT_NOT_APPLIED);
  });

  it("carries the sentence onto every row the declaration names", async () => {
    stubApi();
    mount();

    await act("col_1", "join-pick");
    await act("col_2", "join-pick");
    await userEvent.type(screen.getByTestId("join-target"), "event_date");
    await userEvent.click(screen.getByTestId("join-add"));

    // The two sources of the join, and only them: a row nothing treats keeps its
    // reading untouched.
    expect(screen.getByTestId("treatment-not-applied-col_1")).toHaveTextContent(
      TREATMENT_NOT_APPLIED,
    );
    expect(screen.getByTestId("treatment-not-applied-col_2")).toBeInTheDocument();
    expect(screen.queryByTestId("treatment-not-applied-col_0")).not.toBeInTheDocument();
  });
});

/**
 * A CONCEPT CAN BE CALCULATED FROM THE ROW, not only named.
 *
 * MEASURED BEFORE THIS BLOCK EXISTED. `DeclareConceptPanel` posted
 * `canonical_name`, `concept_kind`, `value_type` and `aggregation` and nothing
 * else, because `app.mdm_canonical_fields` has no expression column. So `CPA =
 * cost / conversions` — a line of the owner's own journey — meant leaving the
 * Datastream for Governance, creating it there, coming back, and binding.
 *
 * These tests assert the REQUEST BODY. A panel that looks right and posts a
 * shape the server refuses is the exact defect story 60.2 repaired once in
 * Governance, and only the body tells the two apart.
 */
describe("Mapping tab — the concept a column suggests can be calculated", () => {
  const VOCABULARY = [
    { id: SESSIONS, canonical_name: "cost", concept_kind: "metric", unit: null, aggregation: "sum", description: null },
    { id: REVENUE, canonical_name: "conversions", concept_kind: "metric", unit: null, aggregation: "sum", description: null },
  ];
  const PUBLISHED = [
    conceptItem(COST, COST_V, "cost"),
    conceptItem(CONVERSIONS, CONVERSIONS_V, "conversions"),
  ];

  it("posts a semantic-expression tree through the change set, never through the field registry", async () => {
    const calls = stubApi({ catalog: VOCABULARY, concepts: PUBLISHED });
    mount(payload("mv_live", ["cost", "conversions", "col_2"]));

    await userEvent.click(await screen.findByTestId("declare-concept-cost"));
    await userEvent.selectOptions(screen.getByTestId("declare-concept-source"), "calculated");
    await userEvent.clear(screen.getByTestId("declare-concept-name"));
    await userEvent.type(screen.getByTestId("declare-concept-name"), "CPA");
    await userEvent.selectOptions(screen.getByTestId("declare-concept-additivity"), "non_additive");

    // THE COLUMN THEY WERE LOOKING AT is already the numerator: the need appeared
    // on `cost`, and re-picking it would be the second gesture this door removes.
    const numerator = await screen.findByTestId("operand-reference-numerator");
    expect(numerator).toHaveValue(`${COST}|${COST_V}`);

    await userEvent.selectOptions(screen.getByTestId("operand-kind-denominator"), "concept_ref");
    await userEvent.selectOptions(
      await screen.findByTestId("operand-reference-denominator"),
      `${CONVERSIONS}|${CONVERSIONS_V}`,
    );
    await userEvent.click(screen.getByTestId("declare-concept-submit"));

    const opened = calls.find((call) => call.url.endsWith("/change-sets"));
    expect(opened).toBeDefined();
    const intent = (JSON.parse(String(opened!.init.body)) as {
      intent: { action: string; concept: Record<string, unknown> };
    }).intent;
    expect(intent.action).toBe("create_concept");
    // Only a metric carries a formula, and the two keys migration 237 made
    // NOT NULL travel with it.
    expect(intent.concept.kind).toBe("metric");
    expect(intent.concept.name).toBe("cpa");
    expect(intent.concept.additivity_class).toBe("non_additive");
    // BOTH ids on every reference: a Concept without a version follows `latest`
    // and is not a reference (`semantic_expressions._op_concept_ref`).
    expect(intent.concept.expression).toEqual({
      op: "ratio",
      numerator: { op: "concept_ref", concept_id: COST, version_id: COST_V },
      denominator: { op: "concept_ref", concept_id: CONVERSIONS, version_id: CONVERSIONS_V },
      zero_denominator: "null",
      as_percent: false,
    });

    // ONE engine. Nothing was written to the canonical registry, which has no
    // expression column, and the change set was confirmed rather than left open.
    expect(
      calls.filter((call) => call.url.includes("canonical-fields") && call.init.method === "POST"),
    ).toHaveLength(0);
    await waitFor(() => expect(calls.some((call) => call.url.endsWith("/confirm"))).toBe(true));
    expect(await screen.findByTestId("declare-concept-published")).toHaveTextContent("cpa");
  });

  it("refuses before the click when the metric does not say whether it may be summed", async () => {
    const calls = stubApi({ catalog: VOCABULARY, concepts: PUBLISHED });
    mount();

    await userEvent.click(await screen.findByTestId("declare-concept-col_1"));
    await userEvent.selectOptions(screen.getByTestId("declare-concept-source"), "calculated");

    expect(screen.getByTestId("declare-concept-blocked")).toHaveTextContent(
      /Aggregation behaviour is required/i,
    );
    expect(screen.getByTestId("declare-concept-submit")).toBeDisabled();
    // A change set opened and then abandoned is still a row somebody explains.
    expect(calls.some((call) => call.url.endsWith("/change-sets"))).toBe(false);
  });

  it("refuses an aggregation of None on anything but a non-additive metric", async () => {
    const calls = stubApi({ catalog: VOCABULARY, concepts: PUBLISHED });
    mount();

    await userEvent.click(await screen.findByTestId("declare-concept-col_1"));
    await userEvent.selectOptions(screen.getByTestId("declare-concept-source"), "calculated");
    await userEvent.selectOptions(screen.getByTestId("declare-concept-additivity"), "additive");
    await userEvent.selectOptions(screen.getByTestId("declare-concept-aggregation-function"), "");

    // `validate_aggregation`: a null aggregation is legal only when the metric
    // declared itself non-additive. Said here, before the change set exists.
    expect(screen.getByTestId("declare-concept-blocked")).toHaveTextContent(
      /declares itself non-additive/i,
    );
    expect(calls.some((call) => call.url.endsWith("/change-sets"))).toBe(false);
  });

  it("offers only concepts the server can resolve, and names the vocabulary it cannot pin", async () => {
    stubApi({
      catalog: [
        ...VOCABULARY,
        { id: "mdm_8F35Y19G30JU1WWGK3LR1DCEA0", canonical_name: "margin", concept_kind: "metric", unit: null, aggregation: "sum", description: null },
      ],
      concepts: [conceptItem(COST, COST_V, "cost")],
    });
    mount(payload("mv_live", ["cost", "conversions", "col_2"]));

    await userEvent.click(await screen.findByTestId("declare-concept-cost"));
    await userEvent.selectOptions(screen.getByTestId("declare-concept-source"), "calculated");

    const numerator = await screen.findByTestId("operand-reference-numerator");
    // What a person thinks in, first — and the identity is never what they pick.
    expect(within(numerator).getByRole("option", { name: /cost · version 2/ })).toBeInTheDocument();
    expect(within(numerator).queryByRole("option", { name: /margin/ })).not.toBeInTheDocument();
    // …and the reason `margin` is absent is said, rather than left as a hole.
    expect(screen.getByTestId("operand-unpinnable-numerator")).toHaveTextContent("margin");
  });

  it("renders the server's own refusal whole, and confirms nothing", async () => {
    const calls = stubApi({
      catalog: VOCABULARY,
      concepts: PUBLISHED,
      publishable: false,
      refusals: [
        {
          code: "unit_mismatch",
          message: "A ratio of money by a count is not money.",
          path: "$.concept.expression.denominator",
        },
      ],
    });
    mount(payload("mv_live", ["cost", "conversions", "col_2"]));

    await userEvent.click(await screen.findByTestId("declare-concept-cost"));
    await userEvent.selectOptions(screen.getByTestId("declare-concept-source"), "calculated");
    await userEvent.selectOptions(screen.getByTestId("declare-concept-additivity"), "non_additive");
    await screen.findByTestId("operand-reference-numerator");
    await userEvent.selectOptions(screen.getByTestId("operand-kind-denominator"), "concept_ref");
    await userEvent.selectOptions(
      await screen.findByTestId("operand-reference-denominator"),
      `${CONVERSIONS}|${CONVERSIONS_V}`,
    );
    await userEvent.click(screen.getByTestId("declare-concept-submit"));

    // The `code` is what a support conversation quotes and the `path` says WHICH
    // field to change; a paraphrase loses both.
    const refused = await screen.findByTestId("declare-concept-refusals");
    expect(refused).toHaveTextContent("unit_mismatch");
    expect(refused).toHaveTextContent("$.concept.expression.denominator");
    // `prepare` mints a token whether it refused or not. Reading the token alone
    // is what once announced a Concept that was never published.
    expect(calls.some((call) => call.url.endsWith("/confirm"))).toBe(false);
    expect(screen.queryByTestId("declare-concept-published")).not.toBeInTheDocument();
  });
});
