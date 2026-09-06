/** 57.12, T1 — the load race: an edit must not cancel a read it does not
 *  invalidate.
 *
 *  `edit()` and the initial restore shared ONE generation counter, so an edit
 *  made while `source-options` was still in flight moved the guard and the
 *  arriving options were dropped with no state set at all: the Connector
 *  question never offered a card, and nothing named what happened. The two
 *  guards are now distinct (`generation` for the load, `editGeneration` for
 *  the edit), and this file holds the two outcomes that remain possible:
 *
 *   * an edit DURING the options load — the options still land, and the
 *     Connector cards render;
 *   * a FAILED options load — a named error state on the Source step, never
 *     a silence that reads as an empty deployment.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamPreconfiguration from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    listDatastreamSetupTemplates: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [
    { object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } },
  ],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    reports: [{
      report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" },
      metrics: ["spend"], dimensions: ["date"],
    }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }],
  }],
  managed_channels: [], external_access: [],
};

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({ templates: [], count: 0, limit: 10 });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => ({
    ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input,
  }));
});

it("an edit while source-options is in flight still lets the options arrive", async () => {
  let resolveOptions!: (value: wizardApi.DatastreamSetupSourceOptions) => void;
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockImplementation(
    () => new Promise((resolve) => { resolveOptions = resolve; }),
  );
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  // The draft is up, the options are NOT: this is the window the race lived
  // in. Choosing the mode is an edit — it used to move the only guard and
  // cancel the load in silence.
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  resolveOptions(options);

  // Les cartes de connecteur VIVENT DANS L'ÉTAPE `Source`, sous la question du
  // mode : elles apparaissent dès qu'un mode est choisi et que les options
  // arrivent, sans navigation. Avant la restructuration, toutes les sections
  // co-rendaient ; maintenant une seule est visible à la fois, mais celle-ci
  // est la première.

  // The cards render: the edit invalidated nothing this read carries.
  expect(await screen.findByRole("radio", { name: "Generic Daily" })).toBeInTheDocument();
  expect(screen.queryByText("Source options could not be read")).not.toBeInTheDocument();
});

it("a failed source-options read is a named error, not a vanishing", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockRejectedValue(new Error("route unavailable"));
  // The failed read is named at the TOP OF `Source`, which is the step it
  // blocks: without options there is no Connector grid and no account list.
  // It used to be a line of the name-and-role block instead — the last thing
  // the step draws, and one the walk cannot reach when the read has failed.
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  // The screen says THAT the read failed — an emptiness it has not earned
  // would be a lie — and names the gesture that gets the options back.
  expect(await screen.findByText("Source options could not be read")).toBeInTheDocument();
  expect(screen.getByText(/reopen this draft to read them again/)).toBeInTheDocument();
});
