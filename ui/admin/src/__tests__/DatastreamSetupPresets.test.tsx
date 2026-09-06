/**
 * 57.6 — le catalogue de presets est DÉRIVÉ, complétable, et ne ment sur aucune
 * de ses trois absences.
 *
 * Ce que ce fichier tient, et rien d'autre :
 *
 *   * une carte par rapport du Connecteur, portant le NOM du profil. Sans la
 *     jointure `report_profiles[].display_name`, 131 rapports sur 133 affichent
 *     un identifiant technique — et une carte qui affiche `daily_v2` n'est pas
 *     une carte. La fixture porte un identifiant et un nom distincts, donc ce
 *     test rougit le jour où la jointure est perdue ;
 *   * la carte montre le plus petit grain DÉCLARÉ, les deux comptes de champs,
 *     et la fenêtre étiquetée `Platform default` — aucun des 133 profils ne
 *     déclare de fenêtre, et la rendre nue l'attribuerait au Connecteur ;
 *   * cliquer pré-remplit les métriques et les dimensions, et ajouter une
 *     dimension par-dessus conserve les précédentes. C'est le seul test qui
 *     prouve le mot « complétable » ;
 *   * trois refus, trois phrases : `Unavailable` avec son `reason_code`,
 *     `Stale` pour un rapport sorti du contrat vérifié, et `Unverified` — qui
 *     n'est ni l'un ni l'autre et reste SÉLECTIONNABLE, parce qu'aucun
 *     connecteur de ce déploiement n'a de contrat vérifié et que la sûreté du
 *     premier tirage est jugée ailleurs (`recommend_first_report`, 36.8) ;
 *   * zéro connecteur rend la phrase du vide, de ton `warning` et non `error` :
 *     c'est l'état NORMAL ici, un inventaire du travail restant.
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
    createDatastreamSetupObservation: vi.fn(),
    // 57.7 : l'étape `Source` lit aussi les templates de l'organisation. Non
    // moqué, l'appel réel échoue et rend le bandeau d'erreur qui NOMME la route
    // — un `role="alert"` légitime, mais qui n'est pas celui que le test du vide
    // ci-dessous interroge. La liste vide est la lecture neutre attendue ici.
    listDatastreamSetupTemplates: vi.fn(),
  };
});

/** La carte, et rien que la carte. Plusieurs blocs de l'écran emploient les
 *  mêmes mots — l'écran entier n'est pas la portée d'une assertion sur un
 *  badge. */
function cardOf(name: string): HTMLElement {
  // TOUTES les occurrences, puis celle qui est DANS une carte. Depuis
  // l'amendement du 2026-08-10 le Connecteur est répondu avant, donc le nom du
  // rapport paraît deux fois sur l'écran — sur la carte et dans l'option de
  // `Report family`. Un `getByText` unique échouerait sur l'ambiguïté au lieu de
  // nommer la carte.
  const card = screen.getAllByText(name).map((node) => node.closest("li")).find(Boolean);
  if (!card) throw new Error(`No starting-point card named ${name}`);
  return card;
}

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

/** L'identifiant et le nom DIFFÈRENT, exprès : c'est ce qui rend la jointure
 *  observable. Un rapport dont le nom serait `campaign_daily` laisserait passer
 *  la régression que cette story répare. */
const campaignDaily: wizardApi.DatastreamSetupReportOption = {
  report_ref: "campaign_daily",
  display_name: "Campaign performance (daily)",
  availability: { status: "selectable" },
  metrics: ["spend", "clicks"],
  dimensions: ["date", "campaign", "country"],
  supported_grains: [["date"], ["date", "campaign"]],
  smallest_declared_grain: ["date"],
  safety: { outcome: "recommended", reason: "only_safe_report_family" },
};

const withdrawnReport: wizardApi.DatastreamSetupReportOption = {
  report_ref: "creative_daily",
  display_name: "Creative performance (daily)",
  availability: { status: "unavailable", reason_code: "endpoint_retired_by_provider" },
  metrics: ["spend"],
  dimensions: ["date", "creative"],
  supported_grains: [["date"]],
  smallest_declared_grain: ["date"],
  safety: { outcome: "no_safe_recommendation", reason: "not_a_safe_candidate" },
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
      { field_id: "country", kind: "dimension", description: "Country" },
      { field_id: "spend", kind: "metric", description: "Spend" },
      { field_id: "clicks", kind: "metric", description: "Clicks" },
    ],
    ...overrides,
  };
}

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic-ads" }, label: "Northern market account", states: { availability: "available", authorization: "healthy" } }],
  connectors: [connectorOption()],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [],
  recommendations: [],
  platform_defaults: { date_window_days: 30, window_offset_days: 1, origin: "platform_default" },
};

let lastSavedInput: Record<string, unknown> | undefined;

beforeEach(() => {
  lastSavedInput = undefined;
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockRejectedValue(new Error("not materialized"));
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({ templates: [], count: 0, limit: 10 });
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", draft_revision: 2,
    mode: "connector_pull", discovery_kind: "connector_contract", adapter_ref: "connector_contract",
    connector_contract_version_ref: "ccv_1", request_fingerprint: "b".repeat(64),
    evidence_fingerprint: "c".repeat(64), schema_hash: "d".repeat(64),
    safe_metadata: { fields: [{ name: "date", type: "DATE" }] }, coverage: { field_list: "complete" },
    exceptions: [], observed_at: "2026-08-05T00:00:00Z", expires_at: null, idempotent_replay: false,
  });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    lastSavedInput = input;
    return { ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input };
  });
});

/** Le mode, PUIS le Connecteur, PUIS le compte — l'ordre ratifié le 2026-08-10,
 *  conditionné le même jour (57.12, passe UX). DEPUIS L'AMENDEMENT DU
 *  ratifié, ces réponses vivent dans la MÊME section `Source` : le mode
 *  se choisit seul à `Mode`, le connecteur et le compte à `Source`, atteinte
 *  par le primaire contextuel du pied. Les cartes de démarrage ne sont plus là :
 *  elles OUVRENT `Configure`, dont elles remplissent les champs — un rapport
 *  offert avant que rien ne puisse le servir est une promesse sans source.
 *  `connector: null` sert le seul cas où il n'y a rien à choisir, le catalogue
 *  vide — dont la phrase se lit à `Source`, où les cartes de connecteur
 *  manquent. */
async function openConnectorPull(connector: string | null = "Generic Ads") {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  if (connector) {
    await user.click(await screen.findByRole("radio", { name: connector }));
    await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  }
  return user;
}

/** LA MARCHE JUSQU'À `CONFIGURE`, où vivent désormais les cartes (amendement du
 *  ratifié) : la source doit être OBSERVÉE — le primaire `Continue to
 *  identity` ne s'active qu'avec l'observation, d'où le `waitFor` — puis
 *  l'identité est sa propre section. Le nom s'y SAISIT : la proposition
 *  automatique lisait la famille de rapport, qui n'est choisie qu'une section
 *  plus loin, à `Configure` — elle est donc inatteignable dans la marche avant
 *  (conséquence connue et déjà signalée de l'amendement, ne pas « réparer »). */
async function walkToConfigure(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Some name");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  await screen.findByLabelText("Configure section");
}

it("renders one card per declared report, under the profile's NAME", async () => {
  const user = await openConnectorPull();
  // Les cartes OUVRENT `Configure` : elles en
  // remplissent les champs. L'assertion est inchangée — la marche, elle, suit
  // les sections.
  await walkToConfigure(user);

  expect(await screen.findByText("Starting points")).toBeInTheDocument();
  // DANS LA CARTE, et non n'importe où : la liste `Report family`, qui porte le
  // même nom de rapport, est VOISINE des cartes en tête de `Configure` depuis
  // la restructuration. Une assertion à la portée de l'écran ne dirait
  // plus lequel des deux elle a trouvé.
  expect(within(cardOf("Campaign performance (daily)")).getByText("Campaign performance (daily)")).toBeInTheDocument();
  // Le repli technique ne doit apparaître nulle part : s'il apparaît, la
  // jointure du serveur a été perdue et 131 cartes sur 133 sont illisibles.
  expect(screen.queryByText("campaign_daily")).not.toBeInTheDocument();
});

it("the card carries the grain, both field counts, and the window as a PLATFORM default", async () => {
  const user = await openConnectorPull();
  await walkToConfigure(user); // les cartes vivent à `Configure`

  const card = within(await waitFor(() => cardOf("Campaign performance (daily)")));
  expect(card.getByText("Smallest declared grain")).toBeInTheDocument();
  expect(card.getByText("date")).toBeInTheDocument();
  expect(card.getByText("2 metrics, 3 dimensions")).toBeInTheDocument();
  // Le mot « Platform default » EST l'exigence : le Connecteur ne déclare
  // aucune fenêtre, et un « 30 days » nu la lui attribuerait.
  expect(card.getByText("Window (Platform default)")).toBeInTheDocument();
  expect(card.getByText("30 days, offset 1 day")).toBeInTheDocument();
});

it("selecting a card pre-fills the fields, and the preset survives being narrowed", async () => {
  const user = await openConnectorPull();

  // LA CARTE EST À `CONFIGURE` : on y marche D'ABORD,
  // et c'est là qu'on la choisit. Le clic est un premier choix de famille après
  // une découverte sans rapport — une simple édition, sans confirmation (bug
  // corrigé depuis : seul CHANGER une famille existante invalide les
  // preuves). L'ordre du test s'inverse donc ; ses assertions, non.
  await walkToConfigure(user);
  await user.click(await screen.findByRole("button", { name: /Start from Campaign performance \(daily\)/ }));

  await waitFor(() => expect(lastSavedInput?.configure).toEqual(expect.objectContaining({
    metrics: "spend,clicks",
    dimensions: "date,campaign,country",
    grain: "date,campaign,country",
  })));
  // Le bouton dit son état à un lecteur d'écran, pas seulement à l'œil.
  expect(screen.getByRole("button", { name: /Start from Campaign performance \(daily\)/ })).toHaveAttribute("aria-pressed", "true");

  // Les deux listes que le preset a remplies sont SOUS la carte, dans la même
  // section `Configure` — plus de navigation entre le choix et sa preuve.

  // La pré-sélection EST dans le contrôle : les trois dimensions déclarées,
  // cochées (57.12, T5 — les deux listes sont des checkboxes).
  for (const name of [
    "Reporting date (dimension)", "Campaign (dimension)", "Country (dimension)",
  ]) {
    expect(screen.getByRole("checkbox", { name })).toBeChecked();
  }

  // LA PERTE ACCIDENTELLE EST PARTIE AVEC LE MULTI-SELECT : décocher est un
  // geste délibéré, un champ à la fois, et le grain suit les dimensions comme
  // avant. La différence reste comptée en mots, et le retour au preset reste
  // UN clic — l'écart volontaire existe toujours, lui.
  await user.click(screen.getByRole("checkbox", { name: "Campaign (dimension)" }));
  await user.click(screen.getByRole("checkbox", { name: "Country (dimension)" }));
  await waitFor(() => expect(lastSavedInput?.configure).toEqual(expect.objectContaining({ dimensions: "date", grain: "date" })));
  expect(await screen.findByText("This selection differs from the Connector's declared preset")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Back to the preset selection" }));
  await waitFor(() => expect(lastSavedInput?.configure).toEqual(expect.objectContaining({
    metrics: "spend,clicks", dimensions: "date,campaign,country", grain: "date,campaign,country",
  })));
});

it("a report the contract declares unavailable is refused, with its reason code", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    connectors: [connectorOption({ reports: [campaignDaily, withdrawnReport] })],
  });

  const user = await openConnectorPull();
  await walkToConfigure(user); // la carte refusée se lit à `Configure`

  const card = within(await waitFor(() => cardOf("Creative performance (daily)")));
  expect(card.getByText("Unavailable")).toBeInTheDocument();
  // Le code du contrat, jamais un message générique.
  expect(card.getByText(/endpoint_retired_by_provider/)).toBeInTheDocument();
  expect(card.getByRole("button", { name: /Start from Creative performance \(daily\)/ })).toBeDisabled();
});

it("a saved report that left the verified contract reads Stale, and is refused", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue({
    ...draft,
    operator_input: {
      mode: "connector_pull",
      name: "",
      source: { source_account_ref: "sacct_1", connector_ref: "generic-ads", connector_contract_version_ref: "ccv_1", report_ref: "retired_daily" },
      configure: { date_field: "", metrics: "", dimensions: "", date_window: "", filters: "", history_intent: "", cadence_intent: "", grain: "" },
      // OUVERT DIRECTEMENT SUR `Configure` : la carte
      // `Stale` y vit désormais, en tête de la section — plus à `Source`.
      // `active_section_ref` l'emporte par son NOM ; sans lui, la reprise
      // ouvrirait sur `Mode` et la carte serait hors de portée. La source ne
      // porte pas d'`observation_ref`, donc aucune observation à moquer, et le
      // brouillon n'a pas de proposition — rien d'autre ne manque à la reprise.
      wizard_state: { active_section_ref: "configure" },
    },
  });

  render(<DatastreamPreconfiguration projectId="proj_1" resumeDraftId="dsd_1" />);

  expect(await screen.findByText("Stale")).toBeInTheDocument();
  expect(screen.getByText(/no longer in the verified contract/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Start from retired_daily/ })).toBeDisabled();
});

it("a Connector with no verified contract reads Unverified — and stays selectable", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    connectors: [connectorOption({ contract_state: "unverified", contract_version_ref: "", contract_fingerprint: "", observed_at: null })],
  });

  const user = await openConnectorPull();
  await walkToConfigure(user); // la carte et le bloc contrat vivent à `Configure`

  const badge = await screen.findByText("Unverified");
  expect(badge).toBeInTheDocument();
  // Ni « Stale », ni une panne : la phrase nomme QUAND un contrat est épinglé.
  // Elle ne renvoie plus vers un run de vérification (AI-279) : c'est la
  // LIAISON qui écrit le contrat, et rien ne l'écrit à l'avance.
  // DEUX FOIS, et c'est exact : la carte le dit, et le bloc `Report family` le
  // redit pour le contrat lui-même — VOISINS en tête de `Configure` depuis
  // la restructuration (ils partageaient déjà la même question avant).
  // La phrase est UNE, sa constante est UNE ; ce qui est compté ici, ce sont
  // ses deux emplacements.
  expect(screen.getAllByText(/The contract is pinned when the Connector is bound/)).toHaveLength(2);
  expect(screen.queryByText("Stale")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Start from Campaign performance \(daily\)/ })).toBeEnabled();
});

it("a Connector whose module moved past its pin says so — and stays selectable", async () => {
  // AI-279. `stale` ici veut dire : le MODULE a changé depuis l'épinglage. La
  // liste montre ce que le module sert aujourd'hui, donc rien n'est refusé ; ce
  // qui est signalé, ce sont les Datastreams bâtis sur l'ancien contrat.
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    connectors: [connectorOption({ contract_state: "stale" })],
  });

  const user = await openConnectorPull();
  await walkToConfigure(user); // the `is behind the Connector` block and the card both live on `Configure`

  expect(await screen.findByText(/is behind the Connector/)).toBeInTheDocument();
  expect(screen.getByText(/Datastreams bound to the pinned contract keep it/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Start from Campaign performance \(daily\)/ })).toBeEnabled();
});

it("Discover source needs no pinned contract — the binding is what writes one", async () => {
  // La condition `connector_contract_version_ref` désactivait ce bouton pour
  // TOUT connecteur que personne n'avait encore lié, c'est-à-dire tous. C'est
  // exactement ce qui a fait écrire 39 lignes en base pour qu'il s'allume.
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    connectors: [connectorOption({ contract_state: "unverified", contract_version_ref: "", contract_fingerprint: "", observed_at: null })],
  });

  const user = await openConnectorPull();
  // PLUS DE `Report family` À CETTE SECTION : la
  // famille a déménagé en tête de `Configure`, DEUX sections plus loin, et la
  // découverte n'en a plus besoin — `canDiscover` ne demande que le compte et
  // le connecteur (l'exiger rendait la découverte impossible, bug corrigé le
  // `Configure`). L'intention est intacte : AUCUN contrat épinglé n'est requis
  // pour que le bouton s'allume — c'est la liaison qui l'écrit.
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");

  await waitFor(() => expect(screen.getByRole("button", { name: "Discover source" })).toBeEnabled());
});

it("no Connector module at all renders the sentence, and no grid", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({ ...options, connectors: [] });

  await openConnectorPull(null);

  // LE CATALOGUE EST LE REGISTRE (AI-279), donc une liste vide veut dire qu'AUCUN
  // module n'est installé -- pas qu'un run de vérification est dû. Nommer la
  // mauvaise absence envoyait l'opérateur chercher un run à déclencher.
  const empty = await screen.findByText("No Connector in this deployment");
  // Ton `warning`, jamais `error` : c'est la lecture attendue d'un déploiement
  // sans module, pas un incident. Dans le DOM,
  // `Status` ne prend `role="alert"` QUE pour `error` ; tout le reste annonce
  // poliment. La phrase est donc dans un `status`, et il n'y a pas d'alerte.
  expect(empty.closest("[role]")).toHaveAttribute("role", "status");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByText("Starting points")).not.toBeInTheDocument();
  expect(screen.queryByText("Campaign performance (daily)")).not.toBeInTheDocument();
});
