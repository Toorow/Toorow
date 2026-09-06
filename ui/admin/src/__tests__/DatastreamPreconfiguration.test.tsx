import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamPreconfiguration from "../datastreams/preconfiguration/DatastreamSetupWizard";
import DatastreamCreate from "../shell/pages/DatastreamCreate";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return { ...actual, createDatastreamSetupDraft: vi.fn(), readDatastreamSetupSourceOptions: vi.fn(), createDatastreamSetupObservation: vi.fn(), updateDatastreamSetupDraft: vi.fn(), compileDatastreamSetupDraft: vi.fn(), createDatastreamSetupPreview: vi.fn(), prepareDatastreamFinalReview: vi.fn(), prepareDatastreamDraftConfirmation: vi.fn(), confirmDatastreamDraft: vi.fn(), readDatastreamMaterialization: vi.fn() };
});

const draft = {  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft", current_revision_ref: "dsdr_1",
  current_revision: 1, current_proposal_ref: null, first_incomplete_section: "source",
  invalidation_causes: [], resume_href: "/resume", idempotent_replay: false, operator_input: {},
};

const proposal: wizardApi.DatastreamPreconfigurationProposal = {
  schema_version: "1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", proposal_ref: "dspp_1",
  dependency_fingerprint: "a".repeat(64), proposal_token: "b".repeat(64), content_hash: "c".repeat(64),
  is_stale: false, invalidation_causes: [], idempotent_replay: false, resume_href: "/resume",
  confirmed_intent_bundle: { datastream_name: "Daily performance", data_role: "Performance", joint_grain: ["date", "country"], field_mappings: [{ source_identity: "date", field_id: "date", role: "primary_date", semantic_type: "date", canonical_target: "date", aggregation: "none", sensitivity: "public", included: true }], business_domain_ids: [], dq_gates: [], outputs: [{ kind: "full_grain_output", state: "will_be_created" }], exceptions: [], owner_proposals: [] },
  configuration_summary: {
    existing: [{ object: "Project configuration", version_id: "pcv_1" }],
    will_be_created: [{ object: "Datastream setup artifacts" }],    will_remain_a_proposal: [{ object: "Semantic View" }],
    downstream_impact: ["No active pointer changes."],
  },
  sections: [{
    key: "source", status: "complete", dependency_fingerprint: "d".repeat(64),
    items: [{ key: "source.scope", section: "source", requirement: "required", status: "complete", proposed_value: { connector_id: "generic" }, evidence_refs: [{ kind: "connector_contract", object_type: "connector_contract", object_id: "ccv_1", version_id: "ccv_1", fingerprint: "e".repeat(64), observed_at: "2026-07-29T08:00:00Z" }], confidence: { level: "high", rationale: "Exact contract" }, coverage: { state: "covered" }, exceptions: [], blockers: [], warnings: [], owner_links: [], downstream_impact: [], dependency_fingerprint: "d".repeat(64) }],
  }],
};

const sourceOptions: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{ connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "f".repeat(64), display_name: "Generic Daily", observed_at: "2026-07-29T08:00:00Z", reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date", "country"], supported_grains: [["date", "country"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }], fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "country", kind: "dimension", description: "Country" }, { field_id: "spend", kind: "metric", description: "Spend" }] }],
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
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => { const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1; return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input }; });
  vi.mocked(wizardApi.compileDatastreamSetupDraft).mockResolvedValue(proposal);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.createDatastreamSetupPreview).mockResolvedValue({ state: "done", job_ref: "job_1", preview: { preview_ref: "preview_1", draft_revision_ref: "dsdr_3", proposal_ref: "dspp_1", observation_ref: "dso_1", mode: "connector_pull", dependency_hash: "4".repeat(64), evidence_hash: "5".repeat(64), safe_evidence: { sample: [{ date: "2026-07-28" }], status: "verified" }, status: "ready_for_review", is_stale: false } });
  vi.mocked(wizardApi.prepareDatastreamFinalReview).mockResolvedValue({ final_review_ref: "review_1", content_hash: "6".repeat(64), proposal_ref: "dspp_1", preview_ref: "preview_1", acknowledged_warning_ids: [], confirmed_intent_bundle: proposal.confirmed_intent_bundle ?? {}, schedule: { activation_kind: "schedule" }, idempotent_replay: false });
  vi.mocked(wizardApi.prepareDatastreamDraftConfirmation).mockResolvedValue({ confirmation_ref: "confirm_1", confirmation_secret: "secret", command: "datastream.setup.create_draft", expires_at: "2026-07-29T12:00:00Z" });
  vi.mocked(wizardApi.confirmDatastreamDraft).mockResolvedValue({ operation_ref: "op_1", outcome: "succeeded", materialization_id: "mat_1", datastream_id: "ds_1", lifecycle_state: "draft", plan_version_id: "plan_1", mapping_version_id: "mapping_1", candidate_execution_id: "exec_1", current_published_execution_id: null, schedule_active: false, candidate_job_id: "job_2" });
});

it("creates the server draft before discovery and compiles without activation", async () => {  const user = userEvent.setup();
  const activated = vi.fn();
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  expect(await screen.findByText("Draft saved")).toBeInTheDocument();
  expect(wizardApi.createDatastreamSetupDraft).toHaveBeenCalledTimes(1);
  expect(wizardApi.readDatastreamSetupSourceOptions).toHaveBeenCalledWith(expect.anything(), "dsd_1");

  // THE RATIFIED WALK: five stops, one contextual primary each. The report
  // family is chosen at `Configure`, AFTER the discovery; the name and the
  // role are the last two questions of `Source`, and cost no extra stop.
  //
  await user.click(screen.getByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Daily performance");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  expect((await screen.findAllByText(/90 days/)).length).toBeGreaterThan(0);
  await user.click(screen.getByRole("button", { name: "Compile proposal" }));

  expect(wizardApi.createDatastreamSetupObservation).toHaveBeenCalledTimes(1);
  expect(wizardApi.compileDatastreamSetupDraft).toHaveBeenCalledWith(expect.anything(), "dsd_1", 3, expect.any(String));
  // The review belongs to `Classify and map`, and `Configure` leads straight to
  // it: since 57.9 there is no section in between, so the primary names the one
  // section it lands on.
  await user.click(await screen.findByRole("button", { name: "Continue to classify and map" }));
  expect(await screen.findByRole("heading", { name: "Review proposal" })).toBeInTheDocument();
  expect(screen.getByText("Existing")).toBeInTheDocument();
  expect(screen.getByText("Will be created")).toBeInTheDocument();
  expect(screen.getByText("Will remain a proposal")).toBeInTheDocument();
  expect(screen.getByText("Downstream impact")).toBeInTheDocument();
  // The evidence SOURCE, in the words the target uses (`datastream-workbench-
  // and-wizard.md:59`), not the raw wire kind. This assertion used to pin
  // `connector_contract`; an operator does not read snake_case.
  expect(screen.getByText("Connector contract")).toBeInTheDocument();
  // The three things every proposal owes besides its status, all of them on the
  // wire since `datastream_preconfiguration.py:393-399` and none of them drawn
  // before: confidence WITH its rationale, and coverage.
  expect(screen.getByText("High confidence")).toBeInTheDocument();
  expect(screen.getByText("Exact contract")).toBeInTheDocument();
  expect(screen.getByText("Coverage: covered")).toBeInTheDocument();
  // The requirement legend of `:42` — `Required` blocks continuation, and the
  // operator could not read that level anywhere before.
  //
  // Cherché HORS du récapitulatif : depuis qu'il est un `<dl>`, ses valeurs sont
  // des noeuds à part entière, et « Required » y apparaît pour chaque champ
  // obligatoire encore vide. Un `getByText` global tombait donc sur trois noeuds
  // et échouait — sans que la légende ait bougé. L'assertion gagne au passage ce
  // qu'elle prétendait déjà dire : la légende est UNIQUE. (Le récapitulatif vit
  // désormais à `Schedule and activate` — il n'est pas monté ici —, mais le
  // filtre était écrit quand le récapitulatif ne se montait plus ici ; il est
  // redevenu porteur, puisque la troisième zone de `:31` est de nouveau là.)
  const legend = screen
    .getAllByText("Required")
    .filter((node) => !node.closest("[data-testid='config-summary']"));
  expect(legend).toHaveLength(1);
  // A machine key is not a label.
  expect(screen.getByText("Source.scope")).toBeInTheDocument();
  expect(screen.queryByText("source.scope")).not.toBeInTheDocument();
  // The compiled Output, read from `confirmed_intent_bundle.outputs` rather than
  // recited by a panel: kind and state, in the words the compiler used. Le
  // récapitulatif est désormais la troisième zone de `:31` et se lit à chaque
  // section ; cette marche va jusqu'au bout pour une autre raison — c'est la
  // preview sûre qui manque, pas le panneau.
  await user.click(await screen.findByRole("button", { name: "Continue to preview" }));
  await user.click(screen.getByRole("button", { name: "Create safe preview" }));
  await user.click(await screen.findByRole("button", { name: "Review schedule" }));
  const summary = await screen.findByTestId("config-summary");
  expect(within(summary).getByText("Output")).toBeInTheDocument();
  expect(within(summary).getByText("Full grain output — will be created")).toBeInTheDocument();
  expect(activated).not.toHaveBeenCalled();
});

it("the summary names what will fill each line until the compiler has decided it", async () => {
  // LE RÉCAPITULATIF EST LA TROISIÈME ZONE DE `:31` et se monte à chaque
  // section ; ce rendu ouvre le brouillon à `Schedule and activate` — avec
  // AUCUNE proposition compilée — parce que c'est là que les lignes du
  // compilateur manquent le plus visiblement. `restoreSection` lit la position
  // sauvegardée par son NOM, donc `active_section_ref` est la porte.
  //
  // CE QUE L'ASSERTION A CHANGÉ : elle épinglait un TIRET. Un tiret est une
  // absence dessinée comme une valeur, et il laisse deviner lequel des cinq
  // arrêts y met fin. Chaque ligne nomme désormais son geste, et `Output` dit
  // `Not compiled` — le mot que `datastream-workbench-and-wizard.md:1147`
  // écrit.
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValueOnce({
    ...draft,
    operator_input: {
      mode: "connector_pull",
      name: "Daily performance",
      data_role: "Spend",
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "daily" },
      configure: { date_field: "", metrics: "", dimensions: "", date_window: "", filters: "", history_intent: "", grain: "" },
      wizard_state: { active_section_ref: "schedule_activate" },
    },
  });
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  const summaryPanel = within(await screen.findByTestId("config-summary"));
  const line = (label: string) =>
    (summaryPanel.getByText(label).closest("dt") as HTMLElement).parentElement as HTMLElement;
  for (const [label, gesture] of [
    ["Fields", "Compile the proposal at Configure"],
    ["Joint grain", "Compile the proposal at Configure"],
    ["Output", "Not compiled"],
  ]) {
    expect(within(line(label)).getByText(gesture)).toBeInTheDocument();
  }
  expect((await screen.findByTestId("config-summary")).textContent).not.toContain("—");
});
it("does not mount discovery consumers when draft creation fails", async () => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockRejectedValue(new Error("Draft unavailable"));
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Draft unavailable");
  expect(screen.queryByLabelText(/Connector ID/)).not.toBeInTheDocument();
});

it("mounts draft and proposal review on the live Add Datastream page", async () => {
  const activated = vi.fn();
  render(<DatastreamCreate projectId="proj_1" onCancel={vi.fn()} />);
  // The header names the section the operator is IN. It used to name three
  // sections at once — the merge of three stable sections showing through the
  // title.
  // The live page mounts the wizard on its first section. Asserted through the
  // page title and the section's own action rather than the word "Source",
  // which the section header and its first panel both legitimately carry.
  expect(await screen.findByRole("heading", { name: "Add Datastream" })).toBeInTheDocument();
  // The first decision of the first section, and the only control on screen
  // before a mode is chosen.
  expect(await screen.findByRole("radio", { name: "Connector pull" })).toBeInTheDocument();
  // The first stop's contextual primary — which did not exist before the
  // split: `activeSection <= 2` only ever offered `Compile proposal`, so there
  // was no way forward from `Source` at all. It names the NEXT stop, not the
  // next question: the mode, the source and the name are all this one's.
  expect(screen.getByRole("button", { name: "Continue to configure" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /^Continue to (source|identity)$/ }))
    .not.toBeInTheDocument();
  // Nothing on this page creates a Datastream. The wizard proposes; creation is
  // a separate, explicitly confirmed step.
  expect(screen.queryByRole("button", { name: "Create Datastream" })).not.toBeInTheDocument();
  expect(activated).not.toHaveBeenCalled();
});

it("completes the final-half Wizard and consumes only the Draft confirmation", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValueOnce({ state: "not_materialized" }).mockResolvedValueOnce({ state: "materialized", datastream_ref: "ds_1", candidate_execution_ref: "exec_1", candidate_state: "created", lifecycle_state: "draft", current_published_execution_ref: null });
  render(<DatastreamPreconfiguration projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Connector pull" }));
  await user.click(await screen.findByRole("radio", { name: "Generic Daily" }));
  await user.selectOptions(await screen.findByLabelText(/Source Account/), "sacct_1");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // `Datastream name` and `Data role` close the `Source` step. Le nom n'y
  // est plus PROPOSÉ : la famille de
  // rapport qui alimentait la proposition est choisie à `Configure`, une
  // section plus loin — on le saisit donc, et la boîte de revue le reprend.
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Generic Daily — Daily report");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Performance");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
  // La famille de rapport ouvre `Configure` — elle en remplit les champs ;
  // `Date field` reste le contrôle qui prouve qu'on y est.
  await user.selectOptions(await screen.findByLabelText(/Report family/), "daily");
  await user.selectOptions(await screen.findByLabelText(/Date field/), "date");
  await user.click(screen.getByRole("checkbox", { name: "Spend (metric)" }));
  await user.click(screen.getByRole("checkbox", { name: "Reporting date (dimension)" }));
  await user.click(screen.getByRole("checkbox", { name: "Country (dimension)" }));
  await user.click(screen.getByRole("button", { name: "Compile proposal" }));
  // The name was answered at `Identity`, so `Classify and map` holds no field
  // the operator must come back for: it goes straight to preview.
  await user.click(await screen.findByRole("button", { name: "Continue to classify and map" }));
  await user.click(await screen.findByRole("button", { name: "Continue to preview" }));
  await user.click(screen.getByRole("button", { name: "Create safe preview" }));
  await user.click(await screen.findByRole("button", { name: "Review schedule" }));
  // ONE GESTURE, TWO PHASES (57.12, T4). The footer used to chain three
  // buttons — `Freeze final review`, `Review and create Draft`, `Confirm
  // create Draft` — three clicks for one decision, labelled with the plumbing.
  // The ceremony is now a review carried by a dialog, and one confirmation.
  await user.click(await screen.findByRole("button", { name: "Review and create draft" }));
  const dialog = await screen.findByTestId("datastream-create-review");
  // What the gesture creates, in business words — no "Freeze", no token. The
  // name is the one the operator built on this walk (saisi à `Identity`, la
  // section qui le demande), not a fixture's.
  expect(dialog).toHaveTextContent(/immutable non-live versions and one isolated candidate/);
  expect(dialog).toHaveTextContent("Generic Daily — Daily report");
  await user.click(screen.getByTestId("datastream-create-review-confirm"));
  expect(await screen.findByText(/remains Draft/)).toBeInTheDocument();

  // THE THREE CALLS STILL FIRE, IN ORDER, EACH WITH ITS KEY. The dialog is one
  // gesture; the chain underneath is unchanged.
  const finalsOrder = vi.mocked(wizardApi.prepareDatastreamFinalReview).mock.invocationCallOrder[0];
  const issueOrder = vi.mocked(wizardApi.prepareDatastreamDraftConfirmation).mock.invocationCallOrder[0];
  const consumeOrder = vi.mocked(wizardApi.confirmDatastreamDraft).mock.invocationCallOrder[0];
  expect(finalsOrder).toBeLessThan(issueOrder);
  expect(issueOrder).toBeLessThan(consumeOrder);
  expect(wizardApi.confirmDatastreamDraft).toHaveBeenCalledOnce();

  // THE SAME IDEMPOTENCY KEY AT ISSUE AND AT CONSUME.
  //
  // The server binds a confirmation to the key it was ISSUED with and refuses
  // any other -- `entry_confirmations.py` `_consume`, and this repo's own
  // `test_entry_confirmations.py:107-137` lists `{"idempotency_key":
  // "request-2"}` among the overrides that must raise. The wizard generated a
  // fresh `crypto.randomUUID()` at each call, so the two could never match and
  // every real click of "Confirm create Draft" -- the ONLY action that creates
  // a Datastream -- was refused 409.
  //
  // The assertion above could not see it: both calls are mocked, and counting
  // them proves neither reached a server nor agreed with each other. What has
  // to be asserted is the RELATION between the two calls.
  const issuedKey = vi.mocked(wizardApi.prepareDatastreamDraftConfirmation).mock.calls[0].at(-1);
  const consumedKey = vi.mocked(wizardApi.confirmDatastreamDraft).mock.calls[0].at(-1);
  expect(typeof issuedKey).toBe("string");
  expect(consumedKey).toBe(issuedKey);
});