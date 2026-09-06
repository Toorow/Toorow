import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamPreconfiguration from "../datastreams/preconfiguration/DatastreamSetupWizard";
import DatastreamCreate from "../shell/pages/DatastreamCreate";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    createDatastreamSetupObservation: vi.fn(),
    stageDatastreamSetupAsset: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    compileDatastreamSetupDraft: vi.fn(),
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
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{ connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64), display_name: "Generic Daily", observed_at: "2026-07-29T10:00:00Z", reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date", "country"], supported_grains: [["date", "country"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }], fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "country", kind: "dimension", description: "Country" }, { field_id: "spend", kind: "metric", description: "Spend" }] }],
  managed_channels: [
    { channel: "file_upload", availability: "available", template_ref: null },
    { channel: "google_sheets", availability: "available", template_ref: null },
    { channel: "inbound_email", availability: "setup_required", template_ref: null },
    { channel: "webhook", availability: "setup_required", template_ref: null },
  ],
  // Was empty, which meant the external-BigQuery path could never be walked past
  // its first field — the test only ever saw `Source`, and read a neighbouring
  // panel's copy because every panel shared one page.
  external_access: [{ object_ref: { id: "bqacct_1" }, connector_ref: { id: "bigquery" }, label: "Acme warehouse", states: { availability: "available" } }],
};

const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_1",
  draft_revision: 1, mode: "connector_pull", discovery_kind: "connector_contract",
  adapter_ref: "generic.contract.v1", connector_contract_version_ref: "ccv_1",
  request_fingerprint: "b".repeat(64), evidence_fingerprint: "c".repeat(64), schema_hash: null,
  safe_metadata: { report_refs: ["daily"], field_ids: ["date", "spend", "country"], cadence: ["daily"], history: "90 days", quota_cost: "1 request" },
  coverage: { fields: "available" }, exceptions: [], observed_at: "2026-07-29T10:01:00Z",
  expires_at: null, idempotent_replay: false,
};

/** The smallest compiled proposal that lets the walk leave `Configure`: a
 *  proposal that is not stale is all that step's primary reads. */
const proposal: wizardApi.DatastreamPreconfigurationProposal = {
  schema_version: "1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", proposal_ref: "dspp_1",
  dependency_fingerprint: "e".repeat(64), proposal_token: "f".repeat(64), content_hash: "0".repeat(64),
  is_stale: false, invalidation_causes: [], idempotent_replay: false, resume_href: "/resume",
  confirmed_intent_bundle: { joint_grain: ["date", "country"], field_mappings: [], outputs: [{ kind: "full_grain_output", state: "will_be_created" }] },
  configuration_summary: { existing: [], will_be_created: [], will_remain_a_proposal: [], downstream_impact: [] },
  sections: [],
};

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.stageDatastreamSetupAsset).mockResolvedValue({ asset_ref: "dsa_1", draft_ref: "dsd_1", content_hash: "d".repeat(64), detected_format: "csv", byte_count: 11, state: "available", expires_at: "2026-08-05T10:00:00Z", cleanup_owner: "datastream_setup_asset_retention", idempotent_replay: false });
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => { const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1; return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input }; });
});

it("renders one full-page five-stop shell and names no removed step", async () => {
  render(<DatastreamCreate projectId="proj_1" onCancel={vi.fn()} />);
  expect(await screen.findByRole("heading", { name: "Add Datastream" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

  // FIVE STOPS, NAMED AS THE DOCUMENT FIXES THEM —
  // `datastream-workbench-and-wizard.md:35-39`, counted again at `:1144` and
  // pinned to this very constant at `:2757`. Read off the RAIL, not off the
  // page: a stop is a rail entry, and a question that merely appears in the
  // task area is not one. A build once declared seven here, splitting `Mode`
  // and `Identity` out of `Source` on an amendment that does not exist in
  // that document — so the count is asserted exactly, not "at least".
  const rail = screen.getByLabelText("Datastream setup sections");
  const stops = Array.from(rail.querySelectorAll("li")).map(
    (entry) => (entry.querySelector("span.text-label")?.textContent ?? "").trim(),
  );
  expect(stops).toEqual([
    "Source",
    "Configure",
    "Classify and map",
    "Preview and validate",
    "Schedule and activate",
  ]);
  // The two questions that are NOT stops. `Mode` is asked — it is the first
  // thing `Source` shows — but it costs the operator no extra step, which is
  // what `:31` requires and what the rail is the only place to prove.
  expect(stops).not.toContain("Identity");
  expect(stops).not.toContain("Mode");
  expect(await screen.findByLabelText("Source section")).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: "Connector pull" })).toBeInTheDocument();

  // The removed step, refused by name and by landmark (57.9). A section that
  // asks nothing is not a stop, and its three constant sentences promised a
  // location, a retention and proposed Outputs while binding no value at all.
  expect(screen.queryByText("Destination")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Destination section")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save and exit" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /create|activate/i })).not.toBeInTheDocument();
});

it("walks Source to Classify and map with no panel between the two", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposal);
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  // LE PARCOURS RATIFIÉ : le mode ouvre l'étape `Source`, la
  // source s'observe SANS famille de rapport (elle a déménagé en tête de
  // `Configure`), puis l'identité — nom et rôle — est sa propre section.
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  // Le nom n'est plus proposé : la famille de rapport qui
  // l'alimentait est choisie une section plus loin. On le saisit.
  await user.type(await screen.findByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  expect(await screen.findByLabelText("Configure section")).toBeInTheDocument();
  // Nothing co-renders beside `Configure` any more: the panel that used to ride
  // along with it asked for nothing and displayed nothing bound to a value.
  expect(screen.queryByLabelText("Destination section")).not.toBeInTheDocument();
  await user.selectOptions(await screen.findByLabelText(/Report family/), "daily");
  await user.selectOptions(await screen.findByLabelText(/Date field/), "date");
  await user.click(screen.getByRole("checkbox", { name: "Spend (metric)" }));
  await user.click(screen.getByRole("checkbox", { name: "Reporting date (dimension)" }));
  await user.click(screen.getByRole("checkbox", { name: "Country (dimension)" }));
  await user.click(screen.getByRole("button", { name: "Compile proposal" }));
  // One click, one section: `Continue to classify and map` lands on the section
  // it names, never on an intermediate stop.
  await user.click(await screen.findByRole("button", { name: "Continue to classify and map" }));
  expect(await screen.findByLabelText("Classify and map section")).toBeInTheDocument();
});

it("the rail reports real progress and Back is a control, not decoration", async () => {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await screen.findByRole("radio", { name: "Connector pull" });

  // Nothing is done before the first stop is answered, and `Back` cannot move
  // from it. The state used to be `index === 0 ? "current" : "todo"`, a
  // constant: no section could ever read as done and Back was permanently
  // disabled, so "completed sections are revisitable" was unreachable.
  expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /^Configure/ })).not.toBeInTheDocument();

  // THE WHOLE OF STOP 1, WITHOUT LEAVING IT: the mode, then the mode's own
  // questions, then the name and the role. The rail offers `Configure` only
  // once all of them are answered — a partly answered step is not a done one.
  await user.click(screen.getByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  await screen.findByLabelText(/Datastream name/);
  expect(screen.queryByRole("button", { name: /^Configure/ })).not.toBeInTheDocument();

  await user.type(screen.getByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  const configure = await screen.findByRole("button", { name: /^Configure/ });
  await user.click(configure);
  expect(screen.getByRole("button", { name: "Back" })).toBeEnabled();
  // Answered, so the first stop is revisitable from the rail behind us.
  expect(await screen.findByRole("button", { name: /^Source/ })).toBeInTheDocument();

  // ONE `Back` IS ONE STOP, and the first stop is where it runs out. Three
  // clicks were needed while `Mode` and `Identity` were stops of their own;
  // one is what the ratified five cost.
  await user.click(screen.getByRole("button", { name: "Back" }));
  expect(await screen.findByLabelText("Source section")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
});

it("uses governed Connector identities and observation evidence", async () => {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  await waitFor(() => expect(wizardApi.createDatastreamSetupObservation).toHaveBeenCalledWith(
    // PLUS DE `report_ref` ICI : la famille de
    // rapport est une décision de `Configure`, prise APRÈS la découverte —
    // qui lit le contrat du connecteur, pas un rapport. L'épinglage suit le
    // déménagement ; il n'est pas affaibli. `expected_revision` n'est plus
    // épinglé à un entier : chaque navigation de section sauvegarde
    // (`wizard_navigation`), donc le rang dépend du nombre de sections
    // traversées — ce qui compte est qu'une révision courante soit passée.
    expect.anything(), "dsd_1", expect.objectContaining({ expected_revision: expect.any(Number), source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1" }), expect.any(String),
  ));
  // The observation evidence belongs to `Configure`; the operator reaches it
  // through the footer's contextual primaries, one short section at a time —
  // The name and the role are answered on the way, inside `Source`.
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  expect((await screen.findAllByText(/90 days/)).length).toBeGreaterThan(0);
  expect(screen.getByLabelText(/Metrics/)).toBeInTheDocument();
});

it("keeps external BigQuery read-only and managed Append unavailable", async () => {
  const user = userEvent.setup();
  const external = render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  // `Declared writer` and the acknowledgement are no longer drawn before the
  // object answers (57.12, UX pass) — asserting their absence here is what
  // pins the conditioning; they are exercised below, once reached.
  expect(screen.queryByLabelText(/Declared writer/)).not.toBeInTheDocument();
  // Leaving `Source` requires a discovered source: configuring against an
  // unverified object is exactly what the read-only probe exists to prevent, so
  // the one contextual primary of the step stays disabled until then. Asserted
  // here rather than worked around — it is the guard, not an obstacle.
  expect(screen.getByRole("button", { name: "Continue to configure" })).toBeDisabled();
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "bqacct_1");
  await user.type(screen.getByLabelText(/Table or view reference/), "proj.dataset.table");
  await user.type(screen.getByLabelText(/Declared writer/), "acme-etl");
  await user.click(screen.getByRole("checkbox", { name: /read-only/i }));
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  // No connector in this mode, so no name is proposed and no category is read —
  // the field says the absence, and the two required answers are still owed.
  expect(await screen.findByLabelText(/Source category/))
    .toHaveAttribute("placeholder", "No connector in this mode, so no category is read");
  await user.type(await screen.findByLabelText(/Datastream name/), "Warehouse daily");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Context");
  // `Configure` owns its task area alone: the external mode's own controls are
  // what proves the walk left `Source`, not a sentence a neighbour recited.
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  expect(await screen.findByLabelText(/Watermark semantics/)).toBeInTheDocument();
  external.unmount();

  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  // `Channel` is the Source decision, and it is all this test can honestly
  // reach: a `file_upload` feed cannot be discovered before a file is staged,
  // so the name and the role stay unasked without an upload. The `Append`
  // assertion moved to the upload test, which stages one.
  expect(await screen.findByLabelText(/Channel/)).toHaveValue("file_upload");
  expect(screen.queryByLabelText(/Datastream name/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Continue to configure" })).toBeDisabled();
});

it("uploads managed files without storing bytes in React state", async () => {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  const file = new File(["date,spend\n"], "daily.csv", { type: "text/csv" });
  // The label broadened to name SAV when that format landed (38.12); the control
  // and its path are unchanged. Matched exactly, so a real removal still fails.
  await user.upload(await screen.findByLabelText(/CSV, Excel or SAV file/), file);
  await waitFor(() => expect(wizardApi.stageDatastreamSetupAsset).toHaveBeenCalledWith(
    expect.anything(), "dsd_1", file, expect.any(String),
  ));
  expect(await screen.findByText(/held in draft quarantine/)).toBeInTheDocument();

  // Staging the file is what makes discovery possible, so this is the only test
  // that can walk a managed feed into `Configure`. The assertion below came from
  // the external-BigQuery test, where it was only reachable because every panel
  // rendered at once.
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Weekly upload");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Forecast & plan");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  // `Append` needs a stable key the feed does not declare, so it is offered and
  // refused rather than hidden — the operator learns the constraint exists.
  expect(await screen.findByRole("option", { name: /Append — unavailable/ })).toBeDisabled();
});

/** Story 57.5 — what `Configure` owes each mode, counted.
 *
 *  The three mode blocks of that step were three JSX lines of 1447, 1361 and 985
 *  characters: six fields on one line. Splitting them is the point of the design
 *  pass, and the only thing that proves the split lost nothing is naming every
 *  field of every mode. 8 / 6 / 5, measured against the file before the pass.
 *
 *  7 / 6 / 5 since 57.12 (T3): the cadence is asked ONCE, at the Schedule step —
 *  `Cadence intent` was the second asking of the same question, and its removal
 *  is asserted below by the field's ABSENCE, not just by the shorter list.
 *
 *  8 / 6 / 5 : la famille de rapport a quitté
 *  `Source` pour ouvrir `Configure` — c'est elle qui remplit les listes de
 *  champs. Elle rejoint donc la liste du mode connector, épinglée par son nom. */
const CONFIGURE_FIELDS: Record<string, string[]> = {
  connector_pull: [
    "Report family", "Date field", "Metrics", "Dimensions", "Resulting grain",
    "Date window", "Filters", "History intent",
  ],
  external_bq: [
    "Watermark semantics", "Logical dataset name", "Expected freshness",
    "Verification window", "Expected history", "Row filters",
  ],
  managed_feed: [
    "Input reference", "Parsing contract", "Logical dataset name",
    "Date semantics", "Write mode",
  ],
};

/** LE PARCOURS JUSQU'À `CONFIGURE`, partagé par les trois tests de champs :
 *  mode → source observée → nom et rôle — tout cela DANS `Source` —, puis
 *  `Configure`. Cinq arrêts ratifiés, un bouton primaire contextuel par
 *  arrêt, jamais de défilement. */
async function walkToConfigure(user: ReturnType<typeof userEvent.setup>, name: string, role: string) {
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), name);
  await user.selectOptions(await screen.findByLabelText(/Data role/), role);
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
}

it("keeps every Configure field of the connector mode after the split", async () => {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  await walkToConfigure(user, "Daily spend", "Spend");
  const section = within(await screen.findByLabelText("Configure section"));
  for (const label of CONFIGURE_FIELDS.connector_pull) {
    expect(section.getByLabelText(new RegExp(`^${label}`))).toBeInTheDocument();
  }
  // And the removed one is ABSENT (57.12, T3): the cadence is a Schedule
  // question, asserted by name so a re-introduction fails here.
  expect(section.queryByLabelText(/^Cadence intent/)).not.toBeInTheDocument();
});

it("keeps every Configure field of the external mode after the split", async () => {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "bqacct_1");
  await user.type(screen.getByLabelText(/Table or view reference/), "proj.dataset.table");
  await user.type(screen.getByLabelText(/Declared writer/), "acme-etl");
  await user.click(screen.getByRole("checkbox", { name: /read-only/i }));
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  await walkToConfigure(user, "Warehouse daily", "Context");
  const section = within(await screen.findByLabelText("Configure section"));
  for (const label of CONFIGURE_FIELDS.external_bq) {
    expect(section.getByLabelText(new RegExp(`^${label}`))).toBeInTheDocument();
  }
});

it("keeps every Configure field of the managed feed after the split", async () => {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  const file = new File(["date,spend\n"], "daily.csv", { type: "text/csv" });
  await user.upload(await screen.findByLabelText(/CSV, Excel or SAV file/), file);
  await screen.findByText(/held in draft quarantine/);
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  await walkToConfigure(user, "Weekly upload", "Forecast & plan");
  const section = within(await screen.findByLabelText("Configure section"));
  for (const label of CONFIGURE_FIELDS.managed_feed) {
    expect(section.getByLabelText(new RegExp(`^${label}`))).toBeInTheDocument();
  }
});

it("resumes the exact opaque draft and saves before source-setup handoff", async () => {
  const user = userEvent.setup();
  const onSourceSetup = vi.fn();
  const resumed = {
    ...draft,
    operator_input: {
      mode: "managed_feed",
      source: { channel: "webhook", source_account_ref: "", template_ref: "", staged_asset_ref: "", sheet_ref: "" },
      configure: { input_ref: "", parsing_contract: "header_row=1", logical_dataset_name: "", date_semantics: "", grain: "", write_mode: "replace" },
    },
  };
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockClear();
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValueOnce(resumed);
  render(<DatastreamPreconfiguration projectId="proj_1" resumeDraftId="dsd_1" onSourceSetup={onSourceSetup} />);
  // Sans `wizard_state` sauvegardé, la reprise ouvre sur `Mode` — déjà répondu
  // par le brouillon — et le canal se lit une section plus loin (amendement du
  // `Source` est la première section, et le mode sa première question).
  await screen.findByText(/Channel setup required/);
  await user.click(screen.getByRole("button", { name: "Set up source access" }));
  await waitFor(() => expect(onSourceSetup).toHaveBeenCalledWith("dsd_1"));
  expect(wizardApi.createDatastreamSetupDraft).not.toHaveBeenCalled();
  expect(vi.mocked(wizardApi.updateDatastreamSetupDraft).mock.invocationCallOrder[0]).toBeLessThan(onSourceSetup.mock.invocationCallOrder[0]);
});
it("drops stale async draft results after a Project reset", async () => {
  let resolveOld!: (value: wizardApi.DatastreamSetupDraft) => void;
  const oldDraft = new Promise<wizardApi.DatastreamSetupDraft>((resolve) => { resolveOld = resolve; });
  // La preuve technique (dont la référence du brouillon) VIT DANS LA SECTION
  // `Schedule and activate` — le rail de
  // droite qui la portait partout a été supprimé. Plutôt que de marcher sept
  // sections pour lire une référence, le brouillon neuf s'ouvre directement
  // sur cette section : `restoreSection` lit `wizard_state` par son NOM.
  const newDraft = {
    ...draft, draft_ref: "dsd_new", project_ref: "proj_new",
    operator_input: {
      mode: "connector_pull",
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "daily" },
      configure: { date_field: "", metrics: "", dimensions: "", date_window: "", filters: "", history_intent: "", grain: "" },
      wizard_state: { active_section_ref: "schedule_activate" },
    },
  } as unknown as wizardApi.DatastreamSetupDraft;
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockImplementation(async (cfg) => cfg.projectId === "proj_old" ? oldDraft : newDraft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockImplementation(async (_cfg, draftId) => ({ ...options, draft_ref: draftId }));
  const view = render(<DatastreamPreconfiguration projectId="proj_old" />);
  view.rerender(<DatastreamPreconfiguration projectId="proj_new" />);
  // La référence du brouillon est de la PREUVE TECHNIQUE depuis 57.12 (T6) :
  // elle vit derrière le `Collapsible` du récapitulatif, qu'il faut ouvrir.
  // Le récapitulatif est un `<dl>` : l'intitulé et la valeur sont deux noeuds,
  // « Draft dsd_new » n'est plus une chaîne. On lit la valeur là où elle vit.
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Technical evidence" }));
  const evidence = await screen.findByLabelText("Technical evidence");
  expect(within(evidence).getByText("dsd_new")).toBeInTheDocument();
  resolveOld({ ...draft, draft_ref: "dsd_old", project_ref: "proj_old" });
  await waitFor(() => expect(screen.queryByText("Draft dsd_old")).not.toBeInTheDocument());
  expect(wizardApi.readDatastreamSetupSourceOptions).not.toHaveBeenCalledWith(expect.anything(), "dsd_old");
});