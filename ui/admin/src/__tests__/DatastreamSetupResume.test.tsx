/** Story 57.9 — a saved draft reopens BY NAME, never by rank.
 *
 *  `wizard_state.active_section` is an integer, and an integer has no name. When
 *  story 57.9 took the wizard from the six sections it had until then down to
 *  five, three things followed from reading that integer back as a
 *  rank, and none of them was visible in a screenshot:
 *
 *  1. rank 5 — `Schedule and activate` — indexed past the end of the section
 *     list, and `steps[activeSection][0]` took the whole screen down with it;
 *  2. ranks 3 and 4 moved one section forward, silently: an operator who had
 *     stopped at `Classify and map` reopened on `Preview and validate`;
 *  3. nothing said a word about either.
 *
 *  Preprod carried three revisions, all at `active_section = 0`, when this was
 *  written. That says the defect had not hurt anyone yet — not that the unbounded
 *  read is correct. These three cases are what tells a renumbering by name from a
 *  renumbering by rank.
 *
 *  Le wizard compte CINQ arrêts (`:35-39`) : `mode` et `identity` sont des
 *  et `identity` ont été extraites de l'ancienne `source`). La table de
 *  correspondance des rangs hérités est INTACTE — un rang sauvegardé par une
 *  ancienne version retombe toujours sur la section qui porte ses questions —
 *  donc les tests de rangs ci-dessous gardent leurs cibles. Ce qui change, c'est
 *  le repli « index 0 » : il désigne désormais `Mode`, plus `Source`.
 */
import { render, screen } from "@testing-library/react";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    readDatastreamSetupObservation: vi.fn(),
    readDatastreamPreconfigurationProposal: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_4", current_revision: 4, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"], supported_grains: [["date"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "spend", kind: "metric", description: "Spend" }],
  }],
  managed_channels: [], external_access: [],
};

/** A complete operator input, so that nothing but the saved position decides
 *  where the wizard reopens. */
function resumedDraft(wizardState: Record<string, unknown>): wizardApi.DatastreamSetupDraft {
  return {
    ...draft,
    operator_input: {
      mode: "connector_pull",
      name: "Daily performance",
      data_role: "Spend",
      domain_ids: [],
      schedule: { mode: "daily" },
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "daily" },
      configure: { date_field: "date", metrics: "spend", dimensions: "date", date_window: "", filters: "", history_intent: "", cadence_intent: "daily", grain: "date" },
      wizard_state: wizardState,
    },
  };
}

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => ({ ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input }));
});

it("reopens a rank saved on the last section, and does not throw", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(resumedDraft({ active_section: 5, first_incomplete: "schedule_activate" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);
  // Rank 5 named `Schedule and activate` and still does. Read as a rank against
  // a seven-entry list it is `undefined`, and the header dereferences it.
  expect(await screen.findByLabelText("Schedule and activate section")).toBeInTheDocument();
  expect(screen.queryByText(/older version of this wizard/i)).not.toBeInTheDocument();
});

it("reopens a rank saved on Classify and map, not on the section after it", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(resumedDraft({ active_section: 3, first_incomplete: "classify_and_map" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);
  expect(await screen.findByLabelText("Classify and map section")).toBeInTheDocument();
  expect(screen.queryByLabelText("Preview and validate section")).not.toBeInTheDocument();
});

it("says so when a saved position names no section this wizard knows", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(resumedDraft({ active_section: 99, first_incomplete: "source" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);
  expect(await screen.findByText(/This draft was saved on an older version of this wizard/i)).toBeInTheDocument();
  expect(screen.getByText(/nothing you entered was lost/i)).toBeInTheDocument();
  // Le repli d'une position inconnue est « index 0 » : depuis l'amendement du
  // ratifié, l'index 0 est la section `Source`, qui ouvre le wizard,
  // plus `Source`. L'intention est inchangée — l'opérateur atterrit au début du
  // parcours, averti. À noter : la copie d'implémentation dit encore « You are
  // on Source » alors que l'écran affiche `Mode` (copie périmée signalée, non
  // corrigée ici) ; l'assertion ci-dessus vise la seconde moitié de phrase,
  // toujours exacte.
  expect(screen.getByLabelText("Source section")).toBeInTheDocument();
});

it("reopens a draft with no saved position on the first section, without a word", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue({
    ...resumedDraft({}),
    operator_input: { ...resumedDraft({}).operator_input, wizard_state: undefined },
  });
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);
  // Starting is not an anomaly. The two states have two sentences, and this one
  // is the absence of a sentence.
  // Sans position sauvegardée, le wizard rouvre à « index 0 » en silence :
  // c'était `Source` quand `Source` ouvrait le parcours ; depuis l'amendement
  // ratifié, la première section est `Source`. L'intention — rouvrir au
  // début, sans un mot — est préservée, seule la cible du repli bouge.
  expect(await screen.findByLabelText("Source section")).toBeInTheDocument();
  expect(screen.queryByText(/older version of this wizard/i)).not.toBeInTheDocument();
});

it("prefers the saved section name over the rank when both are present", async () => {
  // A draft written by this build carries both keys. The name is authoritative:
  // the rank is kept only so an older build reading the same row lands somewhere
  // real, and a rank that disagreed would otherwise win by accident.
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(resumedDraft({ active_section: 0, active_section_ref: "preview_validate", first_incomplete: "preview_validate" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);
  expect(await screen.findByLabelText("Preview and validate section")).toBeInTheDocument();
});
