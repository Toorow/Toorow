import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewSemanticViewDialog from "../governance/NewSemanticViewDialog";
import { apiGet, apiPost } from "../lib/apiFetch";
import { getGovernanceCollection } from "../governance/governanceSurface";

vi.mock("../lib/apiFetch", () => ({
  ApiError: class ApiError extends Error {},
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}));
vi.mock("../governance/governanceSurface", () => ({
  getGovernanceCollection: vi.fn(),
}));
vi.mock("../governance/BusinessDomainPicker", () => ({
  default: () => <div>Business Domain picker</div>,
}));

const mockedGet = vi.mocked(apiGet);
const mockedPost = vi.mocked(apiPost);
const mockedCollection = vi.mocked(getGovernanceCollection);

beforeEach(() => {
  mockedCollection.mockResolvedValue({
    items: [{
      object_ref: { id: "concept_revenue", label: "Revenue" },
      active_version_ref: { id: "concept_version_revenue", version: 3 },
    }],
  } as never);
  mockedGet.mockResolvedValue({
    matches: [{
      kind: "candidate_key_missing",
      left: { datastream_id: "ds_spend", name: "Campaign spend" },
      right: { datastream_id: "ds_sales", name: "Sales" },
      common_key: {
        version_id: "ckv_day_campaign",
        name: "Day and Campaign",
        components: [
          { canonical_field_id: "day", canonical_name: "Day" },
          { canonical_field_id: "campaign_id", canonical_name: "Campaign" },
        ],
      },
    }],
  });
  mockedPost
    .mockResolvedValueOnce({ change_set_id: "change_1" })
    .mockResolvedValueOnce({
      confirmation_token: "confirm_1",
      validation: { publishable: true },
    })
    .mockResolvedValueOnce({});
});

afterEach(() => vi.clearAllMocks());

test("publishes one exact Datastream pair through its MDM common-key version", async () => {
  const user = userEvent.setup();
  render(
    <NewSemanticViewDialog
      open
      projectId="project_1"
      onClose={vi.fn()}
      onCreated={vi.fn()}
    />,
  );

  await user.type(screen.getByLabelText("View Identifier"), "Cross channel");
  await user.click(await screen.findByText(/Revenue · version 3/));
  await user.selectOptions(
    await screen.findByLabelText("Datastreams this View may cross"),
    "ds_spend|ds_sales|ckv_day_campaign",
  );
  await user.selectOptions(screen.getByLabelText("Relationship cardinality"), "many_to_one");
  await user.click(screen.getByRole("button", { name: "Create Semantic View" }));

  await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(3));
  const command = mockedPost.mock.calls[0]?.[1] as {
    intent: { view: { relationships: Array<Record<string, unknown>> } };
  };
  expect(command.intent.view.relationships).toEqual([{
    name: "cross_ds_spend_ds_sales",
    from_dataset: "fact_daily_kpi",
    to_dataset: "fact_daily_kpi",
    from_columns: ["day", "campaign_id"],
    to_columns: ["day", "campaign_id"],
    cardinality_type: "many_to_one",
    fan_out_policy: "forbid",
    bridge_dataset: null,
    mdm_common_key_version_id: "ckv_day_campaign",
    left_datastream_id: "ds_spend",
    right_datastream_id: "ds_sales",
  }]);
});

// ---------------------------------------------------------------------------
// Editing a published View — 2026-08-18
//
// `edit_view` has been in `_SUPPORTED_INTENTS` since the change set existed and
// no screen ever sent it. An edit carries the EXACT object and the EXACT base
// version, and `_apply_view` appends version N+1 rather than rewriting one.
// ---------------------------------------------------------------------------

/** The View's summary as `_semantic_view` + `_enrich_semantic_object` compose
 *  it. `name` was the key the read model did not carry until 2026-08-18 — it
 *  composed `label` from `label or name` and published no machine name, so no
 *  edit could be composed at all. The owner carries it now; the first test below
 *  keeps the refusal, because an envelope that arrives without it must still be
 *  refused rather than have a name reconstructed from its display title. */
function editTarget(summary: Record<string, unknown>) {
  return {
    objectId: "sv_EXAMPLE",
    baseVersionId: "svv_EXAMPLE",
    label: "Cross channel",
    summary,
  };
}

function renderEdit(summary: Record<string, unknown>) {
  render(
    <NewSemanticViewDialog
      open
      projectId="project_1"
      onClose={vi.fn()}
      onCreated={vi.fn()}
      edit={editTarget(summary)}
    />,
  );
}

test("refuses the edit when the read model carries no canonical name", async () => {
  renderEdit({
    business_scope: "sales",
    description: "Spend against sales.",
    members: [{ concept_id: "concept_revenue", concept_version_id: "concept_version_revenue", role: "metric", name: "revenue", label: "Revenue" }],
  });

  expect(screen.getByTestId("view-edit-block")).toHaveTextContent(/canonical name is not carried/i);
  expect(screen.getByRole("button", { name: "Edit Semantic View" })).toBeDisabled();
  await waitFor(() => expect(mockedPost).not.toHaveBeenCalled());
});

test("sends edit_view from the exact base, pinning the versions the View publishes", async () => {
  const user = userEvent.setup();
  renderEdit({
    // Carried by `_semantic_view` since 2026-08-18, and asserted server-side in
    // `test_a_view_carries_what_an_edit_must_preserve`.
    name: "cross_channel",
    business_scope: "sales",
    description: "Spend against sales.",
    query_policy: { max_rows: 5000 },
    business_domain_refs: ["bd_EXAMPLE"],
    members: [{ concept_id: "concept_revenue", concept_version_id: "concept_version_revenue", role: "metric", name: "revenue", label: "Revenue" }],
    relationships: [],
  });

  expect((screen.getByLabelText("View Identifier") as HTMLInputElement).value).toBe("cross_channel");
  expect((screen.getByLabelText("Business Scope") as HTMLInputElement).value).toBe("sales");
  await user.click(screen.getByRole("button", { name: "Edit Semantic View" }));

  await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(3));
  const [url, body] = mockedPost.mock.calls[0] as [string, Record<string, unknown>];
  expect(url).toContain("/governance/semantic-model/change-sets");
  expect(body.object_id).toBe("sv_EXAMPLE");
  expect(body.base_version_id).toBe("svv_EXAMPLE");
  const intent = body.intent as { action: string; view: Record<string, unknown> };
  expect(intent.action).toBe("edit_view");
  expect(intent.view.name).toBe("cross_channel");
  // The EXACT pinned versions, not the current ones, and the policy the
  // published version was compiled under rather than a silent reset.
  expect(intent.view.concepts).toEqual([
    { concept_id: "concept_revenue", concept_version_id: "concept_version_revenue", dataset: "fact_daily_kpi" },
  ]);
  expect(intent.view.query_policy).toEqual({ max_rows: 5000 });
});

test("refuses rather than publishing a version without the relationship it cannot recompose", async () => {
  renderEdit({
    name: "cross_channel",
    members: [{ concept_id: "concept_revenue", concept_version_id: "concept_version_revenue" }],
    relationships: [{
      name: "cross_ds_gone_ds_other",
      mdm_common_key_version_id: "ckv_retired",
      left_datastream_id: "ds_gone",
      right_datastream_id: "ds_other",
      cardinality_type: "many_to_one",
    }],
  });

  expect(await screen.findByTestId("view-edit-block")).toHaveTextContent(
    /no longer among the approved common-key candidates/i,
  );
  expect(screen.getByRole("button", { name: "Edit Semantic View" })).toBeDisabled();
  expect(mockedPost).not.toHaveBeenCalled();
});

test("names the members pinned to a version that is no longer current", async () => {
  // The version is named by its NUMBER, not by `scv_...`. This test asserted the
  // identifier until 2026-08-21, which is why the line could print one: the pin
  // notice sat one line under `label ?? name ?? concept_id` and printed
  // `concept_version_id` raw -- the twin of the Builder rail defect of f3ca3d50.
  renderEdit({
    name: "cross_channel",
    members: [
      {
        concept_id: "sc_01EXAMPLE0000000000000001",
        concept_version_id: "scv_01EXAMPLE00000000000003",
        version_number: 3,
        label: "Revenue",
      },
    ],
  });

  const notice = await screen.findByTestId("view-edit-stale-pins");
  expect(notice).toHaveTextContent("Revenue");
  expect(notice).toHaveTextContent("version 3");
  expect(notice.textContent ?? "").not.toContain("scv_01EXAMPLE00000000000003");
  expect(notice.textContent ?? "").not.toContain("sc_01EXAMPLE0000000000000001");
  expect(notice).toHaveTextContent(/They stay pinned exactly as published/i);
});

test("says nothing about the version when the envelope carries no number", async () => {
  // An envelope written before the read model served `version_number`. The line
  // drops the version clause rather than falling back to the identifier: an
  // unnamed version is silent, never opaque.
  renderEdit({
    name: "cross_channel",
    members: [
      {
        concept_id: "sc_01EXAMPLE0000000000000001",
        concept_version_id: "scv_01EXAMPLE00000000000003",
        label: "Revenue",
      },
    ],
  });

  const notice = await screen.findByTestId("view-edit-stale-pins");
  expect(notice).toHaveTextContent("Revenue");
  expect(notice.textContent ?? "").not.toContain("scv_01EXAMPLE00000000000003");
  // The Status title legitimately contains the word "version"; what must be
  // absent is the version CLAUSE this line composes.
  expect(notice.textContent ?? "").not.toMatch(/· version/);
});
