/**
 * AI-174 — l'étape 1 recommande, ou elle est une page blanche.
 *
 * La cible exige de `Source`, en mode `connector_pull` : « **Recommended:** best
 * account and report family from project intent and observed metadata »
 * (`datastream-workbench-and-wizard.md:48`). Le front n'en rendait rien — trois
 * `<select>` vides et sans classement.
 *
 * Ce que ce fichier tient :
 *
 *   * les cartes se rendent, avec leur preuve et leur confiance (`:96` exige les
 *     deux de toute proposition) ;
 *   * une recommandation appliquée par-dessus une évidence déjà recueillie passe
 *     par la MÊME confirmation que les autres éditions amont — sinon c'est le
 *     seul endroit du wizard où une proposition écrit sans demander ;
 *   * zéro contrat ne rend aucune carte : un déploiement sans contrat persisté
 *     n'a pas de point de départ, et l'inventer serait la faute d'origine.
 *
 * DEUX LIGNES ONT CHANGÉ AVEC LA STORY 57.6, et elles ont changé parce que la
 * cible a changé :
 *
 *   * le panneau s'appelle « Starting points » : il ne porte plus seulement les
 *     recommandations appariées à un compte, mais une carte par rapport déclaré
 *     du Connecteur ;
 *   * cliquer PRÉ-REMPLIT `configure.metrics`, `configure.dimensions` et le
 *     grain qui les suit. C'est exactement ce qui fait d'une carte un preset
 *     complétable (`datastream-workbench-and-wizard.md:287-289`) ; ce qui reste
 *     interdit — et que le test tient toujours — est d'écrire la fenêtre, la
 *     cadence, les filtres ou le champ de date, qui appartiennent au
 *     compilateur.
 *
 * LE PANNEAU A DÉMÉNAGÉ EN TÊTE DE `Configure` : le wizard est passé de cinq
 * sections défilantes à cinq ARRÊTS, une section visible à la fois
 * (`source` → `configure` → …), et « Starting points » a
 * quitté la question 4 de `Source` pour OUVRIR `Configure` — c'est lui, avec la
 * famille de rapport, qui remplit les champs de cette section. Aucune assertion
 * de ce fichier n'a changé de sens : seul le chemin change, et chaque test
 * marche désormais jusqu'à `Configure` (mode → source OBSERVÉE → nom et rôle →
 * configure) avant de lire une seule carte.
 */
import { render, screen, waitFor } from "@testing-library/react";
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
    createDatastreamSetupObservation: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

const recommendation: wizardApi.DatastreamSetupRecommendation = {
  recommendation_ref: "generic:sacct_1:daily",
  confidence: { level: "high", rationale: "One safe report family in the persisted contract for this account." },
  source_account_ref: "sacct_1", source_account_label: "Acme account",
  connector_ref: "generic", connector_display_name: "Generic Daily",
  connector_contract_version_ref: "ccv_1",
  report_ref: "daily", report_display_name: "Daily report",
  derived_grain: ["date"], metric_count: 1, dimension_count: 1, currency: "EUR",
  estimated_cost: { read_points: 30, unit: "read_points", window_days: 30, quota_pre_check: "ok" },
  evidence_refs: [
    { kind: "connector_contract", object_type: "connector", object_id: "generic", version_id: "ccv_1", fingerprint: "a".repeat(64), observed_at: "2026-08-04T00:00:00Z" },
    { kind: "observed_metadata", object_type: "source-account", object_id: "sacct_1", version_id: "unavailable", fingerprint: "1", observed_at: "2026-08-04T00:00:00Z" },
  ],
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{ connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64), display_name: "Generic Daily", observed_at: "2026-08-04T00:00:00Z", reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"], supported_grains: [["date"]], quota_cost: { read_points: 1 } }], fields: [{ field_id: "date", kind: "dimension", physical_type: "date" }, { field_id: "spend", kind: "metric" }] }],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [],
  recommendations: [recommendation],
};

/** La plus petite observation qui fasse passer `Source` : quitter l'étape
 *  exige une source OBSERVÉE (son primaire est verrouillé sinon, et le nom et
 *  le rôle ne sont même pas encore posés), et la découverte lit le contrat du
 *  connecteur — plus aucune famille de rapport n'est envoyée, elle se choisit
 *  à `Configure`. */
const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_1",
  draft_revision: 1, mode: "connector_pull", discovery_kind: "connector_contract",
  adapter_ref: "generic.contract.v1", connector_contract_version_ref: "ccv_1",
  request_fingerprint: "b".repeat(64), evidence_fingerprint: "c".repeat(64), schema_hash: null,
  safe_metadata: { report_refs: ["daily"], field_ids: ["date", "spend"], cadence: ["daily"], history: "90 days", quota_cost: "1 request" },
  coverage: { fields: "available" }, exceptions: [], observed_at: "2026-08-04T00:01:00Z",
  expires_at: null, idempotent_replay: false,
};

let lastSavedInput: Record<string, unknown> | undefined;

beforeEach(() => {
  lastSavedInput = undefined;
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    lastSavedInput = input;
    // Une sauvegarde qui porte déjà l'observation ne fait pas avancer la
    // révision : la découverte a été créée CONTRE la révision courante, et la
    // faire dériver invaliderait la preuve que le wizard vient d'attacher.
    const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1;
    return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input };
  });
});

/** LE PARCOURS JUSQU'AUX CARTES : le mode, le Connecteur, le compte, la
 *  découverte SANS famille de rapport puis le nom et le rôle — tout cela dans
 *  l'étape `Source` —, et enfin `Configure`, dont les cartes « Starting
 *  points » sont la première question. Avant la restructuration ce helper
 *  s'arrêtait au compte : les cartes se lisaient dans la même page défilante. */
async function walkToStartingPoints() {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // Le primaire de `Source` se déverrouille en ASYNCHRONE, une fois
  // l'observation attachée : cliquer un bouton désactivé est un no-op silencieux.
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  // Le nom n'est plus proposé avant `Identity` : la famille de rapport qui
  // alimentait la proposition est choisie une section plus loin. On le saisit.
  await user.type(await screen.findByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  const toConfigure = await screen.findByRole("button", { name: "Continue to configure" });
  await waitFor(() => expect(toConfigure).toBeEnabled());
  await user.click(toConfigure);
  return user;
}

it("shows the recommended starting point with its evidence and confidence", async () => {
  await walkToStartingPoints();

  expect(await screen.findByText("Starting points")).toBeInTheDocument();
  // DEUX NOEUDS : la carte, et l'option de `Report family` — qui vit à côté,
  // en tête de `Configure` (avant : la même
  // question 4 de `Source`). Le compte est 2, la co-localisation a bougé.
  expect(screen.getAllByText("Daily report")).toHaveLength(2);
  expect(screen.getByText("Generic Daily — Acme account")).toBeInTheDocument();
  expect(screen.getByText("high confidence")).toBeInTheDocument();
  // `:96` — every proposal names its evidence source. Both of them, deduped.
  expect(screen.getByText("Connector contract, Observed schema")).toBeInTheDocument();
  expect(screen.getByText("30 read_points")).toBeInTheDocument();
});

it("applying one fills the source and the preset — and nothing the compiler owns", async () => {
  const user = await walkToStartingPoints();

  // PREMIÈRE sélection d'une famille de rapport APRÈS une découverte qui n'en
  // lisait plus aucune : c'est une édition simple, SANS confirmation — la
  // confirmation ne s'ouvre que pour CHANGER un rapport existant ou suivre une
  // carte appariée à un AUTRE compte.
  await user.click(await screen.findByRole("button", { name: /Start from Daily report/ }));

  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    source_account_ref: "sacct_1",
    connector_ref: "generic",
    connector_contract_version_ref: "ccv_1",
    // `report_ref` PERSISTE dans `source` même si sa question a déménagé à
    // `Configure` : la clé appartient à l'enveloppe du brouillon, pas à la
    // section qui la pose.
    report_ref: "daily",
  })));
  // The preset: the report's own declared lists, and the grain that follows the
  // dimensions. Nothing here is authored by the screen.
  expect(lastSavedInput?.configure).toEqual(expect.objectContaining({
    metrics: "spend", dimensions: "date", grain: "date",
  }));
  // The window, the cadence, the filters and the date field stay the
  // compiler's. A card that wrote them would be a second engine.
  expect(lastSavedInput?.configure).toEqual(expect.objectContaining({
    date_field: "", date_window: "", filters: "", cadence_intent: "",
  }));
});

it("the applied card reads as selected, from the input and not a second state", async () => {
  const user = await walkToStartingPoints();

  await user.click(await screen.findByRole("button", { name: /Start from Daily report/ }));

  expect(await screen.findByRole("button", { name: /Start from Daily report/ })).toHaveTextContent("Selected");
});

it("without an account-paired ranking, the contract's own report families remain", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({ ...options, recommendations: [] });

  await walkToStartingPoints();

  // Le classement par compte est vide ; le CATALOGUE, lui, vient du contrat et
  // n'en dépend pas. C'est la correction de 57.6 : le `return null` de ce
  // panneau est la raison pour laquelle personne ne l'avait jamais vu.
  expect(await screen.findByText("Starting points")).toBeInTheDocument();
  expect(screen.getAllByText("Daily report")).toHaveLength(2);
  expect(screen.queryByText("high confidence")).not.toBeInTheDocument();
});

it("is offered only where the target asks for it — not in managed feed", async () => {
  // RACCOURCI DE REPRISE : le panneau vit en tête de
  // `Configure`, un arrêt après le choix du mode — marcher un managed feed
  // jusque-là exigerait un fichier staged et une observation pour ne lire
  // qu'une absence. On ouvre donc le wizard DIRECTEMENT sur `Configure` :
  // `restoreSection` lit `wizard_state.active_section_ref` par son NOM, et le
  // `operator_input` porte le mode, sa `source` et son `configure` — le minimum
  // que la restauration lit sans condition.
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValueOnce({
    ...draft,
    operator_input: {
      mode: "managed_feed",
      source: { channel: "file_upload", source_account_ref: "", template_ref: "", staged_asset_ref: "", sheet_ref: "" },
      configure: { input_ref: "", parsing_contract: "header_row=1", logical_dataset_name: "", date_semantics: "", grain: "", write_mode: "replace" },
      wizard_state: { active_section_ref: "configure" },
    },
  });
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  // La preuve que `Configure` a bien rendu EN managed feed — sinon l'absence
  // des cartes ne vaudrait rien — puis l'absence elle-même : le catalogue est
  // un bloc `connector_pull`, la cible ne le demande que là.
  expect(await screen.findByLabelText("Configure section")).toBeInTheDocument();
  expect(await screen.findByLabelText(/Input reference/)).toBeInTheDocument();
  expect(screen.queryByText("Starting points")).not.toBeInTheDocument();
});
