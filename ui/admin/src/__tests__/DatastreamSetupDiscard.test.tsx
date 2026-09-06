/** `Discard this draft` — how a wizard draft ends (AI-336, ratified by Jean 2026-08-31).
 *
 *  Until this gesture a setup draft had NO END. There was no delete, no expiry
 *  and no abandon marker: POST, GET and PATCH, and a Project bounded only by the
 *  rule that it carries one resumable draft at a time. The database has declared
 *  the terminal state `archived` since migration 134 and migration 284 kept it
 *  deliberately unwritten, outside the resumable predicate, so that whichever
 *  answer Jean chose would have somewhere to land.
 *
 *  This suite covers the half a screen owns: the gesture is offered, the
 *  confirmation names what the person LOSES rather than a state word, cancelling
 *  changes nothing, confirming calls the one route and hands the route that owns
 *  the resume key its cue to forget it, and a refusal is shown rather than
 *  swallowed.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>(
    "../datastreams/wizard/wizardApi",
  );
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    readDatastreamSetupObservation: vi.fn(),
    readDatastreamPreconfigurationProposal: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
    discardDatastreamSetupDraft: vi.fn(),
    listDatastreamSetupTemplates: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_4", current_revision: 4, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "",
  idempotent_replay: false,
  operator_input: {
    mode: "connector_pull",
    name: "Daily performance",
    data_role: "Spend",
    domain_ids: [],
    schedule: { mode: "daily" },
    source: {
      source_account_ref: "sacct_1", connector_ref: "generic",
      connector_contract_version_ref: "ccv_1", report_ref: "daily",
    },
    configure: {
      date_field: "date", metrics: "spend", dimensions: "date", date_window: "",
      filters: "", history_intent: "", cadence_intent: "daily", grain: "date",
    },
  },
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{
    object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" },
    label: "Acme account", states: { availability: "available" },
  }],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    reports: [{
      report_ref: "daily", display_name: "Daily report",
      availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"],
      supported_grains: [["date"]], history: "90 days",
      cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 },
    }],
    fields: [
      { field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" },
      { field_id: "spend", kind: "metric", description: "Spend" },
    ],
  }],
  managed_channels: [], external_access: [],
};

beforeEach(() => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({ templates: [], count: 0, limit: 20 });
  vi.mocked(wizardApi.discardDatastreamSetupDraft).mockResolvedValue({ ...draft, state: "archived" });
});

async function openTheDiscardConfirmation() {
  const user = userEvent.setup();
  const onDiscarded = vi.fn();
  const onCancel = vi.fn();
  render(
    <DatastreamSetupWizard
      projectId="proj_1"
      resumeDraftId="dsd_1"
      onDiscarded={onDiscarded}
      onCancel={onCancel}
    />,
  );
  await user.click(await screen.findByRole("button", { name: "Discard this draft" }));
  return { user, onDiscarded, onCancel, dialog: await screen.findByTestId("datastream-setup-discard") };
}

it("names what the person loses, and never the state the database writes", async () => {
  const { dialog } = await openTheDiscardConfirmation();

  // The three facts a person recognises their own draft by. The wizard has them
  // because it holds the answers; the fleet screen does not, and says less.
  expect(dialog).toHaveTextContent("Daily performance");
  expect(dialog).toHaveTextContent("Connector pull");
  expect(dialog).toHaveTextContent("Source");

  // The sentence that makes the choice safe: this draft never created anything
  // that collects.
  expect(dialog).toHaveTextContent(/No Datastream was created from this draft/);
  expect(dialog).toHaveTextContent(/starts from the first question/);

  // NOT the database's word. `archived` is what the server writes; a person
  // reading it here would learn nothing they can act on.
  expect(dialog).not.toHaveTextContent(/archiv/i);

  // Next to a destructive verb, the way out names what SURVIVES.
  expect(screen.getByTestId("datastream-setup-discard-cancel")).toHaveTextContent("Keep the draft");
});

it("discards nothing when the person keeps the draft", async () => {
  const { user, onDiscarded } = await openTheDiscardConfirmation();

  await user.click(screen.getByTestId("datastream-setup-discard-cancel"));

  expect(wizardApi.discardDatastreamSetupDraft).not.toHaveBeenCalled();
  expect(onDiscarded).not.toHaveBeenCalled();
  expect(screen.queryByTestId("datastream-setup-discard")).not.toBeInTheDocument();
  // The draft is still there, and so is its gesture.
  expect(screen.getByRole("button", { name: "Discard this draft" })).toBeInTheDocument();
});

it("calls the one route, then hands the route that owns the resume key its cue", async () => {
  const { user, onDiscarded, onCancel } = await openTheDiscardConfirmation();

  await user.click(screen.getByTestId("datastream-setup-discard-confirm"));

  expect(wizardApi.discardDatastreamSetupDraft).toHaveBeenCalledOnce();
  expect(vi.mocked(wizardApi.discardDatastreamSetupDraft).mock.calls[0][1]).toBe("dsd_1");
  // `onDiscarded`, NOT `onCancel`. Cancelling leaves a draft that stays
  // resumable; this one ended it, and the route that owns
  // `datastream-setup-return` has to forget the key — otherwise the next entry
  // resumes a draft the server refuses every write on.
  expect(onDiscarded).toHaveBeenCalledOnce();
  expect(onCancel).not.toHaveBeenCalled();
});

it("shows a refusal instead of pretending the draft is gone", async () => {
  vi.mocked(wizardApi.discardDatastreamSetupDraft).mockRejectedValue(
    new Error(
      "This setup already created a Datastream. Archive the Datastream from its own screen; "
      + "the draft that created it is kept as its record.",
    ),
  );
  const { user, onDiscarded } = await openTheDiscardConfirmation();

  await user.click(screen.getByTestId("datastream-setup-discard-confirm"));

  expect(await screen.findByText(/Archive the Datastream from its own screen/)).toBeInTheDocument();
  expect(onDiscarded).not.toHaveBeenCalled();
});
