import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import * as wizardApi from "../datastreams/wizard/wizardApi";

/** Story 57.2 — what the Google Sheets channel shows once its client exists.
 *
 *  Four things are pinned, and each was a defect before: the account list was
 *  empty for EVERY Google authorization that can exist (it compared the provider
 *  "google" to the Connector id "google-sheets"), the step claimed discovery was
 *  unwired, step 2 stated no column, and nothing said that the header row is an
 *  assumption or that no type is declared by a spreadsheet. */

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

/** THE FIXTURE THAT PROVES THE REPAIR. `connector_ref.id` is "google" — the
 *  authorization's provider, which names no tool — and `opens` is where the
 *  Connector really is. Under the old filter this list stayed empty. */
const account: wizardApi.DatastreamSetupSourceAccount = {
  object_ref: { id: "sacct_sheet_1" }, connector_ref: { id: "google" },
  label: "Media plan workbook", states: { availability: "available" },
  opens: [{ connector_name: "google-sheets", display_name: "Google Sheets", available: true }],
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1", source_accounts: [account], connectors: [],
  managed_channels: [
    { channel: "file_upload", availability: "available", template_ref: null },
    { channel: "google_sheets", availability: "available", template_ref: null },
  ],
  external_access: [],
};

const observation: wizardApi.DatastreamSetupObservation = {
  observation_ref: "dso_1", draft_ref: "dsd_1", draft_revision_ref: "dsdr_2", draft_revision: 2,
  mode: "managed_feed", discovery_kind: "sheet_schema",
  adapter_ref: "managed_feed.google_sheets.readonly.v1",
  connector_contract_version_ref: null, request_fingerprint: "a".repeat(64),
  evidence_fingerprint: "b".repeat(64), schema_hash: "c".repeat(64),
  safe_metadata: {
    fields: [
      { name: "Date", field_id: "Date", type: "unknown", nullable: true, mode: "UNKNOWN" },
      { name: "Spend", field_id: "Spend", type: "unknown", nullable: true, mode: "UNKNOWN" },
    ],
    objects: [
      { object_ref: "SHEET_ID!Q1", label: "Q1" },
      { object_ref: "SHEET_ID!Budget", label: "Budget" },
    ],
    location: "fr_FR", watermark: null, freshness: null, row_count_bucket: "1000-9999",
  },
  coverage: {
    schema: "available", values: "not_observed", tab: "named", header: "assumed",
    types: "not_inferred", grid: "available",
    field_list: "complete", fields_listed: "2", fields_observed: "2",
    object_list: "complete", objects_listed: "2", objects_observed: "2",
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

/** Ouvre le canal Google Sheets à l'étape `Source`. Le mode est la première
 *  question de cette étape ; le canal, l'autorisation Google et la référence
 *  du classeur sont les suivantes, dans la même étape et sans arrêt entre
 *  elles. */
async function chooseSheetsChannel(user: ReturnType<typeof userEvent.setup>) {
  render(<DatastreamSetupWizard projectId="proj_1" />);
  await user.click(await screen.findByRole("radio", { name: "Managed feed" }));
  await user.selectOptions(await screen.findByLabelText(/Channel/), "google_sheets");
}

/** Découvre l'onglet puis rejoint `Configure`, où la PREUVE de l'observation
 *  se lit désormais (schéma découvert, sentences du canal — amendement du
 *  Un seul guichet : le primaire de `Source` (nom + rôle compris, sa
 *  propre section — le nom n'est plus proposé en marche avant, la famille de
 *  rapport qui l'alimentait étant choisie à `Configure`) puis `Configure`.
 *  Après `Discover source`, le primaire ne s'active qu'une fois l'observation
 *  posée : un clic sur un bouton désactivé est un no-op silencieux, donc on
 *  ATTEND qu'il soit actif avant de cliquer. */
async function discoverSheet(user: ReturnType<typeof userEvent.setup>) {
  await chooseSheetsChannel(user);
  await user.type(screen.getByLabelText(/Sheet reference/), "SHEET_ID!Budget");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  // THE LAST TWO QUESTIONS OF `Source` COME UP ON THEIR OWN once the source
  // has answered: no stop, no click, no rail entry between them and the
  // questions above (`datastream-workbench-and-wizard.md:31`, `:183`).
  await screen.findByLabelText(/Datastream name/);
  await user.type(await screen.findByLabelText(/Datastream name/), "Media plan");
  await user.selectOptions(await screen.findByLabelText(/Data role/), "Forecast & plan");
  await user.click(await screen.findByRole("button", { name: "Continue to configure" }));
}

/** THIS CHANNEL ASKS FOR THE GRANT, NOT FOR A PRODUCT ACCOUNT.
 *
 *  The ratified companion says no Connector, no authorization and no account is
 *  asked on this path — "showing them would be asking about a product where the
 *  source is a channel" — and the screen asked for a `Source Account` anyway,
 *  because a private workbook needs a Google consent carrying
 *  `spreadsheets.readonly`. Measured on 2026-08-10: the server never reads the
 *  account. `_sheets_connection_id` resolves whatever scope it is given to
 *  `cr.id`, and every scope of one consent carries the same one — so the scope
 *  was a detour to the grant and could not change a byte of what is read.
 *
 *  The account list is still filtered through `opens` (57.2): `connector_ref.id`
 *  is "google", and comparing it to "google-sheets" left this empty for every
 *  Google authorization that can exist. */
it("asks for the Google grant and never for an account of it", async () => {
  const user = userEvent.setup();
  await chooseSheetsChannel(user);

  expect(await screen.findByLabelText(/Google authorization/)).toBeInTheDocument();
  // ABSENT, not hidden: the account is written for the operator, never asked.
  expect(screen.queryByLabelText(/Source Account/)).not.toBeInTheDocument();
  expect(screen.queryByText(/No Google Sheets access is exposed/)).not.toBeInTheDocument();
});

/** ONE GRANT IS NOT A DECISION — the rule step 1 already applies to a single
 *  candidate. It is written on the gesture that selects the channel, so
 *  `Discover source` is reachable without an answer nobody was asked for. */
it("defaults the only Google grant, and writes the scope the server resolves", async () => {
  const user = userEvent.setup();
  await chooseSheetsChannel(user);

  const grant = await screen.findByLabelText(/Google authorization/) as HTMLSelectElement;
  expect(grant.value).not.toBe("");
  expect(screen.getByText(/only Google consent this Project exposes for Sheets/)).toBeInTheDocument();
  await user.type(screen.getByLabelText(/Sheet reference/), "SHEET_ID!Budget");
  expect(screen.getByRole("button", { name: "Discover source" })).toBeEnabled();
});

/** TWO CONSENTS ARE TWO OPTIONS, and choosing one writes a scope OF it. The
 *  scope is not a choice withheld from the operator — it does not exist: both
 *  scopes of a consent resolve to the same `cr.id`. */
it("offers one option per consent, and choosing one writes a scope of that consent", async () => {
  let saved: Record<string, unknown> | undefined;
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => {
    saved = input;
    return { ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input };
  });
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({
    ...options,
    source_accounts: [
      { ...account, object_ref: { id: "sacct_alpha_1" }, label: "Alpha plans",
        connection_ref: { object_type: "connection", id: "conn_alpha" } },
      { ...account, object_ref: { id: "sacct_alpha_2" }, label: "Alpha budgets",
        connection_ref: { object_type: "connection", id: "conn_alpha" } },
      { ...account, object_ref: { id: "sacct_beta_1" }, label: "Beta plans",
        connection_ref: { object_type: "connection", id: "conn_beta" } },
    ],
  });
  const user = userEvent.setup();
  await chooseSheetsChannel(user);

  const grant = await screen.findByLabelText(/Google authorization/) as HTMLSelectElement;
  // Two consents, two options — not three accounts.
  expect(Array.from(grant.options, (option) => option.value).filter(Boolean))
    .toEqual(["conn_alpha", "conn_beta"]);
  expect(grant.value).toBe("");
  // Several consents: nothing is defaulted, and the hint says what is being
  // asked rather than leaving an operator hunting for the scope to pick.
  expect(screen.getByText(/token is minted from the consent itself/)).toBeInTheDocument();

  await user.selectOptions(grant, "conn_beta");

  await waitFor(() => expect(saved?.source).toEqual(
    expect.objectContaining({ source_account_ref: "sacct_beta_1" }),
  ));
});

it("no longer claims the discovery adapter is unwired", async () => {
  const user = userEvent.setup();
  await chooseSheetsChannel(user);
  expect(screen.queryByText(/Google Sheets discovery is not wired/)).not.toBeInTheDocument();
  expect(screen.queryByText(/adapter_unavailable/)).not.toBeInTheDocument();
});

it("says an authorization nobody granted is missing, and never calls it a failure", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue({ ...options, source_accounts: [] });
  await chooseSheetsChannel(user);
  expect(await screen.findByText(/granting spreadsheets.readonly/)).toBeInTheDocument();
  expect(screen.queryByText(/could not read this sheet/)).not.toBeInTheDocument();
});

it("offers the workbook's tabs as a choice instead of a name to type", async () => {
  const user = userEvent.setup();
  await chooseSheetsChannel(user);
  await user.type(screen.getByLabelText(/Sheet reference/), "SHEET_ID");
  // Before a discovery nothing has seen the workbook, so nothing is offered.
  expect(screen.queryByLabelText(/^Tab$/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  const tabs = await screen.findByLabelText(/^Tab$/);
  expect(tabs).toContainHTML("Budget");
  expect(tabs).toContainHTML("Q1");
  // Picking one writes the reference the operator would have typed — and asks
  // first, because an observation of the previous tab is already attached.
  // The asking is the console's `ConfirmDialog` (57.12), not a native
  // `window.confirm`: the sentence is the same, the dialog is the design
  // system's, and the gesture completes on its confirm button.
  await user.selectOptions(tabs, "SHEET_ID!Q1");
  const dialog = await screen.findByTestId("datastream-setup-confirm");
  expect(dialog).toHaveTextContent(/invalidates dependent evidence/);
  expect(screen.getByLabelText(/Sheet reference/)).toHaveValue("SHEET_ID");
  await user.click(screen.getByTestId("datastream-setup-confirm-accept"));
  expect(screen.getByLabelText(/Sheet reference/)).toHaveValue("SHEET_ID!Q1");
  // And the sentence that said tabs could not be browsed is gone.
  expect(screen.queryByText(/nothing lists them yet/)).not.toBeInTheDocument();
});

it("says a bounded tab list is bounded, and leaves the reference free", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    coverage: { ...observation.coverage, object_list: "truncated", objects_listed: "2", objects_observed: "310" },
    exceptions: [{ code: "object_list_truncated" }],
  });
  await chooseSheetsChannel(user);
  await user.type(screen.getByLabelText(/Sheet reference/), "SHEET_ID");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  expect(await screen.findByText(/This tab list is shorter than the workbook/)).toBeInTheDocument();
  expect(screen.getByText(/2 of 310 tabs are listed here/)).toBeInTheDocument();
  expect(screen.getByLabelText(/Sheet reference/)).not.toHaveAttribute("readonly");
});

it("says a workbook that lists no tab lists none, and offers nothing", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { ...observation.safe_metadata, objects: [] },
    coverage: { ...observation.coverage, object_list: "complete", objects_listed: "0", objects_observed: "0" },
  });
  await chooseSheetsChannel(user);
  await user.type(screen.getByLabelText(/Sheet reference/), "SHEET_ID");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  expect(await screen.findByText(/This workbook lists no tab to choose from/)).toBeInTheDocument();
  expect(screen.queryByText(/tabs are listed here/)).not.toBeInTheDocument();
});

it("lists the header row as columns, every one of them selectable", async () => {
  const user = userEvent.setup();
  await discoverSheet(user);
  expect(await screen.findByText("Date")).toBeInTheDocument();
  expect(screen.getByText("Spend")).toBeInTheDocument();
  // A spreadsheet column is flat by construction, so the shared container rule
  // never bites here — every row reads `Yes`, which is the right answer.
  expect(screen.getAllByText("Yes").length).toBe(2);
  expect(screen.queryByText(/cannot be selected as a column/)).not.toBeInTheDocument();
});

it("says row 1 is an assumption, and names the field that corrects it", async () => {
  const user = userEvent.setup();
  await discoverSheet(user);
  expect(await screen.findByText(/Row 1 is read as the header/)).toBeInTheDocument();
  expect(screen.getByText(/header_row in Parsing contract/)).toBeInTheDocument();
  expect(screen.getByLabelText(/Parsing contract/)).toHaveValue("header_row=1");
});

it("states no column type, and says that is a refusal rather than a gap", async () => {
  const user = userEvent.setup();
  await discoverSheet(user);
  // TWO DISTINCT SENTENCES. "Inferred from the header row" is about where the
  // COLUMNS come from; "a spreadsheet declares no type" is about the type.
  expect(await screen.findByText(/These columns are inferred from the header row/)).toBeInTheDocument();
  expect(screen.getByText(/reading values to guess one is a pull, not a description/)).toBeInTheDocument();
  expect(screen.getByText(/no metric is proposed from a sheet/)).toBeInTheDocument();
  expect(screen.getAllByText("unknown").length).toBe(2);
});

it("reports the grid as a bucket and says what it does not measure", async () => {
  const user = userEvent.setup();
  await discoverSheet(user);
  expect(await screen.findByText(/The grid is 1000-9999 rows tall/)).toBeInTheDocument();
  expect(screen.getByText(/how many carry data is unknown until the first import/)).toBeInTheDocument();
});

it("tells a workbook without a tab from a tab that does not exist", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { fields: [], location: null },
    coverage: { schema: "unavailable", values: "not_observed", tab: "unnamed", header: "uncertain", types: "not_inferred", grid: "unavailable" },
    exceptions: [{ code: "sheet_tab_not_named" }],
  });
  await discoverSheet(user);
  expect(await screen.findByText(/This reference names a workbook, not a tab/)).toBeInTheDocument();
  expect(screen.getByText(/four-tab workbook would then describe four different schemas/)).toBeInTheDocument();
  expect(screen.queryByText(/carries no tab with that name/)).not.toBeInTheDocument();
});

it("says a missing tab means the workbook WAS read", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { fields: [], location: "fr_FR" },
    coverage: { schema: "unavailable", values: "not_observed", tab: "not_found", header: "uncertain", types: "not_inferred", grid: "unavailable" },
    exceptions: [{ code: "tab_not_found" }],
  });
  await discoverSheet(user);
  expect(await screen.findByText(/so the authorization works/)).toBeInTheDocument();
  expect(screen.queryByText(/could not read this sheet/)).not.toBeInTheDocument();
});

it("says a sheet with no header is not a broken sheet", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockResolvedValue({
    ...observation,
    safe_metadata: { fields: [], location: "fr_FR" },
    coverage: { schema: "unavailable", values: "not_observed", tab: "named", header: "uncertain", types: "not_inferred", grid: "available" },
    exceptions: [{ code: "sheet_headers_unavailable" }],
  });
  await discoverSheet(user);
  expect(await screen.findByText(/not a broken sheet/)).toBeInTheDocument();
  // No fabricated column name took the place of the missing header.
  expect(screen.queryByText("Column A")).not.toBeInTheDocument();
  expect(screen.queryByText(/could not read this sheet/)).not.toBeInTheDocument();
});

it("says a failed discovery is a failure, and names the adapter", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.createDatastreamSetupObservation).mockRejectedValue(
    new wizardApi.WizardApiError(503, "observation_unavailable", "The Google Sheets discovery adapter (managed_feed.google_sheets.readonly.v1) could not read this sheet. Nothing was saved."),
  );
  await chooseSheetsChannel(user);
  await user.type(screen.getByLabelText(/Sheet reference/), "SHEET_ID!Budget");
  await user.click(screen.getByRole("button", { name: "Discover source" }));
  expect(await screen.findByText(/could not read this sheet/)).toBeInTheDocument();
  expect(screen.getByText(/managed_feed.google_sheets.readonly.v1/)).toBeInTheDocument();
  // Not one of the three emptinesses: this one is a fault.
  expect(screen.queryByText(/not a broken sheet/)).not.toBeInTheDocument();
  expect(screen.queryByText(/granting spreadsheets.readonly/)).not.toBeInTheDocument();
});
