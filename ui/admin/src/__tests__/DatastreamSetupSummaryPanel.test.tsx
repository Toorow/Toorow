/** LA TROISIÈME ZONE DE `:31`, PROUVÉE À CHAQUE ARRÊT.
 *
 * `docs/product-architecture/datastream-workbench-and-wizard.md:31` :
 *
 *   > At desktop width, use a persistent left stepper, a central task area,
 *   > and a right `Configuration summary` panel.
 *
 * « Persistent » est le mot que ce fichier tient. L'assistant déclarait deux
 * zones et ne montait le récapitulatif qu'à `Schedule and activate` — le seul
 * arrêt où il ne sert plus à rien, puisque tout ce qu'il rapporte y est déjà
 * décidé. Aucun amendement n'autorisait les deux zones.
 *
 * Trois choses se prouvent ici, et une seule ne suffit pas :
 *   1. le panneau est monté aux CINQ arrêts ;
 *   2. il porte les réponses DÉJÀ données, à l'arrêt où elles sont données ;
 *   3. aux premiers arrêts, où presque rien n'est répondu, il NOMME le geste
 *      qui remplira chaque ligne — au lieu d'aligner des tirets.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    createDatastreamSetupObservation: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    compileDatastreamSetupDraft: vi.fn(),
    createDatastreamSetupPreview: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
  };
});

const draft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft", current_revision_ref: "dsdr_1",
  current_revision: 1, current_proposal_ref: null, first_incomplete_section: "source",
  invalidation_causes: [], resume_href: "/resume", idempotent_replay: false, operator_input: {},
};

const proposal: wizardApi.DatastreamPreconfigurationProposal = {
  schema_version: "1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", proposal_ref: "dspp_1",
  dependency_fingerprint: "a".repeat(64), proposal_token: "b".repeat(64), content_hash: "c".repeat(64),
  is_stale: false, invalidation_causes: [], idempotent_replay: false, resume_href: "/resume",
  confirmed_intent_bundle: {
    datastream_name: "Daily performance", data_role: "Performance",
    joint_grain: ["date", "country"],
    field_mappings: [{ source_identity: "date", field_id: "date", role: "primary_date", semantic_type: "date", canonical_target: "date", aggregation: "none", sensitivity: "public", included: true }],
    business_domain_ids: [], dq_gates: [],
    outputs: [{ kind: "full_grain_output", state: "will_be_created" }],
    exceptions: [], owner_proposals: [],
  },
  configuration_summary: {
    existing: [{ object: "Project configuration", version_id: "pcv_1" }],
    will_be_created: [{ object: "Datastream setup artifacts" }],
    will_remain_a_proposal: [{ object: "Semantic View" }],
    downstream_impact: ["No active pointer changes."],
  },
  sections: [{
    key: "source", status: "complete", dependency_fingerprint: "d".repeat(64),
    items: [{ key: "source.scope", section: "source", requirement: "required", status: "complete", proposed_value: { connector_id: "generic" }, evidence_refs: [], confidence: { level: "high", rationale: "Exact contract" }, coverage: { state: "covered" }, exceptions: [], blockers: [], warnings: [], owner_links: [], downstream_impact: [], dependency_fingerprint: "d".repeat(64) }],
  }],
};

const sourceOptions: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "f".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-07-29T08:00:00Z",
    reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date", "country"], supported_grains: [["date", "country"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "country", kind: "dimension", description: "Country" }, { field_id: "spend", kind: "metric", description: "Spend" }],
  }],
  managed_channels: [], external_access: [],
};

const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_3", draft_revision: 3,
  mode: "connector_pull", discovery_kind: "connector_contract", adapter_ref: "generic.contract.v1",
  connector_contract_version_ref: "ccv_1", request_fingerprint: "1".repeat(64), evidence_fingerprint: "2".repeat(64), schema_hash: null,
  safe_metadata: { history: "90 days", cadence: ["daily"], quota_cost: "1 request" }, coverage: { fields: "available" }, exceptions: [],
  observed_at: "2026-07-29T08:30:00Z", expires_at: null, idempotent_replay: false,
};

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(sourceOptions);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1;
    return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input };
  });
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposal);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.createDatastreamSetupPreview).mockResolvedValue({
    state: "done", job_ref: "job_1",
    preview: { preview_ref: "preview_1", draft_revision_ref: "dsdr_3", proposal_ref: "dspp_1", observation_ref: "dso_1", mode: "connector_pull", dependency_hash: "4".repeat(64), evidence_hash: "5".repeat(64), safe_evidence: { sample: [{ date: "2026-07-28" }], status: "verified" }, status: "ready_for_review", is_stale: false },
  });
});

/** Le panneau, à l'instant où on le lit. */
const panel = async () => within(await screen.findByTestId("config-summary"));

/** La ligne d'un libellé : `<dl> > <div> > <dt> + <dd>`, donc on remonte du
 *  libellé à son `<dt>` puis au bloc qui porte la valeur. */
const line = async (label: string) => {
  const term = (await panel()).getByText(label);
  return (term.closest("dt") as HTMLElement).parentElement as HTMLElement;
};

it("mounts the Configuration summary at all five sections, carrying what is already answered", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  expect(await screen.findByText("Draft saved")).toBeInTheDocument();

  // ---------------------------------------------------------------- 1. Source
  // Le panneau existe AVANT la première réponse. C'est là qu'il était absent.
  expect(await screen.findByLabelText("Source section")).toBeInTheDocument();
  expect(await screen.findByTestId("config-summary")).toBeInTheDocument();
  // Et il n'est pas muet : chaque ligne encore vide nomme le geste qui la
  // remplit, à l'endroit où la valeur se posera.
  expect(within(await line("Mode")).getByText("Choose how this reads its source, at Source"))
    .toBeInTheDocument();
  expect(within(await line("Name")).getByText("Name it at Source")).toBeInTheDocument();
  expect(within(await line("Fields")).getByText("Compile the proposal at Configure"))
    .toBeInTheDocument();
  expect(within(await line("Schedule")).getByText("Choose a cadence at Schedule and activate"))
    .toBeInTheDocument();
  // `:1147` — la ligne `Output` vaut `Not compiled` avant compilation, et le
  // dit avec le mot du document plutôt qu'avec un tiret.
  expect(within(await line("Output")).getByText("Not compiled")).toBeInTheDocument();
  // AUCUN TIRET NULLE PART. Un tiret est une absence dessinée comme une valeur.
  expect((await screen.findByTestId("config-summary")).textContent).not.toContain("—");

  // Chaque réponse de `Source` arrive dans le panneau à l'arrêt où elle est
  // donnée — pas trois arrêts plus loin.
  await user.click(screen.getByRole("radio", { name: "Connector pull" }));
  expect(within(await line("Mode")).getByText("Connector pull")).toBeInTheDocument();
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  await user.type(await screen.findByLabelText(/Datastream name/), "Daily performance");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  expect(within(await line("Name")).getByText("Daily performance")).toBeInTheDocument();
  expect(within(await line("Role")).getByText("Spend")).toBeInTheDocument();

  // ------------------------------------------------------------- 2. Configure
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  expect(await screen.findByLabelText("Configure section")).toBeInTheDocument();
  expect(within(await line("Name")).getByText("Daily performance")).toBeInTheDocument();
  // Rien n'est compilé ici : le panneau le dit, et dit où.
  expect(within(await line("Joint grain")).getByText("Compile the proposal at Configure"))
    .toBeInTheDocument();

  // ------------------------------------------------------ 3. Classify and map
  await user.click(screen.getByRole("button", { name: "Compile proposal" }));
  await user.click(await screen.findByRole("button", { name: "Continue to classify and map" }));
  expect(await screen.findByLabelText("Classify and map section")).toBeInTheDocument();
  // Ce que le compilateur a PROPOSÉ, à côté de ce que la personne a répondu.
  expect(within(await line("Joint grain")).getByText("date / country")).toBeInTheDocument();
  expect(within(await line("Fields")).getByText("1")).toBeInTheDocument();
  expect(within(await line("Output")).getByText("Full grain output — will be created"))
    .toBeInTheDocument();

  // --------------------------------------------------- 4. Preview and validate
  await user.click(await screen.findByRole("button", { name: "Continue to preview" }));
  expect(await screen.findByLabelText("Preview and validate section")).toBeInTheDocument();
  expect(within(await line("Name")).getByText("Daily performance")).toBeInTheDocument();
  expect(within(await line("Joint grain")).getByText("date / country")).toBeInTheDocument();

  // --------------------------------------------------- 5. Schedule and activate
  await user.click(screen.getByRole("button", { name: "Create safe preview" }));
  await user.click(await screen.findByRole("button", { name: "Review schedule" }));
  expect(await screen.findByLabelText("Schedule and activate section")).toBeInTheDocument();
  expect(within(await line("Output")).getByText("Full grain output — will be created"))
    .toBeInTheDocument();
});

/** TROIS ZONES, PAS DEUX. jsdom ne met rien en page, donc la seule preuve
 *  disponible ici est la déclaration elle-même : la grille racine ouvre une
 *  troisième piste de 290 px à la largeur bureau, et le panneau s'y place. Sans
 *  cette assertion, on peut reperdre la colonne en gardant le panneau — qui
 *  retomberait sous la zone de tâche à toutes les largeurs, ce qui est
 *  exactement l'état que cette story répare. */
it("declares the third zone of `:31` at desktop width, and puts the panel in it", async () => {
  render(<DatastreamSetupWizard projectId="proj_1" />);
  const summary = await screen.findByTestId("config-summary");
  const zone = summary.closest("aside") as HTMLElement;
  expect(zone).toHaveAttribute("aria-label", "Configuration summary");
  expect(zone.className).toContain("xl:col-start-3");
  const root = zone.parentElement as HTMLElement;
  expect(root.className).toContain("xl:grid-cols-[210px_minmax(0,1fr)_290px]");
  // Et sous 1280 px la grille n'a que deux pistes : le panneau suit la zone de
  // tâche, il ne tombe pas sous le rail de 210 px.
  expect(root.className).toContain("grid-cols-[210px_minmax(0,1fr)]");
  expect(zone.className).toContain("col-start-2");
});

/** LA LIGNE `Automatic` DE LA LÉGENDE (`:41`), qui est exactement ce que ce
 *  panneau sert : une ligne que personne ne décide reste VISIBLE comme preuve,
 *  au lieu de disparaître parce qu'elle ne demande rien. */
it("shows the compiler-owned lines as Automatic evidence rather than hiding them", async () => {
  render(<DatastreamSetupWizard projectId="proj_1" />);
  const summary = await panel();
  const automatic = summary.getAllByText("Automatic").map((badge) => badge.parentElement?.textContent);
  expect(automatic).toEqual(expect.arrayContaining(["Joint grainAutomatic", "OutputAutomatic"]));
});

/** Une reprise ouvre le panneau DÉJÀ REMPLI, au premier arrêt : c'est la preuve
 *  que le panneau lit le brouillon et non la progression de la session. */
it("reports the answers a resumed draft already carries, from the very first section", async () => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValueOnce({
    ...draft,
    operator_input: {
      mode: "connector_pull",
      name: "Daily performance",
      data_role: "Spend",
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "daily" },
      configure: { date_field: "", metrics: "", dimensions: "", date_window: "", filters: "", history_intent: "", grain: "" },
      wizard_state: { active_section_ref: "source" },
    },
  });
  render(<DatastreamSetupWizard projectId="proj_1" />);

  expect(await screen.findByLabelText("Source section")).toBeInTheDocument();
  expect(within(await line("Name")).getByText("Daily performance")).toBeInTheDocument();
  expect(within(await line("Role")).getByText("Spend")).toBeInTheDocument();
  // Et ce qui manque encore nomme toujours son geste.
  expect(within(await line("Output")).getByText("Not compiled")).toBeInTheDocument();
});
