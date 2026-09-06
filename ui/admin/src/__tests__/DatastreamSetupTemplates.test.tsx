/**
 * 57.7 — sauver une configuration validée, puis repartir d'elle en ne changeant
 * QUE le compte source.
 *
 * Ce que ce fichier tient, et rien d'autre :
 *
 *   * une carte par template de l'organisation, portant le NOM du rapport joint
 *     comme 57.6 le joint. La fixture porte `report_ref` et `display_name`
 *     distincts : le test rougit le jour où la jointure est perdue ;
 *   * cliquer remplit tout SAUF le compte — les deux moitiés, dans le même test,
 *     parce qu'un template qui remplirait aussi le compte ne servirait pas la
 *     duplication, et un template qui laisserait un champ de plus vide ne
 *     tiendrait pas sa promesse ;
 *   * choisir ensuite un compte qui SERT le connecteur du template conserve le
 *     connecteur et le rapport. C'est le seul test qui prouve que la variable
 *     ouverte ne détruit pas le template, et c'est la réparation de CLASSE :
 *     avant 57.7, `narrowedConnector` vidait les trois champs dès que le compte
 *     servait ≠ 1 connecteur — un consentement Google en ouvre DIX ;
 *   * un rapport sorti du contrat rend `Stale`, avec les MOTS de 57.6 et un
 *     bouton désactivé ; aucun second vocabulaire ;
 *   * le vide (`neutral`) et la panne (bandeau qui nomme la route) sont deux
 *     phrases différentes — une grille vide dirait « cette organisation n'a
 *     aucun template », ce qui serait faux ;
 *   * à la revue finale, `Save as template` dit ce qu'il emporte et ce qu'il
 *     n'emporte pas, et le refus serveur nommé s'affiche tel quel. Mesuré le
 *     2026-08-05 contre la préprod : `app.datastream_setup_materializations`
 *     est à 0 pour 45 Datastreams, donc ce refus est le SEUL rendu que ce
 *     déploiement peut produire aujourd'hui.
 *
 * CINQ ARRÊTS RATIFIÉS — une section visible à la fois, et
 * les templates ont déménagé : ils ouvrent désormais la section `Configure`
 * (c'est eux qui remplissent ses champs), au lieu d'être la question zéro de
 * l'ancienne section `Source`. Trois conséquences pour ce fichier :
 *
 *   * on n'y arrive plus en rendant l'assistant — il faut y être. Les tests de
 *     CARTES ouvrent le brouillon directement sur `Configure` par la position
 *     sauvegardée (`wizard_state.active_section_ref`, lue par son NOM) ; les
 *     tests de GESTES appliquent là, puis reculent d'une section par `Back`
 *     vers `Source`, où vivent le connecteur et le compte ;
 *   * appliquer un template lève désormais TOUJOURS la confirmation de 57.12 :
 *     `Configure` n'est atteignable qu'avec un mode choisi, et `input.mode`
 *     posé suffit à `setPendingConfirm`. Avant, les templates se lisaient sans
 *     mode et la boîte ne se levait jamais ;
 *   * le récapitulatif `config-summary` est la troisième zone de `:31` et se
 *     monte à chaque section. La ligne `From template`, elle, ne dit « account
 *     changed » qu'une fois le compte effectivement changé : c'est le test
 *     « account changed » qui paie la marche jusque-là.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
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
    readDatastreamMaterialization: vi.fn(),
    listDatastreamSetupTemplates: vi.fn(),
    saveDatastreamSetupTemplate: vi.fn(),
    // La marche complète jusqu'à `Schedule and activate` (le récapitulatif n'a
    // plus d'autre maison) passe par la découverte, la compilation et la
    // preview sûre — trois appels que seul le test « account changed » atteint.
    createDatastreamSetupObservation: vi.fn(),
    compileDatastreamSetupDraft: vi.fn(),
    createDatastreamSetupPreview: vi.fn(),
  };
});

/** La carte, et rien que la carte : plusieurs blocs de l'écran emploient les
 *  mêmes mots, et l'écran entier n'est pas la portée d'une assertion. */
function cardOf(label: string): HTMLElement {
  const heading = screen.getByText(label);
  const card = heading.closest("li");
  if (!card) throw new Error(`No template card named ${label}`);
  return card;
}

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

/** L'identifiant et le nom DIFFÈRENT, exprès : c'est ce qui rend la jointure
 *  observable, exactement comme dans la suite de 57.6. */
const campaignDaily: wizardApi.DatastreamSetupReportOption = {
  report_ref: "campaign_daily",
  display_name: "Campaign performance (daily)",
  availability: { status: "selectable" },
  metrics: ["spend", "clicks"],
  dimensions: ["date", "campaign"],
  supported_grains: [["date"], ["date", "campaign"]],
  smallest_declared_grain: ["date"],
};

function connectorOption(overrides: Partial<wizardApi.DatastreamSetupConnectorOption> = {}): wizardApi.DatastreamSetupConnectorOption {
  return {
    connector_ref: "generic-ads",
    contract_version_ref: "ccv_1",
    contract_fingerprint: "a".repeat(64),
    display_name: "Generic Ads",
    observed_at: "2026-08-05T00:00:00Z",
    contract_state: "verified",
    reports: [campaignDaily],
    fields: [
      { field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" },
      { field_id: "campaign", kind: "dimension", description: "Campaign" },
      { field_id: "spend", kind: "metric", description: "Spend" },
      { field_id: "clicks", kind: "metric", description: "Clicks" },
    ],
    ...overrides,
  };
}

/** Un consentement, plusieurs outils : le fournisseur est sur `connector_ref`
 *  et l'outil sur `opens`, comme la préprod les stocke. */
function account(id: string, label: string, opens: wizardApi.DatastreamSetupConnectorOpened[]): wizardApi.DatastreamSetupSourceAccount {
  return { object_ref: { id }, connector_ref: { id: "google" }, label, opens, states: { availability: "available", authorization: "ok" } };
}

const ADS = { connector_name: "generic-ads", display_name: "Generic Ads", available: true };
const ANALYTICS = { connector_name: "generic-analytics", display_name: "Generic Analytics", available: true };

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [
    account("sacct_north", "Northern market account", [ADS]),
    account("sacct_south", "Southern market account", [ADS]),
  ],
  connectors: [connectorOption()],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [],
  recommendations: [],
  platform_defaults: { date_window_days: 30, window_offset_days: 1, origin: "platform_default" },
};

/** Ce que le serveur rend : la charge est déjà expurgée — ni `observation_ref`,
 *  ni `staged_asset_ref`, ni `wizard_state`, ni compte source. */
function template(overrides: Partial<wizardApi.DatastreamSetupTemplate> = {}): wizardApi.DatastreamSetupTemplate {
  return {
    template_ref: "dst_1",
    label: "Daily spend by market",
    origin_project_ref: "proj_1",
    origin_datastream_ref: "ds_origin",
    mode: "connector_pull",
    connector_ref: "generic-ads",
    report_ref: "campaign_daily",
    origin_contract_version_ref: "ccv_1",
    origin_source_account_ref: "sacct_north",
    open_variables: ["source_account_ref"],
    created_by: "owner@example.com",
    created_at: "2026-08-04T09:00:00Z",
    operator_input: {
      mode: "connector_pull",
      name: "Daily spend by market",
      data_role: "Spend",
      domain_ids: [],
      schedule: { mode: "daily" },
      source: { connector_ref: "generic-ads", connector_contract_version_ref: "ccv_1", report_ref: "campaign_daily" },
      configure: {
        date_field: "date", metrics: "spend,clicks", dimensions: "date,campaign",
        date_window: "30", filters: "", history_intent: "recent", cadence_intent: "daily",
        grain: "date,campaign",
      },
    },
    ...overrides,
  };
}

/** L'OUVERTURE DIRECTE SUR `CONFIGURE`. Marcher
 *  Mode → Source → Identity pour lire une carte coûterait trois sections à
 *  chaque test ; le brouillon créé porte donc une position sauvegardée, que
 *  `restoreSection` lit par son NOM (`active_section_ref`). Trois clés sont
 *  exigées dans `operator_input`, faute de quoi l'assistant plante : `mode`
 *  (c'est lui qui déclenche TOUTE la restauration), `source` (lu
 *  inconditionnellement dès que `mode` est posé) et le `configure` propre au
 *  mode. Pas d'`observation_ref` ici : aucune preuve n'est restaurée, et
 *  `readDatastreamSetupObservation` n'a pas à être simulée. */
const CONNECTOR_PULL_INPUT = {
  mode: "connector_pull",
  source: { source_account_ref: "", connector_ref: "", connector_contract_version_ref: "", report_ref: "" },
  configure: { date_field: "", metrics: "", dimensions: "", date_window: "", filters: "", history_intent: "", grain: "" },
};
const EXTERNAL_BQ_INPUT = {
  mode: "external_bq",
  source: { access_ref: "", object_ref: "", declared_writer: "", readonly_acknowledged: false },
  configure: {
    watermark_semantics: "", logical_dataset_name: "", expected_freshness: "",
    verification_window: "", expected_history: "", row_filters: "",
  },
};
const MANAGED_FEED_INPUT = {
  mode: "managed_feed",
  source: { channel: "file_upload", source_account_ref: "", template_ref: "", staged_asset_ref: "", sheet_ref: "" },
  configure: {
    input_ref: "", parsing_contract: "header_row=1", logical_dataset_name: "",
    date_semantics: "", grain: "", write_mode: "replace",
  },
};

function draftAtConfigure(operatorInput: Record<string, unknown>): wizardApi.DatastreamSetupDraft {
  return {
    ...draft,
    operator_input: { ...operatorInput, wizard_state: { active_section_ref: "configure" } },
  };
}

/** Appliquer lève la confirmation (voir l'en-tête) : un mode est toujours
 *  choisi là où les templates se lisent désormais. Le `mode` filtre aussi la
 *  liste — un template n'est offert qu'à la section `Configure` de SON mode,
 *  donc chaque test ouvre le brouillon sur le mode du template qu'il lit. */
async function applyTemplate(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(await screen.findByRole("button", { name: `Start from template ${label}` }));
  await user.click(await screen.findByTestId("datastream-setup-confirm-accept"));
}

/** `Back` recule d'EXACTEMENT une section : Configure → Identity → Source. Les
 *  gestes de source (compte, connecteur) ne se font que là ; les assertions de
 *  champs remplis se font sur le brouillon sauvegardé, pas sur la section. */
async function backToSource(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Back" }));
  await screen.findByLabelText("Source section");
  await user.click(screen.getByRole("button", { name: "Back" }));
  await screen.findByLabelText("Source section");
}

/** LA QUESTION 1 RÉPONDUE N'EST PLUS UN CATALOGUE.
 *
 *  `SourceConnectorPull` replie la grille de connecteurs dès qu'un connecteur
 *  est choisi : une ligne qui porte sa marque, son nom, et `Change`. Un template
 *  répond cette question, donc après `applyTemplate` il n'y a plus AUCUN
 *  `role="radio"` de connecteur à l'écran — les trois cas ci-dessous
 *  l'attendaient encore et échouaient tous les trois sur la même cause.
 *
 *  Les deux helpers disent les deux gestes que le pli laisse : LIRE la réponse,
 *  et la ROUVRIR. Aucun ne relâche l'assertion — `chosenConnector` prouve que la
 *  réponse tient et la nomme, ce que le `toBeChecked()` d'une carte prouvait
 *  moins bien. */
function chosenConnector(): HTMLElement {
  const change = screen.getByRole("button", { name: "Change" });
  const row = change.parentElement;
  if (!row) throw new Error("The collapsed Connector answer has no row");
  return row;
}

async function reopenConnectors(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Change" }));
}

/** La découverte que `Discover source` produit dans la marche complète :
 *  le contrat du connecteur, SANS famille de rapport — elle se choisit à
 *  `Configure`, donc la découverte n'en
 *  envoie plus (`report_ref` absent de la requête) et n'en rapporte pas. */
const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_1",
  draft_revision: 1, mode: "connector_pull", discovery_kind: "connector_contract",
  adapter_ref: "generic-ads.contract.v1", connector_contract_version_ref: "ccv_1",
  request_fingerprint: "b".repeat(64), evidence_fingerprint: "c".repeat(64), schema_hash: null,
  safe_metadata: {
    report_refs: ["campaign_daily"], field_ids: ["date", "campaign", "spend", "clicks"],
    cadence: ["daily"], history: "90 days", quota_cost: "1 request",
  },
  coverage: { fields: "available" }, exceptions: [], observed_at: "2026-08-05T00:01:00Z",
  expires_at: null, idempotent_replay: false,
};

/** La plus petite proposition compilée qui laisse quitter `Configure` : non
 *  périmée, un mapping, aucune section requise bloquée. */
const proposal: wizardApi.DatastreamPreconfigurationProposal = {
  schema_version: "1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", proposal_ref: "dspp_1",
  dependency_fingerprint: "e".repeat(64), proposal_token: "f".repeat(64), content_hash: "0".repeat(64),
  is_stale: false, invalidation_causes: [], idempotent_replay: false, resume_href: "/resume",
  confirmed_intent_bundle: {
    joint_grain: ["date", "campaign"],
    field_mappings: [{ source_identity: "date", field_id: "date", role: "primary_date", semantic_type: "date", canonical_target: "date", aggregation: "none", sensitivity: "public", included: true }],
    outputs: [{ kind: "full_grain_output", state: "will_be_created" }],
  },
  configuration_summary: { existing: [], will_be_created: [], will_remain_a_proposal: [], downstream_impact: [] },
  sections: [],
};

let lastSavedInput: Record<string, unknown> | undefined;

beforeEach(() => {
  lastSavedInput = undefined;
  // Le `window.confirm` d'ici est devenu le `ConfirmDialog` de la console
  // (57.12) : rien à stubber, la boîte se conduit comme un composant.
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockRejectedValue(new Error("not materialized"));
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({
    templates: [template(), template({ template_ref: "dst_2", label: "Weekly reach", origin_project_ref: "proj_other" })],
    count: 2,
    limit: 10,
  });
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposal);
  vi.mocked(wizardApi.createDatastreamSetupPreview).mockResolvedValue({
    state: "done", job_ref: "job_1",
    preview: {
      preview_ref: "preview_1", draft_revision_ref: "dsdr_3", proposal_ref: "dspp_1",
      observation_ref: "dso_1", mode: "connector_pull", dependency_hash: "4".repeat(64),
      evidence_hash: "5".repeat(64),
      safe_evidence: { sample: [{ date: "2026-08-04" }], status: "verified" },
      status: "ready_for_review", is_stale: false,
    },
  });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    lastSavedInput = input;
    return { ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input };
  });
});

afterEach(() => { vi.restoreAllMocks(); });

it("renders one card per saved configuration, under the report's NAME and with the server's count", async () => {
  // Les cartes ouvrent `Configure` : le brouillon
  // s'ouvre directement sur cette section. Le mode posé filtre la liste — les
  // deux templates sont `connector_pull`, les deux restent visibles.
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  expect(await screen.findByText("Organization templates")).toBeInTheDocument();
  expect(screen.getByText("2 of 10 templates")).toBeInTheDocument();
  const card = cardOf("Daily spend by market");
  expect(within(card).getByText("Generic Ads — Campaign performance (daily)")).toBeInTheDocument();
  // Le repli technique ne doit apparaître nulle part : s'il apparaît, la
  // jointure `report_profiles[].display_name` de 57.6 a été perdue.
  expect(within(card).queryByText(/campaign_daily/)).not.toBeInTheDocument();
  expect(within(card).getByText("owner@example.com")).toBeInTheDocument();
  // Ce que la carte dit d'ouvert est LU sur `open_variables`, pas récité.
  expect(within(card).getByText(/The Source Account — this is the variable duplication exists for/)).toBeInTheDocument();
  // La seconde différence, énoncée : un template d'un autre Projet de la même
  // organisation s'applique ici, et la carte le dit au lieu de se taire.
  expect(within(cardOf("Weekly reach")).getByText("Saved in another Project of this organization")).toBeInTheDocument();
});

it("applying a template fills the configuration and leaves the Source Account EMPTY", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  await applyTemplate(user, "Daily spend by market");

  await waitFor(() => expect(lastSavedInput?.configure).toEqual(expect.objectContaining({
    metrics: "spend,clicks",
    dimensions: "date,campaign",
    grain: "date,campaign",
  })));
  expect(lastSavedInput?.data_role).toBe("Spend");
  expect(lastSavedInput?.mode).toBe("connector_pull");
  // LES DEUX MOITIÉS. Le connecteur et le rapport voyagent ; le compte, non.
  expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    connector_ref: "generic-ads",
    report_ref: "campaign_daily",
    source_account_ref: "",
  }));
  // Le compte se lit à `Source`, deux sections plus tôt (amendement du
  // ratifié) : `Back` recule d'une section à la fois — Configure, puis
  // Identity, puis Source — et le champ y dit son vide.
  await backToSource(user);
  expect((await screen.findByLabelText(/Source Account/) as HTMLSelectElement).value).toBe("");
});

/** UNE PHRASE PAR VARIABLE DÉCLARÉE, ET AUCUNE INVENTÉE.
 *
 *  La carte imprimait un littéral — « Origin — you will choose the account » —
 *  sur les trois modes. Il est FAUX pour deux d'entre eux :
 *  `_OPERATOR_SOURCE_KEYS['external_bq']` ne contient aucun
 *  `source_account_ref`, et `managed_feed` rouvre `template_ref` côté serveur
 *  sans qu'aucune phrase ne le dise. Ces trois tests épinglent la lecture de
 *  `open_variables` mode par mode : c'est là que la classe se répare, pas dans
 *  un `if (mode === …)`. */
it("an external BigQuery template reopens NOTHING, and says so instead of promising an account", async () => {
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({
    templates: [template({
      template_ref: "dst_bq", label: "Warehouse table of the quarter", mode: "external_bq",
      connector_ref: null, report_ref: null, origin_source_account_ref: null,
      // Mesuré : access_ref, object_ref, declared_writer et readonly_acknowledged
      // traversent tous l'organisation, donc le serveur ne rouvre rien.
      open_variables: [],
      operator_input: {
        mode: "external_bq", name: "Warehouse table", data_role: "Reference & targets",
        schedule: { mode: "daily" },
        source: { access_ref: "sacct_bq", object_ref: "project.dataset.table", declared_writer: "Nightly ELT", readonly_acknowledged: true },
        configure: { watermark_semantics: "ingested_at", logical_dataset_name: "warehouse_table", expected_freshness: "24h", verification_window: "7", expected_history: "365", row_filters: "" },
      },
    })],
    count: 1, limit: 10,
  });
  // La liste est filtrée par le mode choisi : la carte d'un template
  // `external_bq` ne se rend qu'à la section `Configure` d'un brouillon
  // `external_bq` (avant, aucun filtre n'existait,
  // les cartes se lisaient toutes à la même surface).
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(EXTERNAL_BQ_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  const card = cardOf(await screen.findByText("Warehouse table of the quarter").then(() => "Warehouse table of the quarter"));
  expect(within(card).getByText("Nothing is left to choose: this template applies whole.")).toBeInTheDocument();
  // LA PHRASE FAUSSE : ce mode n'a aucun compte source à choisir.
  expect(within(card).queryByText(/Source Account/)).not.toBeInTheDocument();
});

it("a managed feed template says BOTH of the references the server reopened", async () => {
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({
    templates: [template({
      template_ref: "dst_feed", label: "Media plan of the quarter", mode: "managed_feed",
      connector_ref: null, report_ref: null, origin_source_account_ref: null,
      // Ce que `derive_template_payload` déclare pour ce mode, prouvé par
      // `test_datastream_setup_templates.py::test_managed_feed_reopens_…`.
      open_variables: ["source_account_ref", "template_ref"],
      operator_input: {
        mode: "managed_feed", name: "Media plan", data_role: "Forecast & plan",
        schedule: { mode: "manual" },
        source: { channel: "file_upload", sheet_ref: "" },
        configure: { input_ref: "media_plan.csv", parsing_contract: "header_row=1", logical_dataset_name: "media_plan", date_semantics: "period_start", grain: "date,campaign", write_mode: "replace" },
      },
    })],
    count: 1, limit: 10,
  });
  // Même filtre que le test BigQuery : la carte d'un template `managed_feed`
  // exige le brouillon ouvert sur ce mode.
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(MANAGED_FEED_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  const card = cardOf(await screen.findByText("Media plan of the quarter").then(() => "Media plan of the quarter"));
  expect(within(card).getByText(/The Source Account/)).toBeInTheDocument();
  // LA SECONDE, qui n'était dite nulle part : un Template appartient à UN Projet.
  expect(within(card).getByText(/The file Template/)).toBeInTheDocument();
  expect(within(card).queryByText("Nothing is left to choose: this template applies whole.")).not.toBeInTheDocument();
});

it("a deployment that declares no open variable promises nothing, and says that too", async () => {
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({
    templates: [template({ open_variables: undefined })],
    count: 1, limit: 10,
  });
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  const card = cardOf(await screen.findByText("Daily spend by market").then(() => "Daily spend by market"));
  // Une absence ÉNONCÉE, jamais la phrase du compte servie par défaut.
  expect(within(card).getByText(/did not declare what this template leaves open/)).toBeInTheDocument();
  expect(within(card).queryByText(/The Source Account —/)).not.toBeInTheDocument();
});

it("a template whose saved account is gone stays applicable, and the empty field says so", async () => {
  // LE CINQUIÈME ÉTAT, et il n'est pas `Stale` : la carte reste cliquable parce
  // que le compte EST la variable ouverte. C'est le champ vide qui parle.
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({
    templates: [template({ origin_source_account_ref: "sacct_retired" })],
    count: 1,
    limit: 10,
  });
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  const apply = await screen.findByRole("button", { name: "Start from template Daily spend by market" });
  expect(apply).toBeEnabled();
  // La carte porte le FAIT, court ; la phrase actionnable appartient au champ
  // qui sera vide. Le même vide énoncé deux fois se lirait comme deux problèmes.
  expect(within(cardOf("Daily spend by market")).getByText("Saved account is gone — you will choose one")).toBeInTheDocument();
  await user.click(apply);
  await user.click(await screen.findByTestId("datastream-setup-confirm-accept"));

  // Et ce n'est PAS `Stale` : le rapport est toujours au contrat. Épinglé à
  // `Configure`, où vivent la carte et les Starting points — les deux badges
  // qui pourraient porter le mot.
  expect(screen.queryByText("Stale")).not.toBeInTheDocument();
  // La phrase actionnable vit à `Source`, dans le champ vidé : deux `Back`
  // depuis `Configure` (le champ et la carte ne
  // partagent plus aucune surface).
  await backToSource(user);
  expect(await screen.findByText("The saved account is no longer exposed to this Project. Choose one.")).toBeInTheDocument();
  expect((screen.getByLabelText(/Source Account/) as HTMLSelectElement).value).toBe("");
});

it("choosing an account that SERVES the template's Connector keeps the Connector and the report", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  await applyTemplate(user, "Daily spend by market");
  // La famille de rapport se choisit à `Configure` depuis l'amendement du
  // : le template l'a remplie, et on y est encore — l'épinglage
  // précède la marche, il n'a pas disparu avec le déménagement du champ.
  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    connector_ref: "generic-ads",
    report_ref: "campaign_daily",
  })));
  expect((screen.getByRole("combobox", { name: /^Report family/ }) as HTMLSelectElement).value).toBe("campaign_daily");

  // `sacct_south` n'est pas le compte d'origine : c'est exactement le geste de
  // duplication. Deux comptes servent `generic-ads`, donc l'ancien
  // `narrowedConnector` — `candidates.length !== 1` — vidait tout ici. Le
  // compte se choisit à `Source`, deux `Back` plus
  // tôt ; aucune observation n'existe encore, le choix ne lève aucune boîte.
  await backToSource(user);
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_south");

  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    source_account_ref: "sacct_south",
    connector_ref: "generic-ads",
    connector_contract_version_ref: "ccv_1",
    report_ref: "campaign_daily",
  })));
  // La réponse repliée EST la preuve : le connecteur du template a survécu au
  // changement de compte, et la ligne le nomme.
  expect(within(chosenConnector()).getByText("Generic Ads")).toBeInTheDocument();

  // Et le récapitulatif dit CE QUI DIFFÈRE de l'original. Il est monté depuis
  // le premier arrêt (troisième zone de `:31`) ; ce qui coûte la marche ici,
  // c'est la PREUVE : appliquer puis changer le compte les a toutes invalidées,
  // donc découverte, proposition et preview sûre sont refaites — le prix
  // honnête de cette assertion.
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  await user.click(await screen.findByRole("button", { name: "Compile proposal" }));
  await user.click(await screen.findByRole("button", { name: "Continue to classify and map" }));
  await user.click(await screen.findByRole("button", { name: "Continue to preview" }));
  await user.click(await screen.findByRole("button", { name: "Create safe preview" }));
  await user.click(await screen.findByRole("button", { name: "Review schedule" }));
  expect(within(await screen.findByTestId("config-summary")).getByText("Daily spend by market · account changed")).toBeInTheDocument();
});

/** LA MÊME GARDE, DANS LE SENS QUE L'AMENDEMENT DU 2026-08-10 REND POSSIBLE.
 *
 *  Elle s'énonçait « ce compte n'ouvre pas ce Connecteur » et se déclenchait au
 *  changement de COMPTE, parce que le compte était demandé en premier. Le
 *  Connecteur étant désormais la question 1, les comptes offerts sont ceux qui
 *  le servent : l'accident ne peut plus venir de là. Il vient du sens inverse —
 *  changer le Connecteur sous un compte déjà choisi — et ce chemin-là n'était
 *  gardé par rien. */
it("changing the Connector under a chosen account clears that account AND says why", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    source_accounts: [
      account("sacct_north", "Northern market account", [ADS]),
      account("sacct_analytics", "Analytics property", [ANALYTICS]),
    ],
    connectors: [connectorOption(), connectorOption({ connector_ref: "generic-analytics", contract_version_ref: "ccv_2", display_name: "Generic Analytics" })],
  });
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  await applyTemplate(user, "Daily spend by market");
  // Compte et connecteur se choisissent à `Source`.
  await backToSource(user);
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_north");
  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    source_account_ref: "sacct_north",
  })));

  // Aucune observation n'existe : changer de connecteur ne lève pas la
  // confirmation, il applique — et dit ce qu'il a pris. Le template a répondu la
  // question 1, donc la grille est repliée : le geste commence par la rouvrir,
  // exactement comme à l'écran.
  await reopenConnectors(user);
  await user.click(await screen.findByRole("radio", { name: "Generic Analytics" }));

  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    connector_ref: "generic-analytics",
    connector_contract_version_ref: "ccv_2",
    source_account_ref: "",
    report_ref: "",
  })));
  // JAMAIS UN VIDAGE MUET : la phrase nomme le compte perdu et le produit qui
  // l'a pris.
  expect(await screen.findByText(/Northern market account does not open Generic Analytics/)).toBeInTheDocument();
});

it("a template whose report left the contract reads Stale, with the words of 57.6, and cannot be applied", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    connectors: [connectorOption({ reports: [{ ...campaignDaily, report_ref: "other_daily", display_name: "Other report (daily)" }] })],
  });
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  const card = cardOf(await screen.findByText("Daily spend by market").then(() => "Daily spend by market"));
  expect(within(card).getByText("Stale")).toBeInTheDocument();
  // MOT POUR MOT la phrase du catalogue de 57.6 : un second vocabulaire ferait
  // lire deux problèmes là où il n'y en a qu'un.
  expect(within(card).getByText("This report is no longer in the verified contract for this Connector.")).toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "Start from template Daily spend by market" })).toBeDisabled();
});

it("an organization with nothing saved is NOT a failure, and a failed read is NOT an empty organization", async () => {
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({ templates: [], count: 0, limit: 10 });
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draftAtConfigure(CONNECTOR_PULL_INPUT));
  const { unmount } = render(<DatastreamPreconfiguration projectId="proj_1" />);

  // L'état vide est de nouveau ACCESSIBLE : la garde `visible.length === 0`
  // du bloc (écrite pour « aucun template DE CE MODE », une non-phrase assumée)
  // avalait aussi « aucun template DU TOUT » — or depuis l'amendement du
  // le bloc ne se rend qu'à `Configure`, où un mode est TOUJOURS
  // posé. La garde ne cache plus le bloc que lorsqu'elle a réellement filtré
  // quelque chose ; la phrase neutre et le badge sont donc épinglés ici, à
  // leur nouveau domicile (`Configure`), comme ils l'étaient à `Source`.
  await screen.findByLabelText("Configure section");
  expect(await screen.findByText("No saved configuration yet")).toBeInTheDocument();
  expect(screen.getByText("0 of 10 templates")).toBeInTheDocument();
  expect(screen.queryByText("Saved configurations could not be read")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Start from template/ })).not.toBeInTheDocument();
  unmount();

  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockRejectedValue(new Error("route unavailable"));
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  expect(await screen.findByText("Saved configurations could not be read")).toBeInTheDocument();
  expect(screen.getByText(/datastream-setup-templates/)).toBeInTheDocument();
  // AUCUNE carte : une grille vide dirait « cette organisation n'en a aucun ».
  expect(screen.queryByText("No saved configuration yet")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Start from template/ })).not.toBeInTheDocument();
});

it("at the final review, Save as template says what it carries, and renders the server's named refusal", async () => {
  const materialized: wizardApi.DatastreamSetupDraft = {
    ...draft,
    state: "materialized",
    operator_input: {
      ...template().operator_input,
      source: { ...(template().operator_input.source as Record<string, unknown>), source_account_ref: "sacct_north" },
      // La reprise lit la position par son NOM (`active_section_ref`), qui
      // l'emporte toujours sur le rang legacy — `schedule_activate`, où vivent
      // la revue et le bloc « Save as template ».
      wizard_state: { active_section: 4, active_section_ref: "schedule_activate", first_incomplete: "complete" },
    },
  };
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(materialized);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({
    state: "materialized", datastream_ref: "ds_created", candidate_state: "ready", lifecycle_state: "draft",
  });
  vi.mocked(wizardApi.saveDatastreamSetupTemplate).mockRejectedValue(
    new wizardApi.WizardApiError(422, "no_recorded_operator_input", "This Datastream was not created by the setup wizard, so no reusable operator input was ever recorded."),
  );
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" resumeDraftId="dsd_1" />);

  const save = await screen.findByRole("button", { name: "Save as template" });
  // Une sauvegarde dont on ne lit pas le contenu est une copie aveugle.
  expect(screen.getByText(/Carries: mode, Connector, report family/)).toBeInTheDocument();
  expect(screen.getByText(/Does not carry: the Source Account/)).toBeInTheDocument();

  expect(save).toBeDisabled();
  await user.type(screen.getByLabelText(/Template name/), "Daily spend, Southern market");
  expect(save).toBeEnabled();
  await user.click(save);

  // LE SEUL RENDU QUE CE DÉPLOIEMENT PEUT PRODUIRE AUJOURD'HUI (0
  // matérialisation pour 45 Datastreams) : le refus, avec les mots du serveur.
  expect(await screen.findByText(/was not created by the setup wizard/)).toBeInTheDocument();
});
