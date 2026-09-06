import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ProjectSettings from "../shell/pages/ProjectSettings";

const CAPABILITIES = [
  "country", "currency_fx", "reporting_timezone", "tax_fees", "competitors", "placement_mapping",
];

// Story 48.1: the server emits SEMANTIC owner references, never browser paths.
// The console resolves them through the canonical navigation registry, so a
// route Epic 49 renames does not leave a dead link frozen in a pinned version.
function ownerRef(workspace: string, section: string) {
  return {
    surface: "project",
    workspace,
    section,
    global_surface: null,
    global_section: null,
    object_type: null,
    object_id: null,
    tab: null,
    action: null,
    version_id: null,
    evidence_id: null,
  };
}
const envelope = {
  project: {
    id: "p1",
    name: "Acme",
    description: "Governed analytics",
    organization: { id: "o1", name: "Acme Group" },
    can_edit: true,
    can_manage: true,
    business_domains: [{ id: "bd1", name: "Marketing" }],
    // `proactive-assertions.md` decision 2: the project-scoped capability that
    // decides whether anything may leave the platform. The DEFAULT is
    // `forbidden`, and this fixture carries the default rather than the
    // convenient value.
    external_sharing: {
      state: "forbidden",
      decided_by: null,
      decided_at: null,
      is_platform_default: true,
      can_change: true,
    },
    defaults: {
      reporting_currency: { active: null, pending: "EUR", origin: "suggestion", confirmation_status: "unconfirmed", owner_reference: ownerRef("governance", "semantic-model") },
      reporting_timezone: { active: "Europe/Paris", pending: null, origin: "project_change_set", confirmation_status: "confirmed", owner_reference: ownerRef("governance", "controls-quality") },
      verification_source: { active: null, pending: null, origin: "unset", confirmation_status: "unconfirmed", owner_reference: ownerRef("governance", "controls-quality") },
    },
  },
  capabilities: CAPABILITIES.map((key, index) => ({
    key,
    availability: index === 1 || index === 2 ? "always_present" : "optional",
    dependencies: key === "tax_fees" || key === "placement_mapping" ? ["currency_fx"] : [],
    active: { state: index === 1 || index === 2 ? "draft" : "disabled", version_id: null },
    pending: null,
    coverage: { applicable: 0, complete: 0, partial: 0, unavailable: 0, excluded: 0, pending: 0, label: "Not applicable", percentage: null },
    exceptions: [],
    blockers: [],
    owner_links: [
      {
        owner: "Governance",
        owner_reference: key === "country"
          ? { ...ownerRef("governance", "master-data"), object_type: "registry", object_id: "mdr_country_1", tab: "versions", version_id: "mdv_country_2" }
          : ownerRef("governance", "master-data"),
      },
      { owner: "Data", owner_reference: ownerRef("data", "datastreams") },
    ],
  })),
  changes: [],
};

afterEach(() => vi.restoreAllMocks());

function response(body: unknown, status = 200) {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) });
}

it("loads the aggregate Settings model and never calls legacy geography routes", async () => {
  const fetchMock = vi.fn((_url: string) => response(envelope));
  vi.stubGlobal("fetch", fetchMock);
  const onOpenOwner = vi.fn();
  render(<ProjectSettings projectId="p1" section="capabilities" onOpenOwner={onOpenOwner} />);

  expect(await screen.findByRole("heading", { name: "Project capabilities" })).toBeInTheDocument();
  for (const key of CAPABILITIES) expect(screen.getByTestId(`capability-${key}`)).toBeInTheDocument();
  expect(screen.getAllByText("Not applicable")).toHaveLength(6);
  expect(screen.getByTestId("capability-country")).toHaveTextContent("Not active");
  expect(screen.getByTestId("capability-country")).toHaveTextContent(
    "Every applicable compatible Datastream is compiled automatically",
  );
  expect(screen.queryByText(/assignment matrix/i)).not.toBeInTheDocument();
  // The owner is opened by resolving a semantic reference, not by following a
  // path this screen invented.
  fireEvent.click(screen.getAllByRole("button", { name: "Open Governance" })[0]);
  expect(onOpenOwner).toHaveBeenCalledWith(
    expect.objectContaining({ surface: "project", workspace: "governance", section: "master-data", object_type: "registry", object_id: "mdr_country_1", tab: "versions", version_id: "mdv_country_2" }),
  );
  const urls = fetchMock.mock.calls.map(([url]) => String(url));
  expect(urls).toContain("/api/projects/p1/settings");
  expect(urls.some((url) => url.includes("geography") || url.includes("vocabularies"))).toBe(false);
});

/**
 * The header of the Capabilities panel used to spell the number of capabilities
 * out, and kept spelling the old one above SIX cards for the whole of story 61.5.
 * A count written into a screen is a second registry: it cannot be wrong for a
 * test that never reads it, and it is wrong for a person on the day the server
 * sends one more row. So the sentence names no number, and this test refuses any.
 */
it("never writes a capability count into the panel that draws them", async () => {
  vi.stubGlobal("fetch", vi.fn(() => response(envelope)));
  render(<ProjectSettings projectId="p1" section="capabilities" onOpenOwner={vi.fn()} />);

  const heading = await screen.findByRole("heading", { name: "Project capabilities" });
  const header = heading.closest("div")!;
  expect(header.textContent).toMatch(/governed capabilities/);
  for (const written of ["five", "Five", "six", "Six", "5", "6"]) {
    expect(header.textContent).not.toContain(written);
  }
  // The rows come from the envelope, and there are as many as it carries.
  expect(screen.getAllByTestId(/^capability-/)).toHaveLength(envelope.capabilities.length);
});

/**
 * Story 61.5. The sixth card is not a special case: it is optional, it is
 * disabled, it depends on Currency & FX, and it must reach the screen through
 * the same envelope as the other five — with its NAME, never its key.
 */
it("gives the sixth capability a card, its label and its dependency", async () => {
  vi.stubGlobal("fetch", vi.fn(() => response(envelope)));
  render(<ProjectSettings projectId="p1" section="capabilities" onOpenOwner={vi.fn()} />);

  const card = await screen.findByTestId("capability-placement_mapping");
  expect(card).toHaveTextContent("Placement Mapping");
  // `?? capability.key` is the fallback in this screen: without a label entry the
  // card would print `placement_mapping` at a person.
  expect(card.textContent).not.toContain("placement_mapping");
  // The ratified dependency, rendered by name too: "Currency & FX is required
  // for any planned-versus-actual figure" (`project-settings.md`).
  expect(card).toHaveTextContent("Depends on Currency & FX");
  // Optional and off: the activation door is the Change Set button, exactly as
  // for the other three optional capabilities.
  expect(screen.getByRole("button", { name: "Enable Placement Mapping" })).toBeInTheDocument();
});

it("shows suggestions as pending and never as confirmed active defaults", async () => {
  vi.stubGlobal("fetch", vi.fn(() => response(envelope)));
  const user = userEvent.setup();
  const onSectionChange = vi.fn();
  render(
    <ProjectSettings
      projectId="p1"
      section="general"
      onSectionChange={onSectionChange}
    />,
  );

  expect(await screen.findByText("Suggested: EUR")).toBeInTheDocument();
  expect(screen.getAllByText("Unconfirmed")).toHaveLength(2);
  expect(screen.getByText("Active: Europe/Paris")).toBeInTheDocument();
  expect(screen.getByRole("tablist")).toHaveAttribute("data-slot", "tabs-list");
  await user.click(screen.getByRole("tab", { name: "Capabilities" }));
  expect(onSectionChange).toHaveBeenCalledWith("capabilities");
});

it("prepares a capability change through the generic Change Set endpoints", async () => {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST" && String(url).endsWith("/change-sets")) return response({ id: "pcset_1", state: "draft" }, 201);
    if (init?.method === "POST" && String(url).endsWith("/pcset_1/prepare")) return response({ prepared_payload_hash: "a".repeat(64), blockers: [] });
    return response(envelope);
  });
  vi.stubGlobal("fetch", fetchMock);
  const onSectionChange = vi.fn();
  render(<ProjectSettings projectId="p1" section="capabilities" onSectionChange={onSectionChange} />);

  fireEvent.click(await screen.findByRole("button", { name: "Activate Country" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/change-sets/pcset_1/prepare"))).toBe(true));
  expect(screen.getByText("Change prepared for review.")).toBeInTheDocument();
  expect(onSectionChange).toHaveBeenCalledWith("changes");
});

/**
 * Two versions exist and this card only ever knew one of them (Story 41.7, AC8).
 *
 * `capability.active.version_id` is the PROJECT CONFIGURATION VERSION. The
 * version that decides what a capability actually does lives in its Governance
 * rule set, and `GET /api/projects/{id}/settings` does not carry it. Labelling
 * the first as "Version" let a reader conclude they had read the second.
 *
 * For Tax & Fees the gap is not cosmetic: migration 148 makes the ladder
 * effective only when FOUR conditions hold, and this envelope proves at most
 * two. So the card names what it cannot see rather than showing a green state it
 * has not measured.
 */
it("never lets an enabled capability read as an effective one", async () => {
  const enabledTaxFees = {
    ...envelope,
    capabilities: envelope.capabilities.map((capability) =>
      capability.key === "tax_fees"
        ? { ...capability, active: { state: "enabled", version_id: "pcv_EXAMPLE" } }
        : capability,
    ),
  };
  vi.stubGlobal("fetch", vi.fn(() => response(enabledTaxFees)));
  render(<ProjectSettings projectId="p1" section="capabilities" onSectionChange={vi.fn()} />);

  // The configuration version is named as such, and never as "the version".
  expect(
    await screen.findByText(/Active: enabled · Project configuration version: pcv_EXAMPLE/),
  ).toBeInTheDocument();
  expect(screen.getByText(/Enabled here is not the same as effective/)).toBeInTheDocument();
  expect(
    screen.getByText(/a published Tax & Fee Rule Set version and a published Money Policy version/),
  ).toBeInTheDocument();

  // AC8's other half: no second activation door. The Change Set button is the
  // only control, and no toggle is introduced beside it.
  expect(screen.getByRole("button", { name: "Disable Tax & Fees" })).toBeInTheDocument();
  expect(screen.queryByRole("switch")).toBeNull();
});

// ---------------------------------------------------------------------------
// AI-294 -- la recette de tache, la ou la personne planifie la tache.
// ---------------------------------------------------------------------------
//
// L'API la servait depuis 35.5 et AUCUN ecran ne l'appelait : six routes
// `daily-insights`, zero consommateur. L'insight quotidien tourne depuis l'hote
// LLM de l'operateur, sur une horloge que toorow ne pose ni ne voit -- donc ce
// que toorow doit, ce n'est pas un planificateur, c'est le TEXTE exact a coller
// dans le sien.
//
// L'arbitrage de Jean du 2026-08-16 disait : rattacher a l'existant, aucun
// septieme endroit. C'est pourquoi cela vit dans General et non dans un ecran
// neuf.

const RECIPE = {
  recipe: { recipeVersion: "task-recipe.v1" },
  text: "Every day at 07:00 Europe/Paris, call get_daily_report then...",
};

it("serves the scheduled-task recipe in General, derived and never stored", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      String(url).includes("/api/daily-insights/recipe")
        ? response(RECIPE)
        : response(envelope),
    ),
  );
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  expect(
    await screen.findByRole("heading", { name: /scheduled task/i }),
  ).toBeInTheDocument();
  expect(await screen.findByText(RECIPE.text)).toBeInTheDocument();
  // La version est dite : un ancien copier-coller continue de tourner contre
  // l'ancien contrat, et rien d'autre sur cet ecran ne le signalerait.
  expect(await screen.findByText(/task-recipe\.v1/)).toBeInTheDocument();
  // Ce qui quitte le produit est dit aussi -- une personne qui s'apprete a
  // coller un prompt dans son propre hote a le droit de le savoir.
  expect(screen.getByText(/carries no secret/i)).toBeInTheDocument();
});

it("says a recipe that could not be built, instead of showing an empty one", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      String(url).includes("/api/daily-insights/recipe")
        ? response({ code: "db_error", message: "Erreur base de donnees" }, 500)
        : response(envelope),
    ),
  );
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  // Un panneau vide se lirait comme « rien a planifier ». Coller rien dans un
  // hote ne planifie rien, et personne ne s'en apercevrait avant longtemps.
  expect(await screen.findByText(/Erreur base de donnees/i)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// AI-294 -- l'historique des runs, et les CINQ etats qui ne se collapsent pas.
// ---------------------------------------------------------------------------
//
// `execution-substrate.md` fixe le vocabulaire et dit pourquoi il y en a cinq et
// non deux : published, no_insight (rien ne valait la peine d'etre dit), blocked
// (la donnee n'etait pas prete), failed, et une ligne ABSENTE -- la tache n'a pas
// tourne du tout. Collapser une paire quelconque EST le defaut.
//
// La frontiere que ce panneau ne doit jamais franchir : toorow enregistre ce
// qu'on lui a dit et ce qu'il a mesure, il ne rapporte JAMAIS comme ayant eu lieu
// un run qu'il n'a pas observe. Une ligne manquante est une absence a AFFICHER,
// pas une inference a faire.

const RUNS = {
  runs: [
    { state: "published", insightDate: "2026-08-16", itemCount: 3 },
    { state: "no_insight", insightDate: "2026-08-15", itemCount: 0 },
    { state: "blocked", insightDate: "2026-08-14", itemCount: 0 },
    { state: "failed", insightDate: "2026-08-13", itemCount: 0 },
    { state: "absent", detail: "no run recorded" },
  ],
};

function stubDailyInsights(runsResponse: unknown, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const u = String(url);
      if (u.includes("/api/daily-insights/recipe")) return response(RECIPE);
      if (u.includes("/api/daily-insights/runs")) return response(runsResponse, status);
      return response(envelope);
    }),
  );
}

it("shows each run state distinctly, and never collapses two of them", async () => {
  stubDailyInsights(RUNS);
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const panel = await screen.findByTestId("daily-insight-runs");
  // THE SENTENCES, NOT THE STORED WORDS (76-2 review). This loop asserted the
  // wire tokens, which is what the screen printed until its private tone map
  // was deleted; `stateLabel` spells them now, and §4 forbids a stored token
  // reaching a cell at all.
  for (const state of ["Published", "Nothing worth saying", "Blocked", "Failed", "Nothing reported"]) {
    expect(panel).toHaveTextContent(state);
  }
  // `no_insight` est une journee SAINE : la dire autrement apprendrait au lecteur
  // a ignorer la couleur.
  expect(panel).toHaveTextContent(/healthy day/i);
  // Et l'absence dit ce qu'elle est, pas une journee sans insight.
  expect(panel).toHaveTextContent(/NOT a day without insights/i);
});

it("an unreadable journal is not an empty one", async () => {
  stubDailyInsights({ code: "db_error", message: "Erreur base de donnees" }, 500);
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const panel = await screen.findByTestId("daily-insight-runs");
  // Afficher une liste vide dirait « aucune tache n'a jamais tourne » -- une
  // affirmation sur l'hote de l'operateur que cet echec ne soutient pas.
  await waitFor(() => expect(panel).toHaveTextContent(/Erreur base de donnees/i));
  expect(panel).not.toHaveTextContent(/No run has been recorded/i);
});

it("no run yet names the gesture that fills the list", async () => {
  stubDailyInsights({ runs: [] });
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const panel = await screen.findByTestId("daily-insight-runs");
  // Le panneau existe des le premier rendu (« Reading the journal… ») ; l'etat
  // vide n'arrive qu'avec la reponse. Mesure 2026-09-05 : sous une suite de 242
  // fichiers, l'assertion immediate lisait encore le chargement (2 runs sur 2),
  // seule 13/13. Meme attente que le test voisin de l'erreur.
  await waitFor(() => expect(panel).toHaveTextContent(/Copy the recipe above/i));
});

// ---------------------------------------------------------------------------
// LAQUELLE, pas COMBIEN.
// ---------------------------------------------------------------------------
//
// La carte montrait `coverage.applicable` -- un NOMBRE -- alors que le serveur
// sert la LISTE a `/capabilities/{key}/datastreams` depuis toujours. Un compte
// dit qu'il reste quelque chose a couvrir ; seule la liste dit QUOI, et personne
// ne peut agir sur un nombre.

const CAPABILITY_DATASTREAMS = {
  datastreams: [
    { datastream_id: "ds_1", datastream_name: "Google Ads daily", coverage_state: "complete", applicability: "applicable" },
    { datastream_id: "ds_2", datastream_name: "Meta Ads daily", coverage_state: "partial", applicability: "applicable" },
  ],
};

it("names WHICH Datastreams a capability covers, on demand", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      String(url).includes("/datastreams")
        ? response(CAPABILITY_DATASTREAMS)
        : response(envelope),
    ),
  );
  render(<ProjectSettings projectId="p1" section="capabilities" onOpenOwner={vi.fn()} />);

  // A la demande : cinq capacites sur un ecran feraient cinq lectures au montage
  // pour une liste que la plupart des visites n'ouvrent jamais.
  const buttons = await screen.findAllByRole("button", { name: /Show which Datastreams/i });
  fireEvent.click(buttons[0]);

  expect(await screen.findByText("Google Ads daily")).toBeInTheDocument();
  expect(screen.getByText("Meta Ads daily")).toBeInTheDocument();
});

it("a list that failed to load is not an empty one", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      String(url).includes("/datastreams")
        ? response({ code: "db_error", message: "Erreur base de donnees" }, 500)
        : response(envelope),
    ),
  );
  render(<ProjectSettings projectId="p1" section="capabilities" onOpenOwner={vi.fn()} />);

  const buttons = await screen.findAllByRole("button", { name: /Show which Datastreams/i });
  fireEvent.click(buttons[0]);

  // Ne rien rendre dirait « cette capacite ne couvre aucun Datastream » -- une
  // affirmation sur le Projet que la lecture ratee ne soutient pas.
  expect(await screen.findByText(/Erreur base de donnees/i)).toBeInTheDocument();
});
