import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

/** Story 57.1 — what the External BigQuery step shows once its adapter exists.
 *
 *  Four things are pinned here, and each of them was a defect before: the step
 *  no longer claims discovery is unwired, it lists the observed columns by their
 *  dotted paths without offering a folder or an array, it shows what a read
 *  would cost before any read, and it tells an empty warehouse apart from a
 *  broken adapter with two different sentences. */

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
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

/** The access names its object, and its dataset was NOT capped — so the
 *  reference below is a read, not a second typing of the same answer. */
const access: wizardApi.DatastreamSetupSourceAccount = {
  object_ref: { id: "sacct_bq_1" }, connector_ref: { id: "bigquery" },
  label: "export_v1", states: { availability: "available" },
  external_object_ref: "warehouse.billing.export_v1",
  truncated: false, listed_objects: 12, listing_bound: 500,
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1", source_accounts: [], connectors: [],
  managed_channels: [{ channel: "file_upload", availability: "available", template_ref: null }],
  external_access: [access],
};

/** Two fields, one of them a nested leaf. `service` (the STRUCT) and `credits`
 *  (the REPEATED record) are absent because the typed client never emits them. */
/** Four fields, and two of them are the ones this story argued about: a nested
 *  leaf that CAN be selected, and the `REPEATED RECORD` above it that cannot.
 *  Both are listed — describing is not offering. */
const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", draft_revision: 2,
  mode: "external_bq", discovery_kind: "warehouse_schema", adapter_ref: "external_bq.readonly.v1",
  connector_contract_version_ref: null, request_fingerprint: "a".repeat(64),
  evidence_fingerprint: "b".repeat(64), schema_hash: "c".repeat(64),
  safe_metadata: {
    fields: [
      { name: "event_date", field_id: "event_date", type: "DATE", nullable: false, mode: "REQUIRED", description: "Reporting day" },
      { name: "service", field_id: "service", type: "RECORD", nullable: true, mode: "NULLABLE" },
      { name: "service.description", field_id: "service.description", type: "STRING", nullable: true, mode: "NULLABLE" },
      { name: "credits", field_id: "credits", type: "RECORD", nullable: true, mode: "REPEATED" },
    ],
    location: "EU", watermark: "event_date", freshness: null,
    quota_cost: { bytes_scanned_estimate: 2_097_152, unit: "bytes", measured_by: "bigquery_dry_run_query", measures: "What one read of this object would scan. Planned by BigQuery without being executed, and not billed." },
  },
  coverage: {
    schema: "available", scan_estimate: "available", unselectable_fields: "2",
    field_list: "complete", fields_listed: "4", fields_observed: "4",
  },
  exceptions: [], observed_at: "2026-08-05T09:00:00Z", expires_at: null, idempotent_replay: false,
};

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue(observation);
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    const current = (input.source as { observation_ref?: string } | undefined)?.observation_ref ? revision : revision + 1;
    return { ...draft, current_revision: current, current_revision_ref: `dsdr_${current}`, operator_input: input };
  });
});

/** LE PARCOURS EXTERNE JUSQU'À `CONFIGURE` : cinq
 *  sections courtes, une visible à la fois. Le mode se choisit seul, la source
 *  s'observe à la section `Source`, puis l'identité — nom et rôle — a sa PROPRE
 *  section avant `Configure`. Un bouton primaire contextuel par section. */
async function discoverExternalObject(user: ReturnType<typeof userEvent.setup>) {
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "sacct_bq_1");
  await user.type(screen.getByLabelText(/Declared writer/), "warehouse-etl");
  await user.click(screen.getByRole("checkbox", { name: /read-only/i }));
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // Le primaire de `Source` ne s'active qu'une fois l'observation créée, et un
  // clic userEvent sur un bouton désactivé est un no-op SILENCIEUX : on attend
  // l'activation avant de cliquer. Il s'appelait `Continue to configure` avant
  // le nom et le rôle ferment l'étape `Source`.
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  // Le nom n'est plus proposé nulle part dans le parcours avant `Configure` :
  // la famille de rapport qui alimentait la proposition est choisie UNE section
  // plus loin. On le saisit à la fin de `Source`.
  await user.type(await screen.findByLabelText(/Datastream name/), "Warehouse billing");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Spend");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
}

it("no longer claims the discovery adapter is unwired", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  // Le mode ouvre l'étape : les champs de
  // source vivent une section plus loin, atteinte par le primaire contextuel.
  await screen.findByLabelText(/BigQuery access/);
  expect(screen.queryByText(/External BigQuery discovery is not wired/)).not.toBeInTheDocument();
  expect(screen.queryByText(/adapter_unavailable/)).not.toBeInTheDocument();
});

it("reads the object from the chosen access instead of asking for it twice", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "sacct_bq_1");
  const objectField = await screen.findByLabelText(/Table or view reference/);
  await waitFor(() => expect(objectField).toHaveValue("warehouse.billing.export_v1"));
  expect(objectField).toHaveAttribute("readonly");
});

it("reopens free entry, and says why, when the dataset listing was capped", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    external_access: [{ ...access, truncated: true, listed_objects: 500 }],
  });
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "sacct_bq_1");
  expect(await screen.findByText(/reached the discovery listing bound/i)).toBeInTheDocument();
  expect(await screen.findByText(/500 objects are listed for warehouse.billing, which is the bound/)).toBeInTheDocument();
  const objectField = screen.getByLabelText(/Table or view reference/);
  expect(objectField).not.toHaveAttribute("readonly");
  expect(objectField).toHaveValue("");
});

it("does not call a count a bound when the bound could not be read", async () => {
  const user = userEvent.setup();
  // `truncated` is also true when the server could not read the walk's bound at
  // all. Saying "12, which is the bound of the discovery walk" states a number
  // that is not the bound — the count is simply what was listed.
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    external_access: [{ ...access, truncated: true, listed_objects: 12, listing_bound: 0 }],
  });
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "sacct_bq_1");
  expect(await screen.findByText(/the bound the discovery walk applies could not be read here/)).toBeInTheDocument();
  expect(screen.queryByText(/which is the bound of the discovery walk/)).not.toBeInTheDocument();
  expect(screen.getByLabelText(/Table or view reference/)).not.toHaveAttribute("readonly");
});

it("lists the observed columns by their dotted paths, with mode and no value", async () => {
  const user = userEvent.setup();
  await discoverExternalObject(user);
  expect(await screen.findByText("service.description")).toBeInTheDocument();
  // Twice on purpose: once as a column of the object, once as the watermark it
  // was chosen to be. Both name their origin.
  expect(screen.getAllByText("event_date").length).toBe(2);
  expect(screen.getAllByText("REQUIRED").length).toBeGreaterThan(0);
  expect(screen.getByText("Reporting day")).toBeInTheDocument();
});

it("describes a nested field and refuses it as a column, in the same row", async () => {
  const user = userEvent.setup();
  await discoverExternalObject(user);
  // DESCRIBING IS NOT OFFERING. Hiding the STRUCT and the array showed a table
  // missing half its columns; showing them without saying they cannot be picked
  // would offer a folder as a column. Both halves are on the row.
  expect(await screen.findByText("service")).toBeInTheDocument();
  expect(screen.getByText("credits")).toBeInTheDocument();
  expect(screen.getAllByText("RECORD").length).toBe(2);
  expect(screen.getByText("REPEATED")).toBeInTheDocument();
  expect(screen.getAllByText(/No — a group, not a column/).length).toBe(2);
  expect(screen.getByText(/2 fields cannot be selected as a column/i)).toBeInTheDocument();
});

it("says how short the field list is when the evidence budget cut it", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    coverage: { ...observation.coverage, field_list: "truncated", fields_listed: "72", fields_observed: "310" },
    exceptions: [{ code: "field_list_truncated" }],
  });
  await discoverExternalObject(user);
  // The counts come from the server: the rendered array's own length IS the
  // truncated one, so it can never say what was lost.
  expect(await screen.findByText(/This list is shorter than the object/)).toBeInTheDocument();
  expect(screen.getByText(/72 of 310 columns are listed here/)).toBeInTheDocument();
});

it("shows what a read would scan, before any read", async () => {
  const user = userEvent.setup();
  await discoverExternalObject(user);
  expect(await screen.findByText(/Estimated scan: 2.0 MB/)).toBeInTheDocument();
  expect(screen.getByText(/without being executed, and not billed/)).toBeInTheDocument();
});

it("says an empty warehouse is empty, and never calls it a failure", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { fields: [], location: null, watermark: null, freshness: null, quota_cost: null },
    coverage: { schema: "unavailable", scan_estimate: "unavailable" },
    exceptions: [{ code: "schema_unavailable" }, { code: "scan_estimate_unavailable" }],
  });
  await discoverExternalObject(user);
  expect(await screen.findByText(/exposes no readable column on that object/)).toBeInTheDocument();
  expect(screen.getByText(/roles\/bigquery.dataViewer/)).toBeInTheDocument();
  expect(screen.queryByText(/could not read this object/)).not.toBeInTheDocument();
  // The missing estimate is its own refusal, told apart from the empty schema:
  // one is answered in Google Cloud, the other by re-running discovery.
  expect(screen.getAllByText(/No scan estimate is attached to this observation/).length).toBeGreaterThan(0);
});

it("says a failed discovery is a failure, and names the adapter", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockRejectedValue(
    new wizardApi.WizardApiError(503, "observation_unavailable", "The External BigQuery discovery adapter (external_bq.readonly.v1) could not read this object. Nothing was saved."),
  );
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "sacct_bq_1");
  await user.type(screen.getByLabelText(/Declared writer/), "warehouse-etl");
  await user.click(screen.getByRole("checkbox", { name: /read-only/i }));
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  expect(await screen.findByText(/could not read this object/)).toBeInTheDocument();
  expect(screen.getByText(/external_bq.readonly.v1/)).toBeInTheDocument();
  // Not the other sentence: this one is a fault, not an honest emptiness.
  expect(screen.queryByText(/exposes no readable column/)).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// 76-4 — the refusal path, RENDERED. `console-presentation.md` §5: one error is
// shown once, in one formulation, and it carries a way forward.
//
// WHY THESE CASES EXIST. The missing scan estimate was rendered by TWO
// `role="alert"` blocks over ONE sentence, under two different titles — `No
// scan estimate` inside `Configure`, and `A read cannot be launched without its
// scan estimate` inside `Preview and validate`. Only one section is drawn at a
// time, so the two never met on screen and no test could have caught it by
// counting nodes in one state. What catches it is asserting the sentence is
// rendered ONCE per screen state, and asserting it from more than one state.
// ---------------------------------------------------------------------------

/** The one sentence, wherever the operator is standing. */
const NEEDS_ESTIMATE = /No scan estimate is attached to this observation/;

it("says the missing scan estimate ONCE, and in one formulation", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { ...observation.safe_metadata, quota_cost: null },
  });
  await discoverExternalObject(user);

  // ONE rendering, not two. `getAllByText` rather than `getByText` so the
  // failure reads "expected 1, received 2" instead of "found multiple".
  expect(await screen.findAllByText(NEEDS_ESTIMATE)).toHaveLength(1);

  // And ONE alert carrying it: the block is `Status as="block" tone="error"`,
  // which is `role="alert"`. The retired second copy had a different title over
  // the same sentence, and that is the defect this pins.
  const alerts = screen.getAllByRole("alert").filter((node) => NEEDS_ESTIMATE.test(node.textContent ?? ""));
  expect(alerts).toHaveLength(1);
  expect(alerts[0]).toHaveTextContent("A read cannot be launched without its scan estimate");
  expect(screen.queryByText("No scan estimate")).not.toBeInTheDocument();
});

it("carries the same fallback on Source, where it opens the control that repairs it", async () => {
  // THE FALLBACK IS ON EVERY RENDERING, not on most of them. `goToSection` also
  // FOCUSES the section it opens, so on step 1 this moves the operator onto the
  // discovery control rather than nowhere -- which is why it is not suppressed
  // here. A first version did suppress it, and this case is what refused that.
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { ...observation.safe_metadata, quota_cost: null },
  });
  await discoverExternalObject(user);

  const alert = screen.getAllByRole("alert").find((node) => NEEDS_ESTIMATE.test(node.textContent ?? ""));
  expect(alert).toBeDefined();
  // The sentence names the gesture, and the block carries a control for it.
  expect(alert).toHaveTextContent("Re-run Discover source");
  const back = within(alert as HTMLElement).getByRole("button", { name: "Go to Source" });
  expect(back).toBeEnabled();
  await user.click(back);
  expect(await screen.findByRole("radio", { name: "External BigQuery" })).toBeInTheDocument();
});
