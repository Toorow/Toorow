/**
 * A dimension is added FROM A SCREEN — amendment 13 of
 * `docs/product-architecture/datastream-workbench-and-wizard.md`, ratified
 * 2026-08-11.
 *
 * What these tests hold is the set of things a selector over a connector
 * catalogue can quietly get wrong, each of which reads as a working control:
 *
 *   * a selection composed from a catalogue nobody read, so the plan names a
 *     field the provider never declared;
 *   * a toggle offered on an `exact_bundle` report, which the plan validator
 *     refuses as `exact_bundle_required` — 115 of the 140 declared reports;
 *   * a grain column dropped, leaving `supported_grains` naming a column the
 *     plan no longer collects;
 *   * a confirmation that shows one symmetric diff, when an addition owes a
 *     HISTORY DEBT and a removal does not (amendment 14 is built on that
 *     asymmetry existing);
 *   * a control that vanishes on a `managed_feed` instead of answering there —
 *     4 of the 6 live Datastreams are file sources;
 *   * a write at the click, instead of one prepared, confirmed, immutable
 *     plan version through the door the Mapping tab already uses.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import WorkbenchProcessingPage from "../datastreams/workbench/pages/WorkbenchProcessingPage";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

const PLAN = {
  source: {
    kind: "connector_pull",
    module: "google-ads",
    report_id: "catalog_daily",
    selection: {
      selection_mode: "catalog_driven",
      metrics: ["clicks"],
      dimensions: ["date", "campaign_id"],
      grain: ["date", "campaign_id"],
      filters: [],
    },
  },
  destination: { policy: "managed_raw" },
  schedule: { mode: "daily", timezone: "UTC" },
  historical: {},
};

/**
 * WHAT THE COLLECTION WRITES, as `declared_collection_fields()` publishes it.
 * Never selectable: no manifest declares `loaded_at`, so a plan naming it is
 * refused `unknown_report_field` by `datastream_intents`.
 */
const COLLECTION_FIELDS = [
  {
    field_id: "loaded_at",
    label: "Collected at",
    description: "The moment the collection that brought this row in finished.",
    always_collected: true,
    selectable: false,
    reason: "No provider declares this field, so it cannot be added to a selection.",
  },
  {
    field_id: "pull_id",
    label: "Collection reference",
    description: "Which collection this row came from.",
    always_collected: true,
    selectable: false,
    reason: "No provider declares this field, so it cannot be added to a selection.",
  },
];

/** Real mart rows, as the server read them off `fact_daily_kpi`. */
const COLLECTION_OBSERVED = {
  state: "observed",
  reason: null,
  rows: [
    {
      date: "2026-07-02",
      metric: "clicks",
      breakdown_dimension: "campaign_id",
      loaded_at: "2026-07-02T02:07:31Z",
      pull_id: "pull_B",
    },
    {
      date: "2026-07-01",
      metric: "clicks",
      breakdown_dimension: "campaign_id",
      loaded_at: "2026-07-01T02:06:00Z",
      pull_id: "pull_A",
    },
  ],
  row_count: 2,
  distinct_instants: 2,
  distinct_collections: 2,
  grain_note:
    "2 distinct instant(s) over 2 collection(s): the instant belongs to the collection, not to the row -- every row of one collection carries the same one.",
};

const CATALOGUE = {
  state: "available",
  mode: "connector_pull",
  connector_ref: "google-ads",
  connector_display_name: "Google Ads",
  report_ref: "catalog_daily",
  report_display_name: "Catalog-driven daily",
  selection_mode: "catalog_driven",
  dimensions: [
    { field_id: "date", description: "Reporting day." },
    { field_id: "campaign_id", description: "Campaign ID." },
    { field_id: "campaign_name", description: "Campaign display name." },
  ],
  metrics: [
    { field_id: "clicks", description: "Number of clicks on the ads." },
    { field_id: "impressions", description: "Number of times the ads were shown." },
  ],
  supported_grains: [["date", "campaign_id"]],
  availability: { status: "selectable" },
  // What the COLLECTION writes onto every row, beside what the provider sends.
  // The server composes both keys (`core/datastream_collection_provenance.py`):
  // the declaration is free, the observation is a warehouse read.
  collection_fields: COLLECTION_FIELDS,
  collection_provenance: COLLECTION_OBSERVED,
};

function payload(evidence: Record<string, unknown>): WorkbenchTabPayload {
  return {
    schema: "datastream_workbench.processing.v1",
    tab: "processing",
    project_id: "proj_EXAMPLE",
    datastream_id: "ds_EXAMPLE",
    evidence,
  } as unknown as WorkbenchTabPayload;
}

function connectorEvidence(overrides: Record<string, unknown> = {}) {
  return {
    plans: [
      {
        id: "dsp_HEAD",
        version_number: 2,
        normalized_payload: PLAN,
        content_hash: "a".repeat(64),
        executable: true,
        validation_issues: [],
        created_at: "2026-08-01T00:00:00Z",
      },
    ],
    active_version: "dsp_HEAD",
    active_mapping_version: "dmap_1",
    raw_zone_policy: "managed_raw",
    retention_days: 30,
    cleanup_rules: { state: "empty", rules: [] },
    source_catalogue: CATALOGUE,
    ...overrides,
  };
}

function renderPage(evidence: Record<string, unknown>, mode = "connector_pull", onConfirmed = vi.fn()) {
  return render(
    <WorkbenchProcessingPage
      payload={payload(evidence)}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      mode={mode}
      onConfirmed={onConfirmed}
    />,
  );
}

describe("Processing · the source selection", () => {
  it("offers the connector's declared dimensions and metrics, described in its own words", () => {
    renderPage(connectorEvidence());
    // The catalogue is READ, never invented: each row carries the manifest's own
    // description, which is what tells a person what `campaign_id` even is.
    expect(screen.getByText("Campaign display name.")).toBeInTheDocument();
    expect(screen.getByText("Number of times the ads were shown.")).toBeInTheDocument();
    expect(screen.getByTestId("select-dimension-campaign_name")).not.toBeChecked();
    expect(screen.getByTestId("select-metric-clicks")).toBeChecked();
  });

  it("locks a dimension that establishes the grain instead of letting a plan break", () => {
    renderPage(connectorEvidence());
    // `supported_grains` is validated on its own by `datastream_intents.
    // _validate_connector`: dropping `campaign_id` would leave the grain naming
    // a column the plan no longer collects.
    expect(screen.getByTestId("select-dimension-campaign_id")).toBeDisabled();
    expect(screen.getByTestId("select-dimension-campaign_name")).not.toBeDisabled();
    expect(screen.getAllByText("Establishes the grain").length).toBe(2);
  });

  it("names the added dimension AND the removed one, and does not treat them as mirrors", () => {
    renderPage(connectorEvidence());
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    const added = screen.getByTestId("dimensions-added");
    expect(added).toHaveTextContent("campaign_name");
    // The history debt amendment 14 is built on. No day count: none is measured
    // anywhere in the repository, and a number nobody measured is an invention.
    expect(added).toHaveTextContent("days already collected were pulled without it");
    expect(screen.queryByTestId("dimensions-removed")).toBeNull();
  });

  it("says a removal is not a mirror of an addition — the rows already collected keep it", () => {
    renderPage(connectorEvidence());
    fireEvent.click(screen.getByTestId("select-metric-clicks"));
    expect(screen.getByTestId("metrics-changed")).toHaveTextContent("Removing clicks");
    // And an empty list is refused BEFORE the click, not discovered as a
    // non-executable version afterwards.
    expect(screen.getByTestId("selection-empty-list")).toBeInTheDocument();
    expect(screen.getByTestId("prepare-selection-change")).toBeDisabled();
  });

  it("leaves through the prepare/confirm door, appending one immutable plan version", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/processing/changes")) {
        return Promise.resolve(new Response(JSON.stringify({
          preparation_id: "dscp_1",
          confirmation_secret: "one-time",
          review_hash: "a".repeat(64),
          expires_in_seconds: 900,
          review: {
            diff: [{ path: "$.source", before_hash: "b".repeat(64), after_hash: "c".repeat(64) }],
            consequence: "Append immutable non-live versions and dispatch one candidate",
            expected_plan_version_id: "dsp_HEAD",
            expected_mapping_version_id: "dmap_1",
          },
        }), { status: 201, headers: { "Content-Type": "application/json" } }));
      }
      if (url.endsWith("/changes/dscp_1/confirm")) {
        return Promise.resolve(new Response(JSON.stringify({ active_versions_unchanged: true }), {
          status: 200, headers: { "Content-Type": "application/json" },
        }));
      }
      throw new Error(`Unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmed = vi.fn();
    renderPage(connectorEvidence(), "connector_pull", confirmed);

    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    // NOTHING IS WRITTEN AT THE CLICK. (The schedule panel above reads its own
    // row on mount, so the assertion names the change seam rather than counting
    // every request the tab makes.)
    const changeCalls = () =>
      fetchMock.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/workbench/"));
    expect(changeCalls()).toEqual([]);

    fireEvent.click(screen.getByTestId("prepare-selection-change"));
    // The scope is on the confirmation, counted before the act.
    expect(await screen.findByTestId("change-scope")).toHaveTextContent("1 dimension(s) added");
    fireEvent.click(screen.getByRole("button", { name: "Prepare exact review" }));
    expect(await screen.findByText("Expected active plan")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and dispatch candidate" }));
    expect(await screen.findByText("Candidate dispatched")).toBeInTheDocument();
    expect(confirmed).toHaveBeenCalled();

    expect(changeCalls()).toEqual([
      "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/workbench/processing/changes",
      "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/workbench/changes/dscp_1/confirm",
    ]);
    const prepared = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/processing/changes")) as [string, RequestInit];
    const body = JSON.parse(String(prepared[1].body)) as { proposed_payload: typeof PLAN };
    const selection = body.proposed_payload.source.selection;
    expect(selection.dimensions).toEqual(["date", "campaign_id", "campaign_name"]);
    // The grain is NOT rewritten from the dimensions: it is validated against
    // `supported_grains` on its own, and the creation wizard's rule (grain =
    // dimensions) would compose an unsupported grain on every subset.
    expect(selection.grain).toEqual(["date", "campaign_id"]);
    expect(selection.metrics).toEqual(["clicks"]);
  });

  it("refuses to offer toggles on an exact_bundle report, and says why", () => {
    renderPage(connectorEvidence({
      source_catalogue: { ...CATALOGUE, selection_mode: "exact_bundle" },
    }));
    expect(screen.getByTestId("selection-exact-bundle")).toHaveTextContent("complete bundle");
    expect(screen.getByTestId("select-dimension-campaign_name")).toBeDisabled();
    expect(screen.getByTestId("select-metric-impressions")).toBeDisabled();
  });

  it("answers on a file source instead of disappearing, and names the tab that decides its columns", () => {
    const navigate = vi.fn();
    render(
      <WorkbenchProcessingPage
        payload={payload({
          plans: [],
          active_version: null,
          cleanup_rules: { state: "empty", rules: [] },
          source_catalogue: {
            state: "no_module",
            mode: "managed_feed",
            reason: "A file source pulls no selection: the columns are the ones the file brings, so nothing is requested from a provider.",
          },
        })}
        projectId="proj_EXAMPLE"
        datastreamId="ds_EXAMPLE"
        mode="managed_feed"
        onConfirmed={vi.fn()}
        onNavigateTab={navigate}
      />,
    );
    expect(screen.getByTestId("selection-no-module")).toHaveTextContent("pulls no selection");
    fireEvent.click(screen.getByRole("button", { name: "Open Mapping" }));
    expect(navigate).toHaveBeenCalledWith("mapping");
  });

  it("says the connector catalogue could not be offered rather than showing an empty list", () => {
    renderPage(connectorEvidence({
      source_catalogue: {
        state: "report_unknown",
        mode: "connector_pull",
        connector_ref: "google-ads",
        reason: "This plan version pins report 'ghost', which connector google-ads no longer declares.",
        report_options: [{ report_ref: "catalog_daily", display_name: "Catalog-driven daily" }],
      },
    }));
    const status = screen.getByTestId("selection-unavailable");
    expect(status).toHaveTextContent("no longer declares");
    expect(status).toHaveTextContent("Catalog-driven daily");
  });

  it("changes the selection from the HEAD version when nothing is in force", () => {
    // REWRITTEN 2026-08-12. This test pinned the opposite — the prepare control
    // DISABLED when no plan version is in force — and it was right for one day:
    // `prepare_change` joined the plan on `d.current_plan_version_id`. Commit
    // `14be81ef` moved the seam to a BASE (the version in force, or the head of
    // the ledger when nothing is), and this control was not told.
    //
    // Measured that day: 6 of the 8 live Datastreams have no plan pointer, so
    // `Prepare plan change` was disabled on every one of them. Jean: « je ne
    // peux pas simplement ajouter un metric ». That was this line.
    renderPage(connectorEvidence({ active_version: null }));
    expect(screen.getByTestId("select-dimension-campaign_name")).not.toBeDisabled();
    // The fact stays — nothing is live, and confirming makes nothing live — but
    // it is a statement now, not a refusal.
    expect(screen.getByTestId("selection-no-active-plan")).toHaveTextContent("Outputs");
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    expect(screen.getByTestId("prepare-selection-change")).not.toBeDisabled();
  });

  it("offers no prepare control at all until something has been chosen", () => {
    // The other half of the same guard, and the one that must NOT move — but it
    // is stricter than a disabled button: the whole review block is absent until
    // there is a change to review. A control that exists and refuses would be a
    // gesture offered for nothing.
    renderPage(connectorEvidence({ active_version: null }));
    expect(screen.queryByTestId("prepare-selection-change")).toBeNull();
  });
});

/**
 * AMENDMENT 14 (ratified 2026-08-11): « Ajouter une dimension déclare une DETTE
 * D'HISTORIQUE, et l'écran la nomme ».
 *
 * The measurement is the server's; what these tests hold is the set of ways a
 * screen can turn a true measurement into a false sentence:
 *
 *   * counting a day from the plan IN FORCE instead of from the run that
 *     collected it — the one thing the amendment forbids by name;
 *   * folding a day whose run is unknown into the debt, so a confirmation names
 *     a spend for days nobody proved were missing anything;
 *   * summing the days of two added fields, when one re-collection of a day
 *     serves both;
 *   * rendering an unmeasurable history as "0 days missing", which is the same
 *     string a real zero produces;
 *   * offering a re-collection past a provider's bound, or promising one when no
 *     bound is declared at all;
 *   * stating the debt only AFTER the write, instead of in the confirmation.
 */
const HISTORY_DAYS = [
  // Collected under version 1, which asked for `date` and `campaign_id` only.
  { date: "2026-07-01", state: "landed", plan_version_id: "dsp_V1", landed_at: "2026-07-02T06:00:00Z" },
  { date: "2026-07-02", state: "landed", plan_version_id: "dsp_V1", landed_at: "2026-07-03T06:00:00Z" },
  // A day whose run is unknown — `pull_jobs.execution_id` is nullable — but
  // which landed BEFORE `campaign_name` could have entered any plan.
  { date: "2026-07-03", state: "landed", plan_version_id: null, landed_at: "2026-07-04T06:00:00Z" },
  // Landed nothing: it owes no dimension and must never be proposed for a spend.
  { date: "2026-07-04", state: "not_landed", plan_version_id: null, landed_at: null },
];

const HISTORY = {
  state: "measured",
  window: { from: "2026-05-12", to: "2026-08-11", days: 92 },
  basis:
    "what the plan version each run executed under asked the provider for, read from the run and never from the plan in force today",
  unmeasurable:
    "what a provider actually returned for a past day is recorded nowhere: a pull is verified by its row count, not by its columns",
  refetch_path: "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/refetch",
  max_provider_backfill_days: null,
  backfill_bound_evidence: "unavailable",
  earliest_recoverable: null,
  days: HISTORY_DAYS,
  counts: { landed: 3, landed_empty: 0, not_landed: 1, in_flight: 0, never_collected: 88 },
  plan_dimensions: { dsp_V1: ["date", "campaign_id"] },
  first_declared: { date: { created_at: "2026-06-01T00:00:00Z", version_number: 1 } },
};

describe("Processing · what an added dimension owes the past", () => {
  it("counts the days from the run that collected them, never from the plan in force", () => {
    renderPage(connectorEvidence({ dimension_history: HISTORY }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    const debt = screen.getByTestId("dimension-debt-campaign_name");
    // Three landed days, all three missing it. The fourth landed nothing.
    expect(debt).toHaveTextContent("3 collected day(s)");
    expect(debt).toHaveTextContent("no version of this plan has ever asked for it");
    // And the measure says WHAT it is, so a day count cannot be read as proof of
    // what a provider returned.
    expect(screen.getByTestId("dimension-debt-basis")).toHaveTextContent(
      "never from the plan in force today",
    );
    expect(screen.getByTestId("dimension-debt-basis")).toHaveTextContent("recorded nowhere");
  });

  it("counts a day the RUN asked for, and never one the run did not", () => {
    // `campaign_id` IS declared by version 1, so the two days it collected carry
    // it — and the third, whose run is unknown and which post-dates the
    // declaration, is left unattributed rather than counted as a debt.
    renderPage(connectorEvidence({
      dimension_history: {
        ...HISTORY,
        first_declared: {
          ...HISTORY.first_declared,
          campaign_id: { created_at: "2026-06-01T00:00:00Z", version_number: 1 },
        },
      },
      // A plan that does NOT pin `campaign_id`, so ticking it is an addition.
      plans: [{
        id: "dsp_HEAD",
        version_number: 2,
        normalized_payload: {
          ...PLAN,
          source: {
            ...PLAN.source,
            selection: { ...PLAN.source.selection, dimensions: ["date"], grain: ["date"] },
          },
        },
        content_hash: "a".repeat(64),
        executable: true,
        validation_issues: [],
        created_at: "2026-08-01T00:00:00Z",
      }],
    }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_id"));
    const debt = screen.getByTestId("dimension-debt-campaign_id");
    // Two days carried it; the unattributed one is reported apart and NOT owed.
    expect(debt).toHaveTextContent("No day of the last 92 days is known to be missing campaign_id");
    expect(debt).toHaveTextContent("1 could not be attributed to a run");
    expect(screen.queryByTestId("dimension-debt-repair")).toBeNull();
  });

  it("proposes the owed days as ONE bounded re-collection, and never sums two fields", () => {
    renderPage(connectorEvidence({ dimension_history: HISTORY }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    fireEvent.click(screen.getByTestId("select-metric-impressions"));
    const repair = screen.getByTestId("dimension-debt-repair");
    // The UNION: one re-collection of a day brings back every dimension the plan
    // then asks for, so three days stay three and never become six.
    expect(repair).toHaveTextContent("3 day(s) would have to be collected again");
    // And the ORDER is part of the answer: a day pulled before the change is in
    // force comes back exactly as it was.
    expect(repair).toHaveTextContent("publish this change from Outputs first");
    expect(screen.getByTestId("dimension-debt-recollect")).toHaveTextContent("Re-collect 3 day(s)");
  });

  it("never launches the re-collection without the confirmation that names its spend", async () => {
    // A FRESH `Response` PER CALL, and that is not a detail: a body can be read
    // once, the tab issues several requests on mount, and a shared instance made
    // the outcome sentence read "queued no pull window" for a route that had
    // queued one.
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            jobs: [{ job_id: "job_1", state: "queued", date_from: "2026-07-01", date_to: "2026-07-03" }],
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPage(connectorEvidence({ dimension_history: HISTORY }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    fireEvent.click(screen.getByTestId("dimension-debt-recollect"));

    // NOTHING IS SPENT AT THE CLICK. The dialog of story 58.4 is the one that
    // opens — same confirmation, same spend line, no second wording.
    const refetchCalls = () =>
      fetchMock.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/refetch"));
    expect(refetchCalls()).toEqual([]);
    expect(await screen.findByTestId("repull-confirm")).toBeInTheDocument();
    expect(screen.getByText(/Not measured/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("repull-go"));
    expect(await screen.findByTestId("dimension-debt-queued")).toHaveTextContent("Queued");
    expect(refetchCalls()).toEqual([
      "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/refetch",
    ]);
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/refetch"));
    const body = JSON.parse(String((call as [string, RequestInit])[1].body));
    // The days the measurement named, and only those: never the whole window.
    expect(body.dates).toEqual(["2026-07-01", "2026-07-02", "2026-07-03"]);
  });

  it("says what a provider bound will never bring back, as an answer and not a failure", () => {
    renderPage(connectorEvidence({
      dimension_history: {
        ...HISTORY,
        max_provider_backfill_days: 30,
        backfill_bound_evidence: "provider_capability",
        earliest_recoverable: "2026-07-03",
      },
    }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    const repair = screen.getByTestId("dimension-debt-repair");
    expect(repair).toHaveTextContent("2 of them fall past this provider's 30-day bound");
    expect(repair).toHaveTextContent("will never carry campaign_name");
    // Only the reachable day is offered, so the confirmation cannot spend on a
    // window the provider refuses.
    expect(screen.getByTestId("dimension-debt-recollect")).toHaveTextContent("Re-collect 1 day(s)");
  });

  it("says the reach is unknown when no connector declares a bound, rather than promising one", () => {
    // Measured 2026-08-12: 0 of the 140 declared reports fill
    // `max_provider_backfill_days`, so this is the live branch.
    renderPage(connectorEvidence({ dimension_history: HISTORY }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    expect(screen.getByTestId("dimension-debt-repair")).toHaveTextContent(
      "declares no history bound",
    );
  });

  it("states an unmeasurable history instead of rendering it as no debt at all", () => {
    renderPage(connectorEvidence({
      dimension_history: {
        state: "unreadable",
        reason: "The collection history of this Datastream could not be read, so what the days already collected carry is unknown.",
        window: { from: "2026-05-12", to: "2026-08-11", days: 92 },
      },
    }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    expect(screen.getByTestId("dimension-debt-unmeasured")).toHaveTextContent("could not be read");
    // No count, and no re-collection proposal built on a count that does not exist.
    expect(screen.queryByTestId("dimension-debt-campaign_name")).toBeNull();
    expect(screen.queryByTestId("dimension-debt-repair")).toBeNull();
  });

  it("puts the debt in the CONFIRMATION, before anything is written", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            preparation_id: "dscp_1",
            confirmation_secret: "one-time",
            review_hash: "a".repeat(64),
            expires_in_seconds: 900,
            review: {
              diff: [],
              consequence: "Append",
              expected_plan_version_id: "dsp_HEAD",
              expected_mapping_version_id: "dmap_1",
            },
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderPage(connectorEvidence({ dimension_history: HISTORY }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    fireEvent.click(screen.getByTestId("prepare-selection-change"));
    // « La confirmation dit la dette AVANT d'écrire » — not a discovery three
    // weeks later on a chart that stayed hollow.
    expect(await screen.findByTestId("change-scope")).toHaveTextContent(
      "campaign_name exists on none of the 3 collected day(s)",
    );
  });

  it("owes nothing on a mode that asks a provider for nothing", () => {
    renderPage(connectorEvidence({
      dimension_history: {
        state: "not_applicable",
        reason: "This Datastream asks a provider for nothing, so a column added to it owes no past pull.",
      },
    }));
    fireEvent.click(screen.getByTestId("select-dimension-campaign_name"));
    expect(screen.getByTestId("dimension-debt-unmeasured")).toHaveTextContent(
      "asks a provider for nothing",
    );
    expect(screen.queryByTestId("dimension-debt-repair")).toBeNull();
  });
});

/**
 * THE EXTRACTION INSTANT — Jean, 2026-08-12: « ou par exemple simplement ajouter
 * la date de l'extraction dans le report ».
 *
 * The measurement said it is already on every row (38 of the 39 connector modules
 * stamp it, 47 of the 54 staging models carry it, `fact_daily_kpi` selects
 * `MAX(loaded_at)` in each of its 34 blocks — 6861 of 6861 rows on a built mart).
 * So these tests hold the ways a screen shows that badly:
 *
 *   * a VALUE fabricated in the browser instead of read off the row;
 *   * a collection column offered in the provider's own lists, where a person
 *     would read a technical field as one their source sent;
 *   * a checkbox, which would compose a plan `unknown_report_field` refuses;
 *   * the stronger promise printed — "when this row was read" — when the measured
 *     grain is the collection;
 *   * a warehouse that could not be read rendered as "nothing was collected".
 */
describe("Processing · the extraction instant", () => {
  it("shows the instant ON A ROW, read from the mart and not composed here", () => {
    renderPage(connectorEvidence());
    // The reading comes FIRST — a person sees the value on their own data before
    // being told what it is.
    const shown = screen.getAllByTestId("collected-at-value").map((cell) => cell.textContent);
    expect(shown).toEqual(["2026-07-02T02:07:31Z", "2026-07-01T02:06:00Z"]);
    // With the collection it belongs to beside it: the instant alone cannot be
    // told apart from a per-row timestamp.
    expect(screen.getByText("pull_B")).toBeInTheDocument();
    expect(screen.getByText("pull_A")).toBeInTheDocument();
  });

  it("says the instant belongs to the COLLECTION, which is the weaker promise", () => {
    renderPage(connectorEvidence());
    // Measured, never asserted: the server counted the distinct instants against
    // the distinct collections and this renders its sentence.
    expect(screen.getByTestId("collection-grain")).toHaveTextContent(
      "belongs to the collection, not to the row",
    );
  });

  it("keeps the collection's columns OUT of the provider's lists and offers no checkbox", () => {
    renderPage(connectorEvidence());
    const group = screen.getByTestId("collection-fields");
    expect(group).toHaveTextContent("What the collection writes onto every row");
    expect(group).toHaveTextContent("Collected at");
    // NO toggle. A field the pinned report does not declare is refused by
    // `datastream_intents` as `unknown_report_field`, so a checkbox here would
    // look like a working control and compose a version nothing can execute.
    expect(screen.queryByTestId("select-dimension-loaded_at")).toBeNull();
    expect(screen.queryByTestId("select-metric-loaded_at")).toBeNull();
    expect(group.querySelectorAll("button[role='checkbox'], input[type='checkbox']").length).toBe(0);
    // And the state says it needs no adding.
    expect(screen.getAllByText("Always collected").length).toBe(2);
  });

  it("never renders an unreadable warehouse as a collection that wrote nothing", () => {
    renderPage(connectorEvidence({
      source_catalogue: {
        ...CATALOGUE,
        collection_provenance: {
          state: "unreadable",
          reason: "The warehouse could not be read.",
          rows: [],
          row_count: 0,
          distinct_instants: null,
          distinct_collections: null,
          grain_note: null,
        },
      },
    }));
    expect(screen.getByTestId("collection-unobserved")).toHaveTextContent(
      "The warehouse could not be read.",
    );
    // The grain sentence is a MEASUREMENT; with nothing measured it must not appear.
    expect(screen.queryByTestId("collection-grain")).toBeNull();
    // But the declaration survives the warehouse: the columns are written by the
    // landing code, and that stays true when nothing can be read back.
    expect(screen.getByTestId("collection-fields")).toHaveTextContent("Collected at");
  });

  it("still declares the collection's columns when the connector manifest is unreadable", () => {
    renderPage(connectorEvidence({
      source_catalogue: {
        state: "connector_unreadable",
        mode: "connector_pull",
        connector_ref: "google-ads",
        reason: "The registry could not be read for connector google-ads.",
        collection_fields: COLLECTION_FIELDS,
      },
    }));
    expect(screen.getByTestId("selection-unavailable")).toBeInTheDocument();
    // Amendment 7 again: a control that silently disappears is the defect. An
    // unreadable manifest makes the PROVIDER's catalogue unknown and leaves this
    // one exactly as certain.
    expect(screen.getByTestId("collection-fields")).toHaveTextContent("Collection reference");
    expect(screen.getByTestId("collection-unobserved")).toBeInTheDocument();
  });

  it("shows no collection group on a source that pulls from no connector", () => {
    renderPage(
      connectorEvidence({
        source_catalogue: {
          state: "no_module",
          mode: "managed_feed",
          reason: "A file source pulls no selection.",
        },
      }),
      "managed_feed",
    );
    // Nothing pulls, so nothing stamps. A group saying "always collected" over a
    // file source would be a promise no code keeps.
    expect(screen.queryByTestId("collection-fields")).toBeNull();
  });
});
