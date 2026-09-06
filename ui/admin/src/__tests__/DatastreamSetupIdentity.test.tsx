/** Story 57.10 — the name, the data role and the source category, at the end
 *  of the `Source` step.
 *
 *  Three things this file exists to keep true, each of which was false before:
 *
 *  1. the role select offered `Finance`, `Reference` and `Operations` — three
 *     tokens no CHECK constraint and no server module knows. Choosing one was
 *     accepted for six sections — the flow had six then, five since 57.9 —
 *     and refused at materialization
 *     with `ActivationValidationError("Confirmed Datastream data role is
 *     invalid")`;
 *  2. the field opened on `Performance`, an answer nobody gave;
 *  3. both fields lived at `Classify and map`, three sections after the source.
 *     Ils ferment désormais l'étape `Source` (`:183`),
 *     et le nom n'est plus PROPOSÉ dans la marche avant,
 *     parce que la famille de rapport qui alimentait la proposition se choisit
 *     une section plus loin, en tête de `Configure`. La proposition elle-même
 *     reste épinglée ci-dessous, via une reprise ouverte directement à
 *     `Configure`.
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
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    createDatastreamSetupObservation: vi.fn(),
    readDatastreamSetupObservation: vi.fn(),
    stageDatastreamSetupAsset: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    compileDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
  };
});

/** The seven values of `app.datastreams.data_role` (migration 093), mirrored by
 *  `server/core/datastreams.py:37-45`. Written out rather than imported so that
 *  a silent edit of the screen's own constant fails here. */
const DATA_ROLES = [
  "Spend",
  "Performance",
  "Revenue & conversions",
  "Forecast & plan",
  "Context",
  "Reference & targets",
  "Operational",
];

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    // Read server-side from the connector manifest's `public_catalog.category`.
    source_category: "paid_media", source_category_origin: "connector_manifest",
    reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"], supported_grains: [["date"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "spend", kind: "metric", description: "Spend" }],
  }],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [{ object_ref: { id: "bqacct_1" }, connector_ref: { id: "bigquery" }, label: "Acme warehouse", states: { availability: "available" } }],
};

const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_1", draft_revision: 1,
  mode: "connector_pull", discovery_kind: "connector_contract", adapter_ref: "generic.contract.v1",
  connector_contract_version_ref: "ccv_1", request_fingerprint: "b".repeat(64),
  evidence_fingerprint: "c".repeat(64), schema_hash: null,
  safe_metadata: { history: "90 days", cadence: ["daily"], quota_cost: "1 request" },
  coverage: { fields: "available" }, exceptions: [], observed_at: "2026-08-05T10:01:00Z",
  expires_at: null, idempotent_replay: false,
};

const proposal: wizardApi.DatastreamPreconfigurationProposal = {
  schema_version: "1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", proposal_ref: "dspp_1",
  dependency_fingerprint: "d".repeat(64), proposal_token: "e".repeat(64), content_hash: "f".repeat(64),
  is_stale: false, invalidation_causes: [], idempotent_replay: false, resume_href: "/resume",
  confirmed_intent_bundle: { joint_grain: ["date"], field_mappings: [{ source_identity: "date", role: "primary_date", semantic_type: "date", canonical_target: "date", aggregation: "none", sensitivity: "public", included: true }] },
  configuration_summary: { existing: [], will_be_created: [], will_remain_a_proposal: [], downstream_impact: [] },
  sections: [],
};

beforeEach(() => {
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposal);
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.readDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.stageDatastreamSetupAsset).mockResolvedValue({ asset_ref: "dsa_1", draft_ref: "dsd_1", content_hash: "d".repeat(64), detected_format: "csv", byte_count: 11, state: "available", expires_at: "2026-08-05T10:00:00Z", cleanup_owner: "datastream_setup_asset_retention", idempotent_replay: false });
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1;
    return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input };
  });
});

/** LA MARCHE JUSQU'AU NOM ET AU RÔLE : le mode se choisit
 *  en tête de `Source`, la source s'observe SANS famille de rapport (elle vit
 *  en tête de `Configure`, un arrêt plus loin), puis les deux dernières
 *  questions de l'étape apparaissent d'elles-mêmes au retour de
 *  l'observation — aucun clic, aucun arrêt entre elles et les questions
 *  au-dessus. */
async function walkToNameAndRole(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await screen.findByLabelText("Source section");
}

it("asks the name, the role and the category at the end of the Source step", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);

  // LE NOM, LE RÔLE ET LA CATÉGORIE FERMENT `Source` (`:183`) : avant, les trois
  // champs vivaient à `Classify and map`, trois sections après le choix du
  // compte. Une seule section est visible à la fois — on les lit là où ils
  // vivent, pas ailleurs.
  const identity = screen.getByLabelText("Source section");
  expect(within(identity).getByLabelText(/Datastream name/)).toBeInTheDocument();
  expect(within(identity).getByLabelText(/Data role/)).toBeInTheDocument();
  expect(within(identity).getByLabelText(/Source category/)).toBeInTheDocument();

  // Le nom s'ouvre VIDE : la proposition du contrat naît du choix de la famille
  // de rapport, qui a déménagé en tête de `Configure` — une section APRÈS
  // `Configure`, dont le guichet exige déjà un nom. La proposition (et sa
  // mention « From connector contract ») est épinglée par le test suivant,
  // via une reprise à `Configure` où ce choix existe.
  expect(screen.getByLabelText(/Datastream name/)).toHaveValue("");

  // The category is preselected from the manifest and READ-ONLY: it describes
  // the product, not the Datastream, so this screen is not its authority.
  // La valeur affichée est la clé HUMANISÉE (`humanKey`) depuis l'amendement du
  // — « paid_media » se lit « Paid media » ; l'épinglage suit le
  // rendu, il n'est pas affaibli (la clé brute reste celle du manifeste).
  const category = screen.getByLabelText(/Source category/);
  expect(category).toHaveValue("Paid media");
  expect(category).toHaveAttribute("readonly");
  expect(screen.getByText(/corrected by an explicit source-type declaration/)).toBeInTheDocument();
});

it("still proposes the name from the connector contract once a report family is chosen", async () => {
  // L'INTENTION SURVIT AU DÉMÉNAGEMENT : le nom
  // proposé vient du contrat du connecteur, jamais d'une constante d'écran, et
  // le champ le dit. Mais la proposition naît de `chooseReport`, et la famille
  // de rapport se choisit à `Configure` — injoignable dans la marche avant sans
  // un nom déjà saisi. Le raccourci de reprise ouvre le brouillon directement à
  // `Configure` avec une observation restaurée et un nom encore vide :
  // `operator_input` porte le mode, son objet `source` (lu inconditionnellement
  // à la restauration) et son `configure`, plus `wizard_state` lu par son NOM ;
  // `source.observation_ref` impose de mocker `readDatastreamSetupObservation`.
  const resumed = {
    ...draft,
    operator_input: {
      mode: "connector_pull",
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "", observation_ref: "dso_1" },
      configure: { date_field: "", metrics: "", dimensions: "", date_window: "", filters: "", history_intent: "", grain: "" },
      wizard_state: { active_section_ref: "configure" },
    },
  } as unknown as wizardApi.DatastreamSetupDraft;
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValueOnce(resumed);
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);

  // Un PREMIER choix de famille n'invalide rien (la découverte ne nommait pas
  // de rapport) — pas de ConfirmDialog ici ; l'édition pose le nom proposé.
  await user.selectOptions(await screen.findByLabelText(/Report family/), "daily");

  // Back recule d'UN arrêt : `Source`, où le nom proposé se lit.
  await user.click(screen.getByRole("button", { name: "Back" }));
  expect(await screen.findByLabelText(/Datastream name/)).toHaveValue("Generic Daily — Daily report");
  expect(screen.getByText(/From connector contract/)).toBeInTheDocument();
});

it("offers exactly the seven values of DATA_ROLES and none of the three retired tokens", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);

  const role = screen.getByLabelText(/Data role/);
  // The empty option is the eighth: the field opens on NO answer. Les VALEURS
  // sont inchangées par le déménagement — seuls les libellés ont
  // gagné une phrase (« <valeur> — <phrase> »), ce qui laisse `selectOptions`
  // par valeur intact.
  const values = within(role).getAllByRole("option").map((option) => (option as HTMLOptionElement).value);
  expect(values).toEqual(["", ...DATA_ROLES]);
  expect(role).toHaveValue("");

  // The three that produced a last-step ActivationValidationError. Épinglé par
  // PRÉFIXE de nom : une correspondance exacte sur « Finance » laisserait passer
  // une option réintroduite sous le libellé « Finance — … ».
  for (const retired of ["Finance", "Reference", "Operations"]) {
    expect(within(role).queryByRole("option", { name: new RegExp(`^${retired}( —|$)`) })).not.toBeInTheDocument();
  }
});

it("refuses the next section until a data role is given, and says what it costs", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);

  // An observed source is no longer a settled section: the role decides which
  // fee and tax rules can ever match, and it has not been given. Le refus et sa
  // phrase vivent à la fin de `Source` — c'est le
  // primaire de CETTE section, « Continue to configure », qui reste fermé, et
  // le rail n'offre pas `Configure` tant que la section courante est incomplète.
  expect(await screen.findByText(/matches no fee or tax rule/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Continue to configure" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /^Configure/ })).not.toBeInTheDocument();

  // Le guichet exige le nom AUSSI : la proposition du contrat n'est plus
  // atteignable dans la marche avant (la famille de rapport se choisit une
  // section plus loin), donc le nom se saisit — le rôle seul ne suffit plus.
  await user.type(screen.getByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(screen.getByLabelText(/Data role/), "Spend");
  expect(screen.getByRole("button", { name: "Continue to configure" })).toBeEnabled();
  expect(await screen.findByRole("button", { name: /^Configure/ })).toBeInTheDocument();
});

it("states the absence of a category instead of guessing one", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    connectors: [{ ...options.connectors[0], source_category: null, source_category_origin: null }],
  });
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);

  const category = screen.getByLabelText(/Source category/);
  expect(category).toHaveValue("");
  expect(category).toHaveAttribute("placeholder", "This connector's manifest declares no category");
});

it("editing the identity after a compile still invalidates the proposal", async () => {
  // The invalidation loop did not disappear when the two fields moved; it MOVED.
  // `DatastreamPreconfiguration.test.tsx` used to exercise it at `Classify and
  // map`, which is no longer where these fields live — c'est la fin de
  // c'est la fin de `Source`. Both halves still hold: `edit()` drops the
  // proposal, and the server counts `name` and `data_role` among the
  // dependencies it invalidates (`datastream_preconfiguration.py`).
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);
  await user.type(screen.getByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(screen.getByLabelText(/Data role/), "Spend");
  await user.click(screen.getByRole("button", { name: "Continue to configure" }));

  // La famille de rapport OUVRE `Configure` :
  // c'est elle qui remplit les listes de champs (Date field, Metrics,
  // Dimensions). Un PREMIER choix n'invalide rien — la découverte ne nommait
  // pas de rapport — donc aucun ConfirmDialog ici.
  await user.selectOptions(await screen.findByLabelText(/Report family/), "daily");
  await user.selectOptions(await screen.findByLabelText(/Date field/), "date");
  await user.click(screen.getByRole("checkbox", { name: "Spend (metric)" }));
  await user.click(screen.getByRole("checkbox", { name: "Reporting date (dimension)" }));
  await user.click(screen.getByRole("button", { name: "Compile proposal" }));
  expect(await screen.findByRole("button", { name: "Continue to classify and map" })).toBeInTheDocument();

  // Back to `Source` — offered by the rail, every stop before it being
  // complete — rename, and the compiled proposal is no longer current.
  await user.click(await screen.findByRole("button", { name: /^Source/ }));
  await user.type(await screen.findByLabelText(/Datastream name/), " EU");
  await user.click(await screen.findByRole("button", { name: /^Configure/ }));
  expect(await screen.findByRole("button", { name: "Compile proposal" })).toBeInTheDocument();
});

it("changing the mode asks first — and keeps the name and the data role", async () => {
  // 57.12. A mode card is not a reason to take back answers that no mode owns:
  // `selectMode` used to write `newInput(mode)` whole, erasing the typed name
  // and the chosen role in the same gesture. The asking is the console's
  // `ConfirmDialog`, and until it is confirmed NOTHING has changed.
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);
  await user.type(screen.getByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(screen.getByLabelText(/Data role/), "Spend");

  // The mode question stays on the step it opens: it is asked here, above the
  // answers it decides, so changing it takes no navigation at all.
  await user.click(screen.getByRole("radio", { name: "Managed feed" }));

  const dialog = await screen.findByTestId("datastream-setup-confirm");
  expect(dialog).toHaveTextContent(/Changing the source mode invalidates dependent observations/);
  // Pending, not applied: the mode's own questions are not drawn yet.
  expect(screen.queryByLabelText(/Channel/)).not.toBeInTheDocument();

  await user.click(screen.getByTestId("datastream-setup-confirm-accept"));

  // The mode changed: the new mode's own question takes the place of the old
  // one's, in the same step, under the same row of cards.
  expect(await screen.findByLabelText(/Channel/)).toBeInTheDocument();

  // The identity did not: the name and the role are re-read at the end of the
  // same step, which a `file_upload` feed reaches only after a file is
  // quarantined and discovered — hence the upload (mocked, no byte travels).
  const file = new File(["date,spend\n"], "daily.csv", { type: "text/csv" });
  await user.upload(await screen.findByLabelText(/CSV, Excel or SAV file/), file);
  await screen.findByText(/held in draft quarantine/);
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  // Le nom épinglé est celui SAISI à l'aller : la proposition du contrat
  // (« Generic Daily — Daily report ») n'est plus atteignable dans la marche
  // avant — l'intention est inchangée : le
  // changement de mode n'a repris ni le nom ni le rôle.
  expect(await screen.findByLabelText(/Datastream name/)).toHaveValue("Daily spend");
  expect(screen.getByLabelText(/Data role/)).toHaveValue("Spend");
});

it("cancelling the mode change leaves every answer where it was", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await walkToNameAndRole(user);
  await user.type(screen.getByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(screen.getByLabelText(/Data role/), "Spend");
  await user.click(screen.getByRole("button", { name: "Continue to configure" }));
  // The report family is a `Configure` question, so its kept value is pinned
  // in the section that asks it.
  await user.selectOptions(await screen.findByLabelText(/Report family/), "daily");

  await user.click(screen.getByRole("button", { name: /^Source/ }));
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  await user.click(await screen.findByTestId("datastream-setup-confirm-cancel"));

  // Nothing moved. Question 1 was ANSWERED by the walk, so the Connector
  // question shows the folded answer — the mark, the name and `Change` —
  // rather than the catalogue, and the mode is still `connector_pull`.
  const change = await screen.findByRole("button", { name: "Change" });
  expect(within(change.parentElement as HTMLElement).getByText("Generic Daily")).toBeInTheDocument();
  expect(screen.queryByLabelText(/Channel/)).not.toBeInTheDocument();
  // The name and the role are re-read WITHOUT LEAVING THIS STEP: they are its
  // last two questions, not a stop of their own.
  expect(screen.getByLabelText(/Data role/)).toHaveValue("Spend");
  await user.click(screen.getByRole("button", { name: /^Configure/ }));
  expect(await screen.findByLabelText(/Report family/)).toHaveValue("daily");
});

it("names the failed source-options read rather than rendering an empty proposal", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockRejectedValue(new Error("Source options are unavailable"));
  // THE FAILURE IS SAID WHERE IT BITES, ON THE FIRST SCREEN. It used to be a
  // line of the name-and-role panel — the last block of the step, drawn only
  // once the source has answered — so a walk that failed at the very first
  // read never reached the sentence that explained it. No resume shortcut is
  // needed to see it any more: the step opens on it.
  render(<DatastreamSetupWizard projectId="proj_1" />);

  expect(await screen.findByText(/no Connector, account, name or category can be/)).toBeInTheDocument();
  expect(screen.getByText(/reopen this draft to read them again/)).toBeInTheDocument();
});
