/** Story 57.4 — what a capability row says, and the one gesture it offers.
 *
 *  Three things this file exists to keep true, and each was false before it:
 *
 *  1. the five rows carried ONE constant sentence — "May change compatible
 *     Datastream fields, grain, processing and Outputs." — which is a placeholder
 *     shaped like an answer;
 *  2. the only permitted gesture was dead: the server composed
 *     `/projects/{id}/settings/capabilities`, and `parsePath` refuses every path
 *     whose first segment is not `account`, `platform` or `org`;
 *  3. nothing forbade an activation call from leaving this screen. Activating a
 *     capability belongs to `POST /api/projects/{id}/settings/change-sets`, behind
 *     its own confirmation, in Project Settings.
 *
 *  The owner reference is resolved here through the REAL router (`buildPath` /
 *  `parsePath`), because "the button navigates" is only true if the address it
 *  produces is one the shell can parse.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";
import { buildPath, parsePath, type CanonicalRoute } from "../shell/router";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    createDatastreamSetupObservation: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    compileDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
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
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    source_category: "paid_media", source_category_origin: "connector_manifest",
    reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"], supported_grains: [["date"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "spend", kind: "metric", description: "Spend" }],
  }],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [],
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

const OWNER_REFERENCE: wizardApi.ProposalOwnerReference = {
  surface: "global", workspace: null, section: null,
  global_surface: "project-settings", global_section: "capabilities",
  object_type: null, object_id: null, tab: null, action: null, version_id: null, evidence_id: null,
};

const evidenceRef = (objectId: string): wizardApi.ProposalEvidenceRef => ({
  kind: "project_setting", object_type: "project_capability", object_id: objectId,
  version_id: "pcs_2", fingerprint: "d".repeat(64), observed_at: "2026-08-05T09:00:00Z",
});

/** One capability row exactly as `compile_preconfiguration` emits it. */
function capabilityItem(
  key: string,
  name: string,
  label: string,
  effect: string | undefined,
  effectCoverage: string,
): wizardApi.PreconfigurationProposalItem {
  const active = label === "Existing";
  return {
    key: `capability.${key}`, section: "capabilities", requirement: "optional",
    status: active ? "complete" : "warning",
    proposed_value: { key, name, label, state: active ? "ready" : "draft", effect, effect_coverage: effectCoverage },
    evidence_refs: [evidenceRef(key)],
    confidence: { level: "medium", rationale: "Supported by the referenced normalized evidence." },
    coverage: { state: active ? "covered" : "pending" },
    exceptions: [], blockers: [], warnings: active ? [] : [{ cause: "capability_not_active" }],
    owner_links: [{ object: "Project Settings", label: active ? "Open Project Settings" : "Propose", owner_reference: OWNER_REFERENCE }],
    downstream_impact: [], dependency_fingerprint: "e".repeat(64),
  };
}

/** The five sentences the server derives, kept DISTINCT here on purpose: a fixture
 *  that repeated one of them could not fail on the defect this story closes. */
const CAPABILITIES: wizardApi.PreconfigurationProposalItem[] = [
  capabilityItem("competitors", "Competitors", "Proposed in Project Settings", "Not applicable: this connector declares no tracked-entity contract, so Competitors would add nothing to this Datastream.", "not_applicable"),
  capabilityItem("country", "Country", "Proposed in Project Settings", "Adds country to the grain: date -> country, date. Read from the connector contract only; the governed markets are settled in Project Settings.", "covered"),
  capabilityItem("currency_fx", "Currency & FX", "Existing", "1 mapped field(s) carry a monetary role (spend): each publishes its native currency, unit and adapter.", "partial"),
  // The row that carries NO sentence: the screen must state the absence itself.
  capabilityItem("reporting_timezone", "Reporting timezone", "Proposed in Project Settings", undefined, "unavailable"),
  capabilityItem("tax_fees", "Tax & fees", "Proposed in Project Settings", "This Datastream resolves to PAID_MEDIA (source category paid_media, data role Spend) and enters the cost cascade.", "partial"),
];

/** The other two owner links this proposal draws — `downstream_candidates`. They were
 *  `<a href="/projects/{id}/governance/semantic-model">` and its Reports twin: the same
 *  dead composition as the capability rows, on the same card, two sections away. */
const DOWNSTREAM: wizardApi.PreconfigurationProposalSection = {
  key: "downstream_candidates", status: "warning", dependency_fingerprint: "e".repeat(64),
  items: [{
    key: "downstream.candidates", section: "downstream_candidates", requirement: "optional", status: "warning",
    proposed_value: { label: "Will remain a proposal", objects: ["Semantic View", "Report"] },
    evidence_refs: [evidenceRef("downstream")],
    confidence: { level: "medium", rationale: "Supported by the referenced normalized evidence." },
    coverage: { state: "pending" }, exceptions: [], blockers: [], warnings: [],
    owner_links: [
      { object: "Semantic View", owner_reference: { ...OWNER_REFERENCE, surface: "project", workspace: "governance", section: "semantic-model", global_surface: null, global_section: null } },
      { object: "Report", owner_reference: { ...OWNER_REFERENCE, surface: "project", workspace: "analyze", section: "reports", global_surface: null, global_section: null } },
    ],
    downstream_impact: ["Downstream owners must review and publish their own versions."],
    dependency_fingerprint: "e".repeat(64),
  }],
};

function proposalWith(items: wizardApi.PreconfigurationProposalItem[] | null): wizardApi.DatastreamPreconfigurationProposal {
  return {
    schema_version: "1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", proposal_ref: "dspp_1",
    dependency_fingerprint: "d".repeat(64), proposal_token: "e".repeat(64), content_hash: "f".repeat(64),
    is_stale: false, invalidation_causes: [], idempotent_replay: false, resume_href: "/resume",
    confirmed_intent_bundle: { joint_grain: ["date"], field_mappings: [{ source_identity: "date", role: "primary_date", semantic_type: "date", canonical_target: "date", aggregation: "none", sensitivity: "public", included: true }] },
    configuration_summary: { existing: [], will_be_created: [], will_remain_a_proposal: [], downstream_impact: [] },
    sections: items === null ? [] : [{ key: "capabilities", status: "warning", dependency_fingerprint: "e".repeat(64), items }],
  };
}

beforeEach(() => {
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposalWith(CAPABILITIES));
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1;
    return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input };
  });
});

/** Walks Source -> Configure -> Classify and map, where the
 *  proposal is reviewed.
 *
 *  LE PARCOURS RATIFIÉ : cinq arrêts, et le mode, le nom et le rôle sont
 *  - le mode se choisit SEUL (`Mode`), avant la source ;
 *  - la source s'observe SANS famille de rapport — `Report family` a déménagé en
 *    tête de `Configure`, et la découverte n'envoie plus de `report_ref` ;
 *  - le nom et le rôle ferment l'étape `Source` : le nom n'est plus
 *    proposé automatiquement dans le parcours avant (la famille qui alimentait
 *    la proposition est choisie plus loin), on le saisit donc au clavier ;
 *  - chaque navigation s'appuie sur le primaire contextuel du pied de section,
 *    des questions du premier — le primaire ne s'active qu'une fois
 *    d'où le `waitFor` avant le clic. */
async function reachClassifyAndMap(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Daily spend");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  await user.selectOptions(await screen.findByLabelText(/Report family/), "daily");
  await user.selectOptions(await screen.findByLabelText(/Date field/), "date");
  await user.click(screen.getByRole("checkbox", { name: "Spend (metric)" }));
  await user.click(screen.getByRole("checkbox", { name: "Reporting date (dimension)" }));
  await user.click(screen.getByRole("button", { name: "Compile proposal" }));
  await user.click(await screen.findByRole("button", { name: "Continue to classify and map" }));
}

/** Resolve an owner reference through the REAL router, exactly as
 *  `ContentRouter.openOwner` does. This is the whole point: a reference is only better
 *  than a composed path if the shell turns it into an address it can parse back. */
function resolve(owner: wizardApi.ProposalOwnerReference): string {
  return buildPath({
    scope: "project", organizationId: "org_1", projectId: "proj_1",
    globalSurface: owner.global_surface, globalSection: owner.global_section,
    workspace: owner.workspace, section: owner.section,
    lens: null, objectType: null, objectId: null, tab: null, versionId: null,
    evidenceId: null, action: owner.action, query: {},
  } as unknown as CanonicalRoute);
}

/** The card of one capability: `<strong>` name -> header row -> the Panel itself. */
function capabilityRow(name: string): HTMLElement {
  return screen.getByText(name).parentElement!.parentElement as HTMLElement;
}

it("renders one line per Project capability, with its state", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await reachClassifyAndMap(user);

  for (const name of ["Competitors", "Country", "Currency & FX", "Reporting timezone", "Tax & fees"]) {
    expect(await screen.findByText(name)).toBeInTheDocument();
  }
  // A machine key is not a name: this is what the row used to be called.
  expect(screen.queryByText("Capability.country")).not.toBeInTheDocument();
  // The state is the compiler's verdict, repeated, never recomputed here. Read on
  // the row itself: "Existing" is also the title of a summary panel above.
  expect(within(capabilityRow("Currency & FX")).getByText("Existing")).toBeInTheDocument();
  expect(within(capabilityRow("Country")).getByText("Proposed in Project Settings")).toBeInTheDocument();
  expect(screen.getAllByText("Proposed in Project Settings")).toHaveLength(4);
});

it("states the effect on this Datastream, not a generic sentence", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await reachClassifyAndMap(user);

  expect(await screen.findByText(/Adds country to the grain/)).toBeInTheDocument();
  expect(screen.getByText(/resolves to PAID_MEDIA/)).toBeInTheDocument();
  // The defect, graven in negative: the two sentences are not the same string, and
  // the constant that used to be on all five rows is nowhere.
  expect(screen.queryByText(/May change compatible Datastream fields/)).not.toBeInTheDocument();
});

it("says a capability effect is unknown rather than inventing one", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await reachClassifyAndMap(user);

  await screen.findByText("Reporting timezone");
  const row = capabilityRow("Reporting timezone");
  expect(within(row).getByText("Effect unknown before the first run")).toBeInTheDocument();
  // No zero stands in for the absence, on this row or anywhere in the section.
  expect(within(row).queryByText("0")).not.toBeInTheDocument();
});

it("no activation call leaves this screen", async () => {
  // LOCAL to this file, never `vi.stubGlobal`: a shared global spy leaks into every
  // other suite in the run (epic 46, already repaired once).
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" onOpenOwner={() => undefined} />);
  await reachClassifyAndMap(user);
  await user.click((await screen.findAllByRole("button", { name: "Propose" }))[0]);

  for (const call of fetchSpy.mock.calls) {
    const target = String(typeof call[0] === "string" ? call[0] : (call[0] as Request).url ?? "");
    expect(target).not.toContain("/settings/change-sets");
    expect(target).not.toContain("/settings/");
    const method = String((call[1] as RequestInit | undefined)?.method ?? "GET").toUpperCase();
    if (target.includes("capabilit")) expect(method).toBe("GET");
  }
  fetchSpy.mockRestore();
});

it("the only button on a capability line opens its owner", async () => {
  const opened: string[] = [];
  const user = userEvent.setup();
  render(
    <DatastreamSetupWizard
      projectId="proj_1"
      // A reference the shell cannot turn into a parsable address is a dead link,
      // whatever the button says.
      onOpenOwner={(owner) => opened.push(resolve(owner))}
    />,
  );
  await reachClassifyAndMap(user);
  await user.click((await screen.findAllByRole("button", { name: "Propose" }))[0]);

  expect(opened).toHaveLength(1);
  expect(opened[0]).toBe("/org/org_1/project/proj_1/settings/capabilities");
  // The address is one the shell can actually open — the assertion the old
  // `endswith("/settings/capabilities")` could not make.
  expect(parsePath(opened[0]).kind).toBe("resolved");
});

it("every owner link on this screen opens an address the shell can parse", async () => {
  // THE CLASS: not the capability row alone. Ten paths were composed server-side and
  // `parsePath` refused all ten; three of them were drawn as links on this card.
  const opened: string[] = [];
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue({
    ...proposalWith(CAPABILITIES),
    sections: [...proposalWith(CAPABILITIES).sections, DOWNSTREAM],
  });
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" onOpenOwner={(owner) => opened.push(resolve(owner))} />);
  await reachClassifyAndMap(user);

  for (const name of ["Open Semantic View", "Open Report"]) {
    await user.click(await screen.findByRole("button", { name }));
  }
  expect(opened).toEqual([
    "/org/org_1/project/proj_1/governance/semantic-model",
    "/org/org_1/project/proj_1/analyze/reports",
  ]);
  for (const address of opened) expect(parsePath(address).kind).toBe("resolved");
  // Not one `<a href>` remains on the card: an anchor here is a composed address.
  expect(document.querySelectorAll("a[href^='/projects/']")).toHaveLength(0);
});

it("never labels the action Enable", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await reachClassifyAndMap(user);

  expect(await screen.findAllByRole("button", { name: "Propose" })).toHaveLength(4);
  expect(screen.queryByRole("button", { name: /enable|activate/i })).toBeNull();
  // The one active capability offers the same gesture under its own verb.
  expect(screen.getByRole("button", { name: "Open Project Settings" })).toBeInTheDocument();
});

it("an empty capability list is not a failure, and a missing section is", async () => {
  const emptyRow = capabilityItem("capabilities.none", "Project capabilities", "This Project has no capability row yet", "Project capabilities are written by the seed_project_capabilities trigger when the Project is created; this Project carries none.", "unavailable");
  emptyRow.key = "capabilities.none";
  emptyRow.owner_links = [];
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposalWith([emptyRow]));
  const user = userEvent.setup();
  const view = render(<DatastreamSetupWizard projectId="proj_1" />);
  await reachClassifyAndMap(user);

  expect(await screen.findByText("This Project has no capability row yet")).toBeInTheDocument();
  expect(screen.getByText(/seed_project_capabilities trigger/)).toBeInTheDocument();
  expect(screen.queryByText("Project capabilities could not be read")).not.toBeInTheDocument();

  // A proposal carrying NO capabilities section is a read that failed, and it says
  // something else entirely.
  view.unmount();
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposalWith(null));
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await reachClassifyAndMap(userEvent.setup());
  expect(await screen.findByText("Project capabilities could not be read")).toBeInTheDocument();
  expect(screen.queryByText("This Project has no capability row yet")).not.toBeInTheDocument();
});
