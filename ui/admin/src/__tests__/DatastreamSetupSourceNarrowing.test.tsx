/** Step 1 asks FOUR questions, and each one narrows the next.
 *
 * The order was ratified on 2026-08-10: the Connector first — it is the only
 * question answerable with no prior setup — then the authorization that opens
 * it, then the account that authorization serves, then the template or the
 * fields. It ran the other way (account, then Connector), and the inversion is
 * what made every empty state unreadable and every pairing possible: nothing
 * stopped a Search Console property being paired with a Meta contract, because
 * `connector_ref` on a Source Account carries the AUTHORIZATION's provider
 * ("google"), which names no tool.
 *
 * What this file holds, in the new order:
 *
 *   * the account option names the TOOL beside the scope — two scopes of one
 *     consent are told apart, and each is offered under the Connector it
 *     serves, never under another;
 *   * the Connector is chosen from CARDS that carry the generated brand mark,
 *     and choosing one writes its pin with it;
 *   * the account question only offers what serves the chosen Connector —
 *     the old pairing defect is structural now, not guarded;
 *   * a Connector the Project cannot open says so and hands off, instead of
 *     rendering as an empty list.
 *
 * ELLES VIVENT TOUTES DANS L'ÉTAPE `Source`, LA PREMIÈRE DES CINQ ratifiées
 * (`datastream-workbench-and-wizard.md:35-39`), et elles se découvrent l'une
 * après l'autre : le mode, puis les questions de ce mode seul (Connector,
 * autorisation, compte), puis — la source une fois observée — le nom, le rôle
 * et la catégorie, que `:183` place en dernier « in that order ». La question
 * 4, gabarits et famille de rapport, ouvre `Configure`, dont elle remplit les
 * champs. Aucun de ces révélations n'est un arrêt : `:31` refuse en toutes
 * lettres qu'une question de plus coûte une étape de plus.
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
    updateDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
    // 57.7: les gabarits enregistrés de l'organisation. Mockés pour que cette
    // suite n'exerce que le rétrécissement — le bloc est RENDU à `Configure`
    // depuis l'amendement du 2026-08-11, mais le wizard le lit dès l'ouverture.
    listDatastreamSetupTemplates: vi.fn(),
    // La marche avant passe désormais par la DÉCOUVERTE (la section `Source`
    // n'est complète qu'une fois une observation en main) et, pour le canal
    // file_upload, par le staging du fichier — les deux sont mockés ici.
    createDatastreamSetupObservation: vi.fn(),
    stageDatastreamSetupAsset: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

function connectorOption(ref: string, name: string): wizardApi.DatastreamSetupConnectorOption {
  return {
    connector_ref: ref, contract_version_ref: `ccv_${ref}`, contract_fingerprint: "a".repeat(64),
    display_name: name, observed_at: "2026-08-05T00:00:00Z",
    reports: [{ report_ref: `${ref}_daily`, display_name: `${name} daily`, availability: { status: "selectable" }, metrics: ["clicks"], dimensions: ["date"] }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date" }],
  };
}

/** One Google authorization, two scopes of it, exactly as preprod stores them:
 *  the provider is "google" and the tool is on `opens`, never on `connector_ref`.
 *  `connection_ref` is the authorization itself (57.11) — two consents in one
 *  Project arrive as two distinct ids. */
function account(
  id: string,
  label: string,
  opens: wizardApi.DatastreamSetupConnectorOpened[],
  connectionRef?: string,
): wizardApi.DatastreamSetupSourceAccount {
  return {
    object_ref: { id }, connector_ref: { id: "google" }, label, opens,
    states: { availability: "available", authorization: "ok" },
    ...(connectionRef ? { connection_ref: { object_type: "connection", id: connectionRef } } : {}),
  };
}

const GSC = { connector_name: "gsc", display_name: "Google Search Console", available: true };
const GA4 = { connector_name: "google-analytics", display_name: "Google Analytics 4", available: true };

const baseOptions: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [account("sacct_gsc", "sc-domain:toorow.test", [GSC])],
  connectors: [connectorOption("gsc", "Google Search Console")],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [],
  recommendations: [],
};

let lastSavedInput: Record<string, unknown> | undefined;

/** La plus petite observation qui complète la section `Source` (amendement du
 *  ratifié) : le primaire de l'étape ne s'active que sur
 *  une source OBSERVÉE. La découverte connector_pull ne envoie plus de
 *  `report_ref` — la famille de rapport est une décision de `Configure`. */
const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_1",
  draft_revision: 1, mode: "connector_pull", discovery_kind: "connector_contract",
  adapter_ref: "gsc.contract.v1", connector_contract_version_ref: "ccv_gsc",
  request_fingerprint: "b".repeat(64), evidence_fingerprint: "c".repeat(64), schema_hash: null,
  safe_metadata: { report_refs: ["gsc_daily"], field_ids: ["date", "clicks"], cadence: ["daily"], history: "90 days", quota_cost: "1 request" },
  coverage: { fields: "available" }, exceptions: [], observed_at: "2026-08-05T00:01:00Z",
  expires_at: null, idempotent_replay: false,
};

beforeEach(() => {
  lastSavedInput = undefined;
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(baseOptions);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockRejectedValue(new Error("not materialized"));
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({ templates: [], count: 0, limit: 10 });
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.stageDatastreamSetupAsset).mockResolvedValue({
    asset_ref: "dsa_1", draft_ref: "dsd_1", content_hash: "d".repeat(64), detected_format: "csv",
    byte_count: 11, state: "available", expires_at: "2026-08-12T00:00:00Z",
    cleanup_owner: "datastream_setup_asset_retention", idempotent_replay: false,
  });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    lastSavedInput = input;
    return { ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input };
  });
});

/** Le mode D'ABORD — il est sa propre section depuis l'amendement du
 *  — première question de `Source` — puis, dessous, la carte
 *  Connector. Les questions compte et rapport n'existent pas avant. */
/** Re-open the answered Connector question. Since the 2026-08-11 collapse the
 *  grid is replaced by the chosen card once the question is answered -- the
 *  thirty-eight declined products stop standing between a person and the
 *  questions their choice unlocked -- so changing it goes through `Change`,
 *  which is the operator's own gesture. */
async function chooseAnotherConnector(
  user: ReturnType<typeof userEvent.setup>, connector: string,
) {
  await user.click(await screen.findByRole("button", { name: "Change" }));
  await user.click(await screen.findByRole("radio", { name: connector }));
}

async function openConnectorPull(connector: string | null = null) {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  if (connector) await user.click(await screen.findByRole("radio", { name: connector }));
  return user;
}

/** De `Source` à `Identity` : la découverte est le guichet entre les deux
 *  (la section `Source` n'est complète qu'avec une observation), et le
 *  primaire ne s'active qu'ASYNCHRONIQUEMENT une fois l'observation rendue —
 *  un clic sur un bouton désactivé est un no-op silencieux, donc on attend. */
async function discoverAndOpenIdentity(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
}

/** `Identity` répondue, puis `Configure`. Le nom est SAISI, pas proposé :
 *  la famille de rapport qui alimentait la proposition est choisie une
 *  section plus loin, donc la marche avant ne
 *  rencontre plus la proposition automatique. */
async function answerIdentityAndContinue(
  user: ReturnType<typeof userEvent.setup>,
  name = "Daily search",
  role = "Spend",
) {
  await user.type(await screen.findByLabelText(/Datastream name/), name);
  await user.selectOptions(await screen.findByLabelText(/Data role/), role);
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
}

it("names the tool beside the scope, offered under the Connector it serves", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [
      account("sacct_gsc", "sc-domain:toorow.test", [GSC]),
      account("sacct_ga4", "properties/1", [GA4]),
    ],
    connectors: [connectorOption("gsc", "Google Search Console"), connectorOption("google-analytics", "Google Analytics 4")],
  });

  const user = await openConnectorPull("Google Search Console");

  // Under GSC, the GA4 scope is NOT offered — the pairing the old order
  // allowed cannot be asked for.
  expect(await screen.findByRole("option", { name: "sc-domain:toorow.test — Google Search Console" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /properties\/1/ })).not.toBeInTheDocument();

  // Under GA4 it is, with its own tool named.
  await chooseAnotherConnector(user, "Google Analytics 4");
  expect(await screen.findByRole("option", { name: "properties/1 — Google Analytics 4" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /sc-domain:toorow\.test/ })).not.toBeInTheDocument();
});

it("choosing the Connector card selects it, and its pin with it", async () => {
  const user = await openConnectorPull("Google Search Console");

  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    connector_ref: "gsc",
    connector_contract_version_ref: "ccv_gsc",
  })));

  // The questions it opens, in order: the account it is served by — puis la
  // famille de rapport de CE Connector, qui a déménagé en tête de `Configure`
  // : c'est elle qui remplit les champs de cette
  // section. On y marche — découverte, identité — pour lire ses options.
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_gsc");
  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    source_account_ref: "sacct_gsc",
    connector_ref: "gsc",
  })));
  await discoverAndOpenIdentity(user);
  await answerIdentityAndContinue(user);
  expect(await screen.findByRole("option", { name: "Google Search Console daily" })).toBeInTheDocument();
});

/** THE MARK IS THE REAL ASSET, or there is none.
 *
 *  The Connector field was a native `<select>`, and a native `<option>` cannot
 *  carry an image -- so the one field where an operator recognises a product at
 *  a glance was the only field of the console with no brand mark, while six
 *  other surfaces already rendered `ConnectorMark`. This asserts the file that
 *  `scripts/sync_connector_logos.py` generated actually reaches the card, per
 *  Connector: a placeholder that let a wrong product look right would pass a
 *  "there is an image" test and fail this one. */
it("every Connector card carries its own generated brand mark", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [account("sacct_both", "shared scope", [GSC, GA4])],
    connectors: [
      connectorOption("gsc", "Google Search Console"),
      connectorOption("google-analytics", "Google Analytics 4"),
    ],
  });

  const user = await openConnectorPull();

  const gsc = await screen.findByRole("radio", { name: "Google Search Console" });
  const ga4 = screen.getByRole("radio", { name: "Google Analytics 4" });
  // The exact paths in ui/admin/src/generated/connector-logos.json -- two
  // DIFFERENT files, so a single shared fallback cannot satisfy both.
  expect(gsc.querySelector("img")).toHaveAttribute("src", "/connectors/google-search-console.png");
  expect(ga4.querySelector("img")).toHaveAttribute("src", "/connectors/google-analytics.png");

  // And choosing one is a choice: the card carries the value into the draft.
  await user.click(gsc);
  await waitFor(() => expect(lastSavedInput?.source).toEqual(
    expect.objectContaining({ connector_ref: "gsc" }),
  ));
});

it("the account question offers only what serves the chosen Connector", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [
      account("sacct_both", "shared scope", [GSC, GA4]),
      account("sacct_ga4", "properties/1", [GA4]),
    ],
    connectors: [
      connectorOption("gsc", "Google Search Console"),
      connectorOption("google-analytics", "Google Analytics 4"),
      connectorOption("meta-ads", "Meta Ads"),
    ],
  });

  const user = await openConnectorPull();

  // THE CATALOGUE IS THE MODULE REGISTRY (AI-279): a Connector nobody can open
  // yet is still a card — choosing it is how the operator learns what is
  // missing, and the sentence names the real absence.
  await user.click(await screen.findByRole("radio", { name: "Meta Ads" }));
  expect(await screen.findByText("Meta Ads has no authorization in this Project")).toBeInTheDocument();

  // Under GSC only the scope that serves it is offered; under GA4, both.
  await chooseAnotherConnector(user, "Google Search Console");
  await screen.findByLabelText(/Source Account/);
  expect(screen.getByRole("option", { name: /shared scope/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /properties\/1/ })).not.toBeInTheDocument();

  await chooseAnotherConnector(user, "Google Analytics 4");
  await screen.findByLabelText(/Source Account/);
  expect(screen.getByRole("option", { name: /shared scope/ })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: /properties\/1/ })).toBeInTheDocument();
});

/** THE CLASS DEFECT 57.7 REPAIRS, kept green through the reorder.
 *
 *  Choosing a report family and THEN swapping the Source Account -- the
 *  ordinary gesture when one client account per market feeds the same report --
 *  used to erase the Connector, its contract and the report family. With the
 *  Connector asked first, the swap changes question 3's answer and nothing
 *  else: the two remaining answers were never the account's to take.
 *
 *  La famille de rapport se choisit à
 *  `Configure` — derrière la découverte et l'identité — et l'échange de
 *  compte revenant à `Source` demande confirmation, car une observation est
 *  en main (comportement pré-existant : l'échange invalide la preuve, mais
 *  ne touche ni au Connector ni à la famille choisie). */
it("swapping to another account that SERVES the chosen Connector keeps it and the report", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [
      account("sacct_first", "sc-domain:first.test", [GSC, GA4]),
      account("sacct_second", "sc-domain:second.test", [GSC, GA4]),
    ],
    connectors: [connectorOption("gsc", "Google Search Console"), connectorOption("google-analytics", "Google Analytics 4")],
  });

  const user = await openConnectorPull("Google Search Console");
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_first");
  await discoverAndOpenIdentity(user);
  await answerIdentityAndContinue(user);
  await user.selectOptions(await screen.findByRole("combobox", { name: /^Report family/ }), "gsc_daily");
  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({ report_ref: "gsc_daily" })));

  // Retour à `Source` par le rail (section complète, donc cliquable) : le
  // compte y vit toujours — c'est la question 3.
  await user.click(screen.getByRole("button", { name: /^Source/ }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_second");
  // Une observation existe : l'échange passe par la confirmation, acceptée.
  await user.click(await screen.findByTestId("datastream-setup-confirm-accept"));

  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    source_account_ref: "sacct_second",
    connector_ref: "gsc",
    connector_contract_version_ref: "ccv_gsc",
    report_ref: "gsc_daily",
  })));
});

/** TWO CONSENTS, ONE PROJECT — question 2 exists because of this fixture.
 *
 *  Changing the authorization takes the account with it when that account
 *  belonged to the previous one: an account is a scope OF a consent, and
 *  keeping it would leave the two answers naming different credentials. It is
 *  cleared, never swapped for whatever the new authorization exposes first. */
it("changing the authorization clears the account that belonged to the previous one", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [
      account("sacct_a", "sc-domain:a.test", [GSC], "conn_alpha"),
      account("sacct_b", "sc-domain:b.test", [GSC], "conn_beta"),
    ],
  });

  const user = await openConnectorPull("Google Search Console");

  // Two authorizations open this Connector, so question 2 is a real decision:
  // nothing is defaulted, and the account question waits on it.
  const authorization = await screen.findByLabelText(/Authorization/);
  expect((authorization as HTMLSelectElement).value).toBe("");

  await user.selectOptions(authorization, "conn_alpha");
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_a");
  await waitFor(() => expect(lastSavedInput?.source).toEqual(
    expect.objectContaining({ source_account_ref: "sacct_a" }),
  ));

  await user.selectOptions(screen.getByLabelText(/Authorization/), "conn_beta");

  await waitFor(() => expect(lastSavedInput?.source).toEqual(expect.objectContaining({
    source_account_ref: "",
    connector_ref: "gsc",
  })));
});

it("a missing module is named as missing, not rendered as an empty list", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [account("sacct_gsc", "sc-domain:toorow.test", [GSC])],
    connectors: [],
  });

  await openConnectorPull();

  // TWO DIFFERENT ABSENCES, both named. The catalogue is the module REGISTRY
  // (AI-279), so an empty one means no module is installed at all; and the
  // authorization opens a product this deployment cannot serve — a missing
  // MODULE, not a missing verification run, which is what the sentence used
  // to send the operator looking for.
  expect(await screen.findByText("No Connector in this deployment")).toBeInTheDocument();
  expect(screen.getByText("No Connector for what this Project authorizes")).toBeInTheDocument();
  expect(screen.getByText(/open Google Search Console/)).toBeInTheDocument();
});

/** THE ORDER ITSELF, MEASURED ON THE DOM.
 *
 *  Everything above asserts what each question OFFERS. None of it would fail if
 *  the four were rendered in any sequence — and the sequence IS the amendment.
 *  These read the labels off a section in document order, so an inversion is
 *  a red line and not a matter of reading the file.
 *
 *  L'ordre se lit SECTION PAR SECTION : une
 *  seule est visible à la fois, donc chaque mesure nomme sa section, et
 *  l'ordre ENTRE sections est épinglé par la marche elle-même — on n'atteint
 *  `Identity` qu'après `Source` observée, `Configure` qu'après `Identity`
 *  répondue.
 */
function questionOrder(sectionLabel: string): string[] {
  const section = screen.getByLabelText(sectionLabel);
  return Array.from(section.querySelectorAll("label"))
    .map((node) => (node.textContent ?? "").replace(/\*$/, "").trim())
    .filter(Boolean);
}
/** Where a block sits among those questions: a panel title is an `h2`, a
 *  question is a `label`, and the amendment orders the two together. */
function positionOf(text: string, sectionLabel: string): number {
  const section = screen.getByLabelText(sectionLabel);
  const nodes = Array.from(section.querySelectorAll("label, h2"));
  return nodes.findIndex((node) => (node.textContent ?? "").includes(text));
}

async function openMode(name: string) {
  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name }));
  // Le mode est la première question de `Source` : les
  // questions de la source se lisent une section plus loin.
  return user;
}

it("asks the four questions of a Connector pull in the ratified order", async () => {
  // ONE SAVED CONFIGURATION, because a template block with nothing to show
  // renders nothing — and the rank of an absent block is not measurable.
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({
    templates: [{
      template_ref: "dst_1", label: "Daily clicks", origin_project_ref: "proj_1",
      mode: "connector_pull", connector_ref: "gsc", report_ref: "gsc_daily",
      operator_input: {}, open_variables: ["source_account_ref"],
    }],
    count: 1,
    limit: 10,
  });

  const user = userEvent.setup();
  render(<DatastreamPreconfiguration projectId="proj_1" />);

  // THE MODE OPENS THE STEP, and until it is answered the step IS that one
  // question — the three modes do not ask the same things, so a source form
  // drawn before the mode would be all three of them at once, most of it
  // switched off.
  const modeCard = await screen.findByRole("radio", { name: "Connector pull" });
  expect(questionOrder("Source section")).toEqual(["Mode"]);
  await user.click(modeCard);
  // ANSWERED, SO THE NEXT ONE APPEARS — and only it.
  expect(questionOrder("Source section")).toEqual(["Mode", "Connector"]);
  await user.click(await screen.findByRole("radio", { name: "Google Search Console" }));

  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_gsc");
  expect(questionOrder("Source section")).toEqual([
    "Mode",
    "Connector",
    "Authorization",
    "Source Account",
  ]);

  // The name, the role and the category CLOSE the same step: `:183` makes them
  // the last Required items of `1. Source`, "in that order". No stop, no rail
  // entry and no click stands between them and the questions above.
  await discoverAndOpenIdentity(user);
  expect(questionOrder("Source section")).toEqual([
    "Mode",
    "Connector",
    "Authorization",
    "Source Account",
    "Datastream name",
    "Data role",
    "Source category",
  ]);
  await answerIdentityAndContinue(user);

  // Question 4, en tête de `Configure` depuis l'amendement : les gabarits de
  // l'organisation PUIS la famille de rapport — les deux remplissent les
  // champs de cette section, donc ils l'ouvrent. L'épinglage « les gabarits
  // APRÈS la question du compte » tient par l'ordre des sections elles-mêmes
  // (`Source` avant `Configure`) ; ici on épingle leur rang DANS `Configure`.
  expect(await screen.findByLabelText("Configure section")).toBeInTheDocument();
  expect(positionOf("Organization templates", "Configure section")).toBeGreaterThan(-1);
  expect(positionOf("Report family", "Configure section")).toBeGreaterThan(-1);
  expect(positionOf("Organization templates", "Configure section"))
    .toBeLessThan(positionOf("Report family", "Configure section"));
});

/** EACH MODE FOLLOWS ITS OWN ORDER, and the fields of another mode are ABSENT
 *  rather than hidden. A control switched off with a class is still in the
 *  accessible tree, still tabbable and still read aloud — so "this mode carries
 *  no Connector" would be contradicted by the DOM it renders. */
it("External BigQuery asks its own four, and carries no Connector question at all", async () => {
  // One exposed access, so the walk can answer each question in turn: the
  // object, the writer and the acknowledgement are only drawn once what they
  // depend on is answered (57.12, passe UX).
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    external_access: [{
      object_ref: { id: "sacct_bq" }, connector_ref: { id: "bigquery" },
      label: "warehouse access", states: { availability: "available" },
    }],
  });
  const user = await openMode("External BigQuery");

  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "sacct_bq");
  await user.type(await screen.findByLabelText(/Table or view reference/), "proj.dataset.table");
  await user.type(await screen.findByLabelText(/Declared writer/), "acme-etl");

  expect(questionOrder("Source section")).toEqual([
    "Mode",
    "BigQuery access",
    "Table or view reference",
    "Declared writer",
    // THE CONSEQUENCE OF THE WRITER, asked after it and never before: the
    // acknowledgement only means something once someone else is authoritative.
    "I acknowledge read-only access",
  ]);
  expect(questionOrder("Source section")).not.toContain("Connector");
  expect(screen.queryByRole("combobox", { name: /Authorization/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: /Source Account/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: /Report family/ })).not.toBeInTheDocument();

  // The name, the role and the category close this mode's step too, and the
  // CATEGORY SAYS ITS ABSENCE instead of leaving a blank: this mode carries no
  // module manifest, so there is nothing to read.
  await user.click(screen.getByRole("checkbox", { name: /read-only/i }));
  await discoverAndOpenIdentity(user);
  expect(questionOrder("Source section")).toEqual([
    "Mode",
    "BigQuery access",
    "Table or view reference",
    "Declared writer",
    "I acknowledge read-only access",
    "Datastream name",
    "Data role",
    "Source category",
  ]);
  expect(screen.getByLabelText("Source category")).toHaveAttribute(
    "placeholder", "No connector in this mode, so no category is read",
  );
});

it("Managed feed asks the channel, and carries no Connector, authorization or account", async () => {
  const user = await openMode("Managed feed");

  await screen.findByLabelText(/Channel/);
  // The default channel is `file_upload`, whose only other question is the file.
  expect(questionOrder("Source section")).toEqual([
    "Mode", "Channel", "CSV, Excel or SAV file",
  ]);
  expect(questionOrder("Source section")).not.toContain("Connector");
  expect(screen.queryByRole("combobox", { name: /Authorization/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: /Source Account/ })).not.toBeInTheDocument();

  // The name and the role wait for an OBSERVED source — a file must be staged
  // before a `file_upload` feed can be discovered at all. The walk does that.
  const file = new File(["date,clicks\n"], "daily.csv", { type: "text/csv" });
  await user.upload(await screen.findByLabelText(/CSV, Excel or SAV file/), file);
  await screen.findByText(/held in draft quarantine/);
  await discoverAndOpenIdentity(user);
  expect(questionOrder("Source section")).toEqual([
    "Mode",
    "Channel",
    "CSV, Excel or SAV file",
    "Datastream name",
    "Data role",
    "Source category",
  ]);
  expect(screen.getByLabelText("Source category")).toHaveAttribute(
    "placeholder", "No connector in this mode, so no category is read",
  );
});

it("Connector pull carries no field of the other two modes", async () => {
  await openConnectorPull("Google Search Console");

  await screen.findByLabelText(/Source Account/);
  expect(questionOrder("Source section")).not.toContain("BigQuery access");
  expect(questionOrder("Source section")).not.toContain("Declared writer");
  expect(questionOrder("Source section")).not.toContain("Channel");
  expect(screen.queryByRole("checkbox", { name: /read-only access/ })).not.toBeInTheDocument();
});

/** AND THE ONE CHANNEL THAT NEEDS A CONSENT ASKS FOR THE CONSENT.
 *
 *  `google_sheets` reads a private workbook, so it needs a Google grant — and it
 *  asked for a `Source Account`, which named a product where the source is a
 *  channel. Measured: the server resolves any scope of a consent to the same
 *  `cr.id`, so the scope was never an answer. The channel asks the grant; no
 *  account question exists on this path, in any channel. */
it("the Google Sheets channel asks for the grant, and for no account", async () => {
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...baseOptions,
    source_accounts: [account("sacct_sheet", "Media plan workbook", [
      { connector_name: "google-sheets", display_name: "Google Sheets", available: true },
    ], "conn_sheets")],
    managed_channels: [
      { channel: "file_upload", availability: "available", template_ref: null },
      { channel: "google_sheets", availability: "available", template_ref: null },
    ],
  });

  const user = await openMode("Managed feed");
  await user.selectOptions(await screen.findByLabelText(/Channel/), "google_sheets");

  expect(await screen.findByLabelText(/Google authorization/)).toBeInTheDocument();
  expect(questionOrder("Source section")).toEqual([
    "Mode", "Channel", "Google authorization", "Sheet reference",
  ]);
  expect(questionOrder("Source section")).not.toContain("Source Account");
  expect(questionOrder("Source section")).not.toContain("Connector");

  // Discovering this channel needs the named workbook — it is typed, and then
  // the last two questions of the step come up on their own.
  await user.type(await screen.findByLabelText(/Sheet reference/), "Sheet1!A1");
  await discoverAndOpenIdentity(user);
  expect(questionOrder("Source section")).toEqual([
    // `Tab` IS ITSELF A NARROWED QUESTION: the workbook has to be read before
    // its tabs can be offered, so it appears with the discovery and never
    // before it.
    "Mode", "Channel", "Google authorization", "Sheet reference", "Tab",
    "Datastream name", "Data role", "Source category",
  ]);
});
