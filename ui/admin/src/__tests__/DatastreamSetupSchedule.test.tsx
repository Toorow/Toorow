/** Story 57.8 — the hour a daily pull is expected to arrive, chosen at step 6.
 *
 *  Jean, 2026-08-05: *"on pull des jours ; on permet juste de dire à quelle heure
 *  on veut que ça arrive."* Step 6 offered ONE editable field — the cadence — so
 *  the only thing a person could say about a schedule was how often it repeats,
 *  never when. `next_run_at` was then written at local midnight for every
 *  Datastream on the platform, and the setting that would have changed it did
 *  not exist on any screen.
 *
 *  WHAT THESE TESTS PIN, and why each one is here rather than assumed:
 *
 *  1. the hour is offered where the schedule is decided, not only on the
 *     Workbench a person reaches after activation;
 *  2. it travels in `input.schedule`, which is the object activation reads
 *     (`operator_input["schedule"]`) — a control that saved nowhere would look
 *     identical on screen and change nothing at all;
 *  3. it is ABSENT for the cadences that cannot honour it (A3), with a sentence
 *     saying why rather than a disabled control saying nothing;
 *  4. it appears in the right-hand summary, which is what the two-phase
 *     confirmation shows before anything is created.
 */
import { render, screen, waitFor } from "@testing-library/react";
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
    readDatastreamSetupObservation: vi.fn(),
    readDatastreamPreconfigurationProposal: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_4", current_revision: 4, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"], supported_grains: [["date"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "spend", kind: "metric", description: "Spend" }],
  }],
  managed_channels: [], external_access: [],
};

/** A draft complete enough to reopen directly on `Schedule and activate`, so the
 *  test measures that step rather than the five that precede it. */
function draftOnScheduleStep(schedule: Record<string, unknown>): wizardApi.DatastreamSetupDraft {
  return {
    ...draft,
    operator_input: {
      mode: "connector_pull",
      name: "Daily performance",
      data_role: "Spend",
      domain_ids: [],
      schedule,
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "daily" },
      configure: { date_field: "date", metrics: "spend", dimensions: "date", date_window: "", filters: "", history_intent: "", cadence_intent: "daily", grain: "date" },
      wizard_state: { active_section: 4, active_section_ref: "schedule_activate", first_incomplete: "schedule_activate" },
    },
  };
}

function savedSchedule(): Record<string, unknown> | undefined {
  const input = vi.mocked(wizardApi.updateDatastreamSetupDraft).mock.calls.at(-1)?.[3] as
    | { schedule?: Record<string, unknown> }
    | undefined;
  return input?.schedule;
}

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => ({
    ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input,
  }));
});

it("offers an arrival hour on the step where the schedule is decided", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draftOnScheduleStep({ mode: "daily" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  expect(await screen.findByLabelText("Schedule and activate section")).toBeInTheDocument();
  const field = await screen.findByLabelText(/Arrival hour/);
  // Empty, not `0`. Local midnight is a legal choice, and pre-filling it would
  // record a decision nobody made.
  expect(field).toHaveValue("");
});

it("writes the chosen hour into input.schedule, which is what activation reads", async () => {
  // The server reads `operator_input["schedule"]`; a control that lived only in
  // component state would look identical and change nothing.
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draftOnScheduleStep({ mode: "daily" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  await user.selectOptions(await screen.findByLabelText(/Arrival hour/), "6");
  await waitFor(() => expect(savedSchedule()?.arrival_hour).toBe(6));
  expect(savedSchedule()?.mode).toBe("daily");
});

it("does not offer an arrival hour to a cadence that has no single arrival", async () => {
  // A3. The grain is a DATE; an hourly cadence re-fetches the day as it fills,
  // and a manual one runs when asked. Both say so instead of showing a control
  // that would be ignored.
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draftOnScheduleStep({ mode: "hourly" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  expect(await screen.findByLabelText("Schedule and activate section")).toBeInTheDocument();
  expect(screen.queryByLabelText(/Arrival hour/)).not.toBeInTheDocument();
  expect(screen.getByText(/no single moment for the day to arrive/i)).toBeInTheDocument();
});

it("drops the hour when the cadence stops being able to honour it", async () => {
  // Otherwise the draft would carry an hour the activation silently ignores,
  // and the summary would keep displaying it.
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(
    draftOnScheduleStep({ mode: "daily", arrival_hour: 6 }),
  );
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  await user.selectOptions(await screen.findByLabelText(/^Cadence/), "hourly");
  await waitFor(() => expect(savedSchedule()?.mode).toBe("hourly"));
  expect(savedSchedule()?.arrival_hour).toBeUndefined();
});

it("shows the arrival hour in the summary the confirmation reads", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(
    draftOnScheduleStep({ mode: "daily", arrival_hour: 6 }),
  );
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  const summary = await screen.findByTestId("config-summary");
  await waitFor(() => expect(summary.textContent).toContain("Arrival hour"));
  expect(summary.textContent).toContain("06:00");
});

it("says no hour is set rather than showing midnight in the summary", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draftOnScheduleStep({ mode: "daily" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  const summary = await screen.findByTestId("config-summary");
  await waitFor(() => expect(summary.textContent).toContain("Arrival hour"));
  expect(summary.textContent).toContain("Not set");
  expect(summary.textContent).not.toContain("00:00");
});

/** AI-217 — the fourth door. Migration 204 made `weekly` legal, the Workbench
 *  offers it, the MCP tool accepts it, the REST seam writes it and the
 *  dispatcher now runs it. This step offered `manual / daily / hourly`, so the
 *  one screen where a schedule is DECIDED was the only one that could not say
 *  it — an omission that reads as "the product has no weekly cadence". */
it("offers the weekly cadence the other three doors already accept", async () => {
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draftOnScheduleStep({ mode: "daily" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  const cadence = await screen.findByLabelText(/^Cadence/);
  expect(Array.from(cadence.querySelectorAll("option")).map((option) => option.getAttribute("value")))
    .toEqual(["manual", "daily", "weekly", "hourly"]);
});

it("writes the weekly cadence into input.schedule, which is what activation reads", async () => {
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draftOnScheduleStep({ mode: "daily" }));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  await user.selectOptions(await screen.findByLabelText(/^Cadence/), "weekly");
  await waitFor(() => expect(savedSchedule()?.mode).toBe("weekly"));
});

it("keeps the arrival hour on a weekly cadence, which runs once a period", async () => {
  // A3 names `daily` and `weekly` together everywhere else: the advance reads
  // the hour for both, and the Workbench offers it to both. Dropping it here
  // would make the wizard the only door that disagrees.
  const user = userEvent.setup();
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(
    draftOnScheduleStep({ mode: "daily", arrival_hour: 6 }),
  );
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);

  const cadence = await screen.findByLabelText(/^Cadence/);
  // The recorded calls are shared by every test in this file, and the previous
  // one also saved `mode: "weekly"` — without an hour. Read only what THIS
  // interaction writes, or the assertion below passes on someone else's call.
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockClear();
  await user.selectOptions(cadence, "weekly");
  await waitFor(() => expect(savedSchedule()?.mode).toBe("weekly"));
  expect(savedSchedule()?.arrival_hour).toBe(6);
  expect(await screen.findByLabelText(/Arrival hour/)).toBeInTheDocument();
});
