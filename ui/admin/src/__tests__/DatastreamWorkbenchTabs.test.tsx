/**
 * What each tab claims, pinned -- the six unconditional ones and the tab a
 * capability opens (story 58.6).
 *
 * Story 47.5 deleted six test files and replaced them with three tests in
 * `DatastreamWorkbenchRoute.test.tsx`, while the completeness ledger closed
 * Datastream criteria 6 to 9 on that basis — finding M-5. These tests cover what
 * the tabs claim rather than that they render: Mapping publishes physical
 * bindings, Processing shows an ordered chain, Data exposes profile and coverage
 * evidence rather than a count of its keys, and every one of them stays honest
 * when the evidence is absent.
 *
 * `fetch` is stubbed rather than `apiFetch`: the seam guard
 * (`apiSeamGuard.test.ts`) is what proves the bearer is attached, and stubbing
 * one level lower would hide it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import { parsePath } from "../shell/router";
// Story 60.3: the step's owner reference is judged by the SAME two functions the
// shell judges it with, so the test cannot pass on a reference `openOwner` drops.
import { findSection, sectionOwnsLens } from "../shell/navigation";
import type { OwnerReference } from "../shell/pages/ProjectSettings";
import type { Tab } from "../shell/pages/datastreamTabs";

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_1",
    project_id: "project_1",
    name: "Orders",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "shopify",
    source_account_ref: "acct_1",
    declared_writer: null,
    business_domains: [{ id: "domain_1", name: "Commerce", slug: "commerce" }],
  },
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Current" },
  versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: null, proposed_mapping: null },
  operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
  runs: { latest: "run_1", latest_state: "published" },
  publications: { candidate: null, current: "run_1", last_known_good: "run_0" },
  // The addresses `_compose_header` really composes
  // (`server/core/datastream_workbench.py:396-400`).
  //
  // THIS FIXTURE USED TO READ `/sources/acct_1`, `/settings`, `/governance`, and
  // two assertions below pinned two of them verbatim (AI-218). None of the three
  // is an address this console can open: `parsePath` refuses anything whose
  // first segment is not `org`, `account` or `platform` (`router.tsx:130`). So
  // the test asserted that the screen renders links to the unknown-route screen
  // — it protected the defect it existed to catch, and would have stayed green
  // the day the server started composing addresses like these for real.
  //
  // A fixture that cannot resolve is now the OTHER test in this file
  // (`falls back to naming the owner…`), where refusing to link is the asserted
  // behaviour rather than an accident of the data.
  links: {
    source: "/org/org_1/project/project_1/data/sources",
    project_settings: "/org/org_1/project/project_1/settings/general",
    governance: "/org/org_1/project/project_1/governance/master-data",
  },
  primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Stable.", tab: "processing" },
};

const MAPPING_EVIDENCE = {
  active_version: "map_1",
  versions: [
    {
      id: "map_1",
      version_number: 2,
      content_hash: "c".repeat(64),
      source_schema_hash: "s".repeat(64),
      executable: true,
      blocking_count: 1,
      created_at: "2026-07-30T08:00:00Z",
      mapping_payload: {
        grain: ["date", "campaign_id"],
        joint_grain: ["campaign_id", "date"],
        fields: [
          {
            field_id: "spend_micros",
            source_identity: "spend_micros",
            // WHERE THE ROLE ACTUALLY LIVES, measured 2026-08-12 over every
            // mapping version in the base: 86 fields carry
            // `suggestion.semantic_role` and **0** carry `role`. This fixture
            // described a payload shape the product has never produced, so the
            // column read `Unknown` on real Datastreams while this test stayed
            // green — the exact reason a suite can pass and prove nothing.
            // Commit 901b8794 moved the reader; this moves the fixture to match
            // the wire.
            suggestion: { semantic_role: "measure" },
            semantic_type: "INTEGER",
            canonical_target: "media_cost_micros",
            mdm_target: null,
            aggregation: "sum",
            sensitivity: "none",
            binding: { status: "bound", confidence: "high", canonical_target: "media_cost_micros", mdm_target: null },
          },
          {
            field_id: "audience_label",
            source_identity: "audience_label",
            suggestion: { semantic_role: "dimension" },
            semantic_type: "STRING",
            canonical_target: null,
            mdm_target: null,
            aggregation: null,
            sensitivity: "restricted",
            // Story 60.6: `binding.status` is the ONE authority for exclusion.
            // This fixture used to carry `included: false` beside a `blocking`
            // binding -- the two flags saying two different things about one
            // column, which is the state the story exists to end. The boolean is
            // gone; the status says what it means.
            binding: { status: "excluded", confidence: "low", canonical_target: null, mdm_target: null },
          },
        ],
      },
      // The per-column reading the server computes (story 60.6). One entry per
      // field, in payload order, carrying the treatment word and the example
      // value read from the sample already profiled into the version.
      columns: [
        {
          field_id: "spend_micros",
          treatment: "Direct",
          canonical_target: "media_cost_micros",
          mdm_target: null,
          binding_status: "bound",
          confidence: 0.94,
          sample_value: "1250000",
          contributes_to: [],
          joined_with: [],
        },
        {
          field_id: "audience_label",
          treatment: "Excluded",
          canonical_target: null,
          mdm_target: null,
          binding_status: "excluded",
          confidence: 0.2,
          sample_value: "no sample value",
          contributes_to: [],
          joined_with: [],
        },
      ],
    },
  ],
};

const PROCESSING_EVIDENCE = {
  active_version: "plan_1",
  active_mapping_version: "map_1",
  // Story 60.3: the cleanup rules that reach this Datastream, served beside the
  // plans. They are NOT part of a plan version -- a cleanup rule is governed and
  // applied at read -- which is why the step carries its own key.
  cleanup_rules: {
    state: "available",
    reason: null,
    rules: [
      {
        id: "crule_1",
        name: "Drop the test campaigns",
        source_field: "campaign_name",
        rule_kind: "exclude_row",
        enabled: true,
        scope: "project",
        dry_run_state: "passed",
        condition: "Keeps a row only when campaign_name does not match `_TEST_`.",
        updated_at: "2026-08-08T09:00:00Z",
      },
    ],
  },
  plans: [
    {
      id: "plan_1",
      version_number: 3,
      content_hash: "p".repeat(64),
      executable: true,
      validation_issues: [],
      created_at: "2026-07-30T08:00:00Z",
      normalized_payload: {
        source: {
          kind: "connector_pull",
          module: "shopify",
          report_id: "orders",
          selection: {
            selection_mode: "explicit",
            metrics: ["spend_micros", "orders"],
            dimensions: ["campaign_id"],
            grain: ["date", "campaign_id"],
            filters: [{ field: "country_code", op: "in", value: ["FR"] }],
          },
        },
        destination: { policy: "replace_window" },
        historical: { start: "2026-01-01", end_exclusive: null },
        schedule: { mode: "interval", interval_minutes: 1440, timezone: "Europe/Paris" },
        geographic: { mode: "markets", country_codes: ["FR", "MC"], markets: ["france"], compilation_status: "compiled" },
      },
    },
  ],
};

const DATA_EVIDENCE = {
  state: "available",
  sample_state: "unavailable",
  // The server's own sentence — one, with no story and no route in it (finding
  // D-7 of the visual review #69).
  sample_reason:
    "No sample and no export can be drawn here — this Datastream names no source"
    + " to read from, and the day-by-day reading above is the only reading it has.",
  availability: { collected: "available", mapped: "available", processed: "available", published: "unavailable" },
  stages: [
    {
      id: "stg_1",
      stage: "processed",
      phase_state: "succeeded",
      execution_id: "dse_1",
      plan_version_id: "plan_1",
      mapping_version_id: "map_1",
      artifact_ref: "artifact/dse_1/processed",
      schema_hash: "h".repeat(64),
      row_count: 1240,
      grain_evidence: ["date", "campaign_id"],
      profile_evidence: { null_rate: "0.00", distinct_campaigns: 12 },
      coverage_evidence: { days_expected: 30, days_present: 29, first_gap: "2026-07-04" },
      occurred_at: "2026-07-30T08:05:00Z",
      safe_error: null,
    },
  ],
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** One stub for the header plus one tab payload, the way the route loads them.
 *
 *  `header` is overridable so a test can hand the screen a header the SERVER
 *  could send and this console cannot open — which is a property of the payload,
 *  not of the tab evidence, and had no way of being expressed here. */
function stubTab(tab: string, evidence: unknown, header: unknown = HEADER) {
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith(`/${tab}`)) {
      return Promise.resolve(response({
        schema: `datastream_workbench.${tab}.v1`,
        tab,
        project_id: "project_1",
        datastream_id: "ds_1",
        evidence,
      }));
    }
    return Promise.resolve(response(header));
  }));
}

// `Tab`, not `string`: this file carried a pre-existing `tsc` error because the
// helper widened the union away, so a typo in a tab name compiled fine and only
// failed at render. Closed here rather than left, since the file is being edited.
function renderTab(tab: Tab) {
  return render(
    <DatastreamWorkbenchRoute projectId="project_1" datastreamId="ds_1" tab={tab} onNavigateTab={vi.fn()} />,
  );
}

describe("Datastream Workbench tabs", () => {
  beforeEach(() => vi.restoreAllMocks());

  /** Story 57.5 — the mount WITHOUT `tabHref`, which is the one this file has
   *  always used and the one nothing asserted.
   *
   *  When the composed `/p/{projectId}/…` addresses were removed, a mount that
   *  passes `onNavigateTab` and no address rendered six `<span aria-disabled>`
   *  with no handler: the `/debug/screen` sandbox — the screen that exists so
   *  this design can be looked at — stopped navigating, and every test here kept
   *  passing because none of them ever clicked a tab. An address is preferred,
   *  it is not the condition of the gesture. */
  it("navigates from a tab even when no address was handed down", async () => {
    const navigate = vi.fn();
    stubTab("overview", {});
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1" datastreamId="ds_1" tab="overview" onNavigateTab={navigate}
      />,
    );
    const band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    // Six contracted tabs, all of them reachable, none of them disabled — IN THE
    // RATIFIED ORDER. Amendment 3 of `datastream-workbench-and-wizard.md`,
    // « `Mapping` vient avant `Data` » — named, not numbered, because that
    // document's line numbers moved in the commit that applied it. It
    // puts `Mapping` before `Data`: a flux is understood by reading what each
    // field becomes before reading the rows. Nothing pinned the order, so the
    // console contradicted the contract silently for a day (story 58.2).
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Processing", "Runs", "Outputs",
    ]);
    fireEvent.click(band.getByRole("button", { name: "Runs" }));
    expect(navigate).toHaveBeenCalledWith("runs");
  });

  /** Story 58.6 — the CONDITIONAL tab, in the same ratified order.
   *
   *  The amendment « Une capacité activée AJOUTE son onglet » of
   *  `datastream-workbench-and-wizard.md` puts `Cost` between `Data` and
   *  `Processing` and makes it disappear entirely when `tax_fees` is off. Both
   *  halves are asserted here, on the file that has frozen this order since 58.2:
   *  an order pinned on one of the two states is an order half pinned, and the
   *  tab that moves is the one nothing was watching. */
  it("puts the capability tab in its ratified place, and nowhere when it is off", async () => {
    const withCapability = (open: boolean) => ({
      ...HEADER,
      capability_tabs: [
        { capability_key: "tax_fees", tab: "cost", state: open ? "ready" : "disabled", open },
      ],
    });

    stubTab("overview", {}, withCapability(true));
    const on = renderTab("overview");
    let band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Cost", "Processing", "Runs", "Outputs",
    ]);
    on.unmount();

    stubTab("overview", {}, withCapability(false));
    renderTab("overview");
    band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Processing", "Runs", "Outputs",
    ]);
    // Not disabled, not greyed, not a slot holding a width: absent.
    expect(band.queryByText("Cost")).toBeNull();
  });

  /** Story 61.1 — the SECOND conditional tab, and the full ratified order.
   *
   *  `Overview`, `Mapping`, `Data`, [`Cost`], [`Placements`], `Processing`,
   *  `Runs`, `Outputs` — amendment 3 of `datastream-workbench-and-wizard.md`.
   *  `Placements` was named in that order and absent from `DATASTREAM_TABS`
   *  until this story, so the contract this file freezes was written down and not
   *  held. The two conditional tabs are asserted TOGETHER because that is the
   *  only arrangement in which their relative position can be wrong.
   */
  it("seats Placements between Cost and Processing, and only while its capability is on", async () => {
    const withBoth = (placementsOpen: boolean) => ({
      ...HEADER,
      capability_tabs: [
        { capability_key: "tax_fees", tab: "cost", availability: "optional", state: "ready", open: true },
        {
          capability_key: "placement_mapping", tab: "placements", availability: "optional",
          state: placementsOpen ? "ready" : "disabled", open: placementsOpen,
        },
      ],
    });

    stubTab("overview", {}, withBoth(true));
    const on = renderTab("overview");
    let band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Cost", "Placements", "Processing", "Runs", "Outputs",
    ]);
    on.unmount();

    stubTab("overview", {}, withBoth(false));
    renderTab("overview");
    band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    // `Cost` stays, `Placements` goes: the two capabilities are independent, and
    // one band drawn from one flag would hide or show both together.
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Cost", "Processing", "Runs", "Outputs",
    ]);
    expect(band.queryByText("Placements")).toBeNull();
  });

  it("refuses the placements address on a measured closed capability, and holds an unknown one", async () => {
    const { conditionalTabIsAddressable, resetCapabilityTabs } =
      await import("../shell/capabilityTabs");
    resetCapabilityTabs();

    // Nobody has been asked yet. Refusing here would kill every bookmark into a
    // Project whose capability IS on — the screen that publishes the answer never
    // mounts.
    expect(conditionalTabIsAddressable("datastream", "placements", "project_1")).toBe(true);

    stubTab("overview", {}, {
      ...HEADER,
      capability_tabs: [
        {
          capability_key: "placement_mapping", tab: "placements", availability: "optional",
          state: "disabled", open: false,
        },
      ],
    });
    renderTab("overview");
    await screen.findByRole("navigation", { name: "Datastream" });

    // A typed `…/tab/placements` must resolve to nothing rather than to a panel
    // explaining the extinction — which is a panel.
    await waitFor(() =>
      expect(conditionalTabIsAddressable("datastream", "placements", "project_1")).toBe(false));
    resetCapabilityTabs();
  });

  /** Story 58.9 — FIVE capabilities on the header, and still no new tab.
   *
   *  Four of the five open no tab, and the header now says so with `tab: null`.
   *  A band that read `open` alone would add an unnamed entry for every active
   *  capability; the router would then be asked about an address with no tab
   *  behind it. What is asserted is that the band is unchanged and that the
   *  route contract answers the way `capabilityTabs.ts` says it does. */
  it("adds no tab for the four capabilities that open none, and waits for the answer", async () => {
    const { conditionalTabIsAddressable, resetCapabilityTabs } =
      await import("../shell/capabilityTabs");
    resetCapabilityTabs();

    // NOBODY HAS BEEN ASKED YET: `unknown` HOLDS the address rather than
    // refusing it. Refusing before the answer kills every deep link into a
    // capability that IS on, permanently — the screen that would publish the
    // answer never mounts.
    expect(conditionalTabIsAddressable("datastream", "cost", "project_1")).toBe(true);

    stubTab("overview", {}, {
      ...HEADER,
      capability_tabs: [
        { capability_key: "country", tab: null, availability: "optional", state: "ready", open: true },
        { capability_key: "currency_fx", tab: null, availability: "always_present", state: "draft", open: false },
        { capability_key: "reporting_timezone", tab: null, availability: "always_present", state: "draft", open: false },
        { capability_key: "tax_fees", tab: "cost", availability: "optional", state: "disabled", open: false },
        { capability_key: "competitors", tab: null, availability: "optional", state: "degraded", open: true },
      ],
    });
    renderTab("overview");

    const band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    // Two capabilities are ACTIVE and neither adds a tab: the band is exactly
    // the six unconditional ones.
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Processing", "Runs", "Outputs",
    ]);
    // And a MEASURED `closed` is the only thing that refuses an address.
    await waitFor(() =>
      expect(conditionalTabIsAddressable("datastream", "cost", "project_1")).toBe(false));
    resetCapabilityTabs();
  });

  it("publishes the physical mapping bindings, not a version inventory alone", async () => {
    stubTab("mapping", MAPPING_EVIDENCE);
    renderTab("mapping");

    expect(await screen.findByText("Physical bindings · map_1")).toBeInTheDocument();
    // AC5's enumerated content: identity, role, semantic type, canonical target,
    // aggregation, sensitivity, inclusion, binding state and confidence.
    // Story 60.6: `Included` became `Treatment` -- `Excluded` is one of its six
    // words, and the other five (`Direct`, `Joined`, `Split into N`, `Resolved
    // by a list`, `Not decided`) had no cell to be said in. `Example value` is
    // added beside it: 46 unfamiliar headers are unreadable without one.
    for (const column of [
      "Source identity", "Role", "Treatment", "Semantic type",
      "Canonical / MDM target", "Example value", "Aggregation", "Sensitivity",
      "Binding",
    ]) {
      expect(screen.getByRole("columnheader", { name: column })).toBeInTheDocument();
    }
    expect(screen.getByText("spend_micros")).toBeInTheDocument();
    // ONE governed target per cell — amendment 5 of the 2026-08-11 review. The
    // cell used to print `media_cost_micros` and, under it, the word `None` for
    // an MDM target that is the SAME reference under another key: measured on
    // the server side, 105 of 708 bindings carry the identity under
    // `canonical_target` and none carries two different concepts. Two lines said
    // "bound to one thing, and missing another", which was never true. And the
    // cell is now a control: this version is in force, so its concept is picked
    // here rather than read.
    // Named in the row, and held by the selector: a concept the catalog does not
    // carry stays selected rather than being dropped in silence, because a
    // vocabulary that could not be read must not unbind what is in force.
    expect(
      screen.getAllByText("media_cost_micros").some((element) => element.tagName === "SPAN"),
    ).toBe(true);
    expect(screen.getByTestId("bind-concept-spend_micros")).toHaveValue("media_cost_micros");
    expect(screen.getByText("Measure")).toBeInTheDocument();
    // A blocking binding reads as blocking, and an excluded field as excluded.
    // Scoped to the table: the triage row above it carries the same two words as
    // filter labels, which is what that row is for and not an ambiguity.
    expect(screen.getAllByText("Blocking")[0]).toBeInTheDocument();
    expect(screen.getAllByText("Excluded")[0]).toBeInTheDocument();
    expect(screen.getByText("1 blocking")).toBeInTheDocument();
    // The joint grain is named, not hashed.
    expect(screen.getByText("campaign_id, date")).toBeInTheDocument();
  });

  it("compares the candidate with what is served, instead of naming it and asking to publish", async () => {
    // The tab listed the three pointers and offered `Publish` — the operator was
    // shown an id and asked to promote it. `datastream-workbench-and-wizard.md:79`
    // requires the diff, and promoting a version you cannot compare is exactly
    // what this tab exists to make safe.
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/outputs")) {
        return Promise.resolve(response({
          schema: "datastream_workbench.outputs.v1", tab: "outputs",
          project_id: "project_1", datastream_id: "ds_1",
          evidence: {
            outputs: [
              { id: "dso_1", output_kind: "table", stable_name: "orders_daily", version_id: "dsov_cur",
                execution_id: "run_1", plan_version_id: "plan_1", mapping_version_id: "map_1",
                schema_hash: "a".repeat(64), grain_evidence: ["date", "order_id"], created_at: "2026-07-30T08:00:00Z" },
              { id: "dso_1", output_kind: "table", stable_name: "orders_daily", version_id: "dsov_cand",
                execution_id: "run_2", plan_version_id: "plan_2", mapping_version_id: "map_1",
                schema_hash: "b".repeat(64), grain_evidence: ["date", "order_id"], created_at: "2026-07-31T08:00:00Z" },
            ],
            used_by: [],
          },
        }));
      }
      return Promise.resolve(response({
        ...HEADER,
        axes: { ...HEADER.axes, publication: "Candidate" },
        publications: { candidate: "run_2", current: "run_1", last_known_good: "run_0" },
      }));
    }));
    renderTab("outputs");

    // `TableScroll` labels the scrolling REGION, not the table inside it.
    const diff = within(await screen.findByRole("region", { name: /Candidate readiness/i }));
    // Schema and plan version moved; grain and mapping version did not. Four
    // separate facts, told apart — which is the point: "the candidate differs"
    // is useless, "the schema differs and the grain does not" is a decision.
    expect(diff.getAllByText("Changed")).toHaveLength(2);
    expect(diff.getAllByText("Unchanged")).toHaveLength(2);
    // No verdict is issued: `Changed` is a fact, `acceptable` is not ours.
    expect(screen.queryByText(/safe to publish|ready to publish/i)).not.toBeInTheDocument();
  });

  it("refuses to compare a candidate whose Output evidence is missing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/outputs")) {
        return Promise.resolve(response({
          schema: "datastream_workbench.outputs.v1", tab: "outputs",
          project_id: "project_1", datastream_id: "ds_1",
          evidence: { outputs: [], used_by: [] },
        }));
      }
      return Promise.resolve(response({
        ...HEADER,
        publications: { candidate: "run_2", current: "run_1", last_known_good: "run_0" },
      }));
    }));
    renderTab("outputs");

    // A candidate pointer with no Output version behind it is a gap, and the
    // screen says so rather than rendering an empty comparison that reads as
    // "nothing changed".
    expect(await screen.findByText("Candidate evidence unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Unchanged")).not.toBeInTheDocument();
  });

  it("says WHY the next step is the next step, and which stage has no evidence", async () => {
    // The server decides the next safe step and sends its REASON with it. The
    // reason lived only in the `title` of the header button — a tooltip, which
    // is to say invisible — while the tab printed a verb and nothing else.
    const navigate = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) =>
      Promise.resolve(String(input).endsWith("/overview")
        ? response({
            schema: "datastream_workbench.overview.v1", tab: "overview",
            project_id: "project_1", datastream_id: "ds_1",
            evidence: {
              state: "available",
              schedule: { next_run_at: null, missed_run_count: 2, retry_count: 0 },
              latest_execution: {},
              stage_coverage: [{ stage: "collected" }, { stage: "mapped" }],
              downstream_count: 0,
            },
          })
        : response({
            ...HEADER,
            axes: { ...HEADER.axes, operations: "Degraded" },
            primary_action: {
              kind: "repair", label: "Repair", tab: "runs",
              reason: "Operational evidence requires an eligible repair.",
            },
          })),
    ));
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1" datastreamId="ds_1" tab="overview" onNavigateTab={navigate}
      />,
    );

    expect(await screen.findByText("Next step · Repair")).toBeInTheDocument();
    expect(screen.getByText("Operational evidence requires an eligible repair.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Go to Runs" }));
    expect(navigate).toHaveBeenCalledWith("runs");

    // `2 / 4 stages` is a fraction that refuses to say which one is missing —
    // and the missing one is the finding.
    expect(screen.getByText("No evidence: Processed, Published")).toBeInTheDocument();
  });

  it("counts what reaches governance, and lets one reading be read alone", async () => {
    // A source report brings tens to hundreds of physical fields and the table
    // listed every one in wire order. "Which ones stop me" and "which ones reach
    // governance" were both unanswerable without reading all of them.
    stubTab("mapping", MAPPING_EVIDENCE);
    renderTab("mapping");

    // `spend_micros` lands and is bound; `audience_label` is excluded, so it
    // never lands and is not counted against the reach.
    expect(await screen.findByText("1 / 1 landing fields reach a canonical or MDM target"))
      .toBeInTheDocument();

    const triage = within(screen.getByRole("radiogroup", { name: "Field binding triage" }));
    // No field in this version is both included and ungoverned, and the reading
    // is disabled rather than hidden: "0 ungoverned" is an answer.
    expect(triage.getByRole("radio", { name: /No governed target/ })).toBeDisabled();

    fireEvent.click(triage.getByRole("radio", { name: /Excluded/ }));
    const bindings = within(screen.getByRole("region", { name: "Physical field bindings" }));
    expect(bindings.getByText("audience_label")).toBeInTheDocument();
    expect(bindings.queryByText("spend_micros")).not.toBeInTheDocument();
  });

  it("says WHY a run ended, not only where it stopped", async () => {
    // The tab exists to diagnose. `error_code`, `error_detail`, the plan and
    // mapping versions, the adapter and the content hash are all sent per
    // execution, and a failed run read `Failed` and nothing else — the phase
    // timeline says where it stopped, never what stopped it.
    stubTab("runs", {
      runs: [{
        id: "exec_1", state: "failed", created_at: "2026-07-31T06:00:00Z", row_count: null,
        error_code: "quota_exhausted",
        error_detail: "Meta returned code 17: User request limit reached",
        plan_version_id: "plan_2", mapping_version_id: "map_1",
        adapter_ref: "meta-ads.insights.v23", content_hash: null, recovery: {},
      }],
      timeline: [],
    });
    renderTab("runs");

    // THE CODE EXACTLY AS IT ARRIVED — story 58.10. `titleCase` upper-cased
    // every word, so `quota_exhausted` read as "Quota Exhausted": a product
    // word for a wire value, and a code this build has never seen would have
    // read as a designed message. The raw key is what an operator can search
    // for, in the provider's own documentation and in our logs.
    expect(await screen.findByText("quota_exhausted")).toBeInTheDocument();
    expect(screen.queryByText("Quota Exhausted")).not.toBeInTheDocument();
    // Verbatim: an operator comparing the provider's message with the screen
    // must find the same string, and a summarised error is unsearchable.
    expect(screen.getByText("Meta returned code 17: User request limit reached")).toBeInTheDocument();
    // The versions that produced it — without them, "reprocess" is a guess.
    expect(screen.getByText("plan_2")).toBeInTheDocument();
    expect(screen.getByText("meta-ads.insights.v23")).toBeInTheDocument();
  });

  it("lets the operator read one data stage instead of every stage at once", async () => {
    stubTab("data", {
      stages: [
        { id: "st_2", stage: "published", phase_state: "succeeded", execution_id: "exec_1",
          plan_version_id: "plan_1", mapping_version_id: "map_1", schema_hash: "b".repeat(64),
          row_count: 9800, grain_evidence: ["date"], occurred_at: "2026-07-31T07:00:00Z" },
        { id: "st_1", stage: "collected", phase_state: "succeeded", execution_id: "exec_1",
          plan_version_id: "plan_1", mapping_version_id: "map_1", schema_hash: "a".repeat(64),
          row_count: 10000, grain_evidence: ["date"], occurred_at: "2026-07-31T06:00:00Z" },
      ],
      availability: { collected: "available", mapped: "unavailable", processed: "unavailable", published: "available" },
      sample_state: "unavailable",
      sample_reason: "No execution-scoped masked sample reference was persisted.",
      state: "available",
    });
    renderTab("data");

    const selector = within(await screen.findByRole("radiogroup", { name: "Data stage" }));
    // The progression is the reading that matters: 10,000 in, 9,800 out. Neither
    // number says it alone.
    expect(selector.getByText("10,000 rows")).toBeInTheDocument();
    expect(selector.getByText("9,800 rows")).toBeInTheDocument();
    // A stage with no evidence is not selectable — filtering to it would show an
    // empty table, which reads as "no data" rather than "this never ran".
    expect(selector.getByRole("radio", { name: /Mapped/ })).toBeDisabled();

    fireEvent.click(selector.getByRole("radio", { name: /Published/ }));
    fireEvent.click(screen.getByRole("button", { name: /Read the executions behind these counts/ }));
    const table = within(screen.getByRole("region", { name: /stage evidence/i }));
    expect(table.getByText("Published")).toBeInTheDocument();
    expect(table.queryByText("Collected")).not.toBeInTheDocument();
    // Hidden is not absent, and the screen says which it is.
    expect(screen.getByText(/hidden, not absent/i)).toBeInTheDocument();
  });

  it("says so when a mapping version carries no bindings", async () => {
    stubTab("mapping", {
      active_version: "map_1",
      versions: [{ id: "map_1", content_hash: "c".repeat(64), executable: false, mapping_payload: {} }],
    });
    renderTab("mapping");

    expect(await screen.findByText("No field bindings in this version")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Source identity" })).not.toBeInTheDocument();
  });

  it("shows the ordered processing chain and what pins each step", async () => {
    stubTab("processing", PROCESSING_EVIDENCE);
    renderTab("processing");

    expect(await screen.findByText("Ordered processing chain · plan_1")).toBeInTheDocument();
    for (const step of [
      "1 · Source or import", "2 · Parse and select", "3 · Map to governed fields",
      "4 · Capability projection", "5 · Resulting grain", "6 · Cleanup rules",
      "7 · Data quality", "8 · Output", "9 · Cadence and history",
    ]) {
      expect(screen.getByText(step)).toBeInTheDocument();
    }
    // Step 3 names the mapping version that binds; step 2 counts the selection;
    // step 5 names the grain columns; step 8 the cadence and its timezone.
    expect(screen.getByText("map_1")).toBeInTheDocument();
    expect(screen.getByText("2 metric(s), 1 dimension(s), 1 filter(s)")).toBeInTheDocument();
    expect(screen.getByText("date, campaign_id")).toBeInTheDocument();
    expect(screen.getByText("Interval · every 1440 min")).toBeInTheDocument();
    expect(screen.getByText(/Europe\/Paris/)).toBeInTheDocument();
    expect(screen.getByText(/2 country code\(s\), 1 market\(s\)/)).toBeInTheDocument();
  });

  it("shows the transformation step with NO edit control of any kind", async () => {
    // Story 60.3. `datastream-workbench-and-wizard.md:989` — "Governed rules are
    // referenced, not edited" — and `data.md:82-85` — "it does not redefine it".
    // The step therefore says what each rule does, in the words the SERVER
    // composed, and offers nothing to click that would change one.
    stubTab("processing", PROCESSING_EVIDENCE);
    renderTab("processing");

    const chain = within(await screen.findByRole("region", { name: "Ordered processing steps" }));
    expect(chain.getByText("6 · Cleanup rules")).toBeInTheDocument();
    expect(chain.getByText("1 of 1 enabled")).toBeInTheDocument();
    expect(
      chain.getByText(/Keeps a row only when campaign_name does not match `_TEST_`\./),
    ).toBeInTheDocument();
    // No control writes a rule from here: not a toggle, not a delete, not a form.
    for (const name of [/disable/i, /enable/i, /delete/i, /new cleanup rule/i, /edit/i]) {
      expect(chain.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(chain.queryByRole("textbox")).not.toBeInTheDocument();
    expect(chain.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("OPENS the Cleanup Rules lens from the step, and the reference really resolves", async () => {
    // Read-only is not the same as inert. The step's docstring calls the Owner
    // column "the one way in", and a way in that carries neither `href`, `tab`
    // nor `ownerRef` is dead text — the exact dead end this column was built to
    // end. Followed here, not merely seen.
    const opened: OwnerReference[] = [];
    stubTab("processing", PROCESSING_EVIDENCE);
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1"
        datastreamId="ds_1"
        tab="processing"
        onNavigateTab={vi.fn()}
        onOpenOwner={(owner) => opened.push(owner)}
      />,
    );

    const chain = within(await screen.findByRole("region", { name: "Ordered processing steps" }));
    fireEvent.click(chain.getByRole("button", { name: "Governance › Cleanup Rules" }));

    expect(opened).toHaveLength(1);
    expect(opened[0]).toMatchObject({
      workspace: "governance",
      section: "semantic-model",
      lens: "cleanup-rules",
      object_type: null,
      object_id: null,
      tab: null,
    });
    // And the shell's OWN validators accept it. Without these two the test would
    // prove a callback fired, not a door that opens: `openOwner` drops a
    // reference whose section is unknown, and drops the lens — landing on
    // `concepts` — when the section does not declare it (story 58.9).
    expect(findSection(opened[0].workspace!, opened[0].section!)).toBeTruthy();
    expect(sectionOwnsLens(opened[0].workspace!, opened[0].section!, opened[0].lens!)).toBe(true);
  });

  it("keeps that door open on the state where a reader most needs it", async () => {
    // A store that could not be read is precisely when someone has to go look.
    const opened: OwnerReference[] = [];
    stubTab("processing", {
      ...PROCESSING_EVIDENCE,
      cleanup_rules: { state: "unavailable", rules: [], reason: "The cleanup rules could not be read." },
    });
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1"
        datastreamId="ds_1"
        tab="processing"
        onNavigateTab={vi.fn()}
        onOpenOwner={(owner) => opened.push(owner)}
      />,
    );

    const chain = within(await screen.findByRole("region", { name: "Ordered processing steps" }));
    fireEvent.click(chain.getByRole("button", { name: "Governance › Cleanup Rules" }));
    expect(opened[0]).toMatchObject({ lens: "cleanup-rules" });
  });

  it("says the transformation step is unknown rather than empty when the store failed", async () => {
    // « Vide » and « Cassé » are two sentences here too: "no cleanup rule" would
    // be read as "nothing is being removed from my data".
    stubTab("processing", {
      ...PROCESSING_EVIDENCE,
      cleanup_rules: {
        state: "unavailable",
        rules: [],
        reason: "The cleanup rules of this Project could not be read, so the transformation step of this chain is unknown. This is not an absence of rules. (OperationalError)",
      },
    });
    renderTab("processing");

    const chain = within(await screen.findByRole("region", { name: "Ordered processing steps" }));
    expect(chain.getByText(/This is not an absence of rules/)).toBeInTheDocument();
    expect(chain.queryByText(/No cleanup rule reaches this Datastream/)).not.toBeInTheDocument();
  });

  it("says no rule reaches this Datastream when the store answered and there are none", async () => {
    stubTab("processing", {
      ...PROCESSING_EVIDENCE,
      cleanup_rules: { state: "empty", rules: [], reason: null },
    });
    renderTab("processing");

    const chain = within(await screen.findByRole("region", { name: "Ordered processing steps" }));
    expect(
      chain.getByText(/No cleanup rule reaches this Datastream/),
    ).toBeInTheDocument();
  });

  it("takes you to the surface each step names, instead of only naming it", async () => {
    // The Owner column named five surfaces as plain text: it told the reader
    // where to go and refused to take them there. An eight-step chain whose
    // every row ends in a dead end is a table of instructions.
    const navigate = vi.fn();
    stubTab("processing", PROCESSING_EVIDENCE);
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1" datastreamId="ds_1" tab="processing" onNavigateTab={navigate}
      />,
    );

    // Scoped to the chain since 57.5: the tab band is a set of controls too, so
    // "Mapping" names two of them on this screen — the step's Owner and the tab.
    // Both navigate to the same place; only one is what this test is about.
    const chain = within(await screen.findByRole("region", { name: "Ordered processing steps" }));
    fireEvent.click(chain.getByRole("button", { name: "Mapping" }));
    expect(navigate).toHaveBeenCalledWith("mapping");

    // Owners outside the Workbench route through the header's own links rather
    // than a path this page invents — and the address has to be one this console
    // can open, which is the property, not the string.
    for (const name of ["Source Account", "Controls & Quality"]) {
      const href = screen.getByRole("link", { name }).getAttribute("href") ?? "";
      expect(parsePath(href.split("?")[0], "")).toMatchObject({ kind: "resolved" });
    }
  });

  it("falls back to naming the owner when its address is one the router refuses", async () => {
    // The header's links are composed by the server, so this screen cannot vouch
    // for them. Rendered blind, a refused address is a control that looks live
    // and lands on the unknown-route screen — `/p/{project}/data/sources` did
    // exactly that on the `/debug/screen` sandbox. The owner keeps its name,
    // because "which surface owns this step" is the evidence; it just stops
    // pretending to be a way in.
    stubTab("processing", PROCESSING_EVIDENCE, {
      ...HEADER,
      links: { source: "/sources/acct_1", project_settings: "/settings", governance: "/governance" },
    });
    renderTab("processing");

    expect(await screen.findByText("Ordered processing chain · plan_1")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Source Account" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Source owner" })).not.toBeInTheDocument();
    expect(screen.getAllByText("Source Account").length).toBeGreaterThan(0);
    expect(screen.getByText("Source owner")).toBeInTheDocument();
  });

  it("names the missing mapping version instead of implying the chain is complete", async () => {
    stubTab("processing", { ...PROCESSING_EVIDENCE, active_mapping_version: null });
    renderTab("processing");

    expect(
      await screen.findByText("No active mapping version, so nothing binds the physical fields"),
    ).toBeInTheDocument();
  });

  it("reads the stage profile, coverage and grain rather than counting their keys", async () => {
    stubTab("data", DATA_EVIDENCE);
    renderTab("data");

    expect(await screen.findByText("Execution-scoped data stages")).toBeInTheDocument();
    // The evidence is the UNFOLDING of the four counts above it — finding D-6 of
    // review #69 — so it is opened, not read flat beside them.
    fireEvent.click(screen.getByRole("button", { name: /Read the executions behind these counts/ }));
    // The grain is columns, not JSON.
    expect(screen.getByText("date, campaign_id")).toBeInTheDocument();
    expect(screen.queryByText(/\[\s*"date"/)).not.toBeInTheDocument();
    // Nothing is summarised as a key count before selection…
    expect(screen.queryByText(/safe profile fields/)).not.toBeInTheDocument();
    expect(screen.queryByText(/coverage signals/)).not.toBeInTheDocument();
    // …and selecting the stage reads each field of both records.
    // Click the stage row, not the availability chip that shares its label.
    fireEvent.click(screen.getByText("dse_1"));
    expect(await screen.findByText("Processed profile and coverage")).toBeInTheDocument();
    expect(screen.getByText("Null rate")).toBeInTheDocument();
    expect(screen.getByText("Distinct campaigns")).toBeInTheDocument();
    expect(screen.getByText("Days present")).toBeInTheDocument();
    expect(screen.getByText("First gap")).toBeInTheDocument();
    expect(screen.getByText("29")).toBeInTheDocument();
  });

  it("keeps an unavailable stage and an unavailable sample honest", async () => {
    stubTab("data", {
      ...DATA_EVIDENCE,
      stages: [],
      state: "unavailable",
      availability: {
        collected: "unavailable", mapped: "unavailable",
        processed: "unavailable", published: "unavailable",
      },
    });
    renderTab("data");

    expect(await screen.findByText("Stage evidence unavailable")).toBeInTheDocument();
    // The absence of a sample costs ONE sentence, the server's, with no title
    // over it restating it — finding D-7 of review #69.
    expect(screen.getByText(DATA_EVIDENCE.sample_reason)).toBeInTheDocument();
    expect(screen.queryByText("Bounded samples unavailable")).not.toBeInTheDocument();
    // Nothing to unfold, so no control offering to.
    expect(screen.queryByRole("button", { name: /executions behind these counts/ }))
      .not.toBeInTheDocument();
    // The four stage chips remain, each saying what it is: a missing stage is
    // reported, not hidden.
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(4);
  });

  it("reports a tab error without substituting another tab's evidence", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) =>
      String(input).endsWith("/mapping")
        ? Promise.resolve(response({ message: "Mapping evidence is unavailable." }, 503))
        : Promise.resolve(response(HEADER))));
    renderTab("mapping");

    await waitFor(() =>
      expect(screen.getByText("Workbench evidence unavailable")).toBeInTheDocument());
    expect(screen.getByText("Mapping evidence is unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("Physical bindings · map_1")).not.toBeInTheDocument();
    expect(screen.queryByText("Ordered processing chain · plan_1")).not.toBeInTheDocument();
  });

  /**
   * Story 59.2 — the issue badge beside the object header, read from every tab.
   *
   * It is a band above `NavTabs`, not a prop on `ObjectHeader` (twelve files
   * mount that primitive) and not a fifth axis tile (refused in advance by the
   * `Incomplete if` of `datastream-workbench-and-wizard.md`: "lifecycle,
   * configuration, operations, run and publication states mixed into one
   * status"). The count travels on the header every tab already loads, so
   * switching tab costs no second reading of it.
   */
  const WITH_ISSUES = {
    ...HEADER,
    open_issues: {
      monitored: true,
      count: 2,
      by_severity: { blocking: 1, degrading: 1, informational: 0 },
      highest_severity: "blocking",
    },
  };

  it("shows the open issue count of the Datastream from every tab", async () => {
    for (const tab of ["overview", "mapping", "data", "processing", "runs", "outputs"] as Tab[]) {
      stubTab(tab, {}, WITH_ISSUES);
      const view = renderTab(tab);
      expect(await screen.findByText("2 open issues · blocking")).toBeInTheDocument();
      view.unmount();
    }
  });

  it("reads the count once with the header, and not again per tab", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) =>
      Promise.resolve(
        String(input).endsWith("/runs")
          ? response({
              schema: "datastream_workbench.runs.v1", tab: "runs",
              project_id: "project_1", datastream_id: "ds_1", evidence: {},
            })
          : response(WITH_ISSUES),
      ));
    vi.stubGlobal("fetch", fetchMock);
    renderTab("runs");

    expect(await screen.findByText("2 open issues · blocking")).toBeInTheDocument();
    const asked = fetchMock.mock.calls.map(([input]) => String(input));
    // The header is read ONCE and the count rides on it. No address of its own
    // exists — `screens/routes.json` does not move for story 59.2 — and the
    // `Runs` tab's own polling (story 63.5) is not this badge's cost.
    expect(asked.filter((url) => url.endsWith("/workbench"))).toHaveLength(1);
    expect(asked.some((url) => /issue/i.test(url))).toBe(false);
  });

  it("opens the runs tab from the badge instead of a floating window", async () => {
    const navigate = vi.fn();
    stubTab("overview", {}, WITH_ISSUES);
    render(
      <DatastreamWorkbenchRoute
        projectId="project_1" datastreamId="ds_1" tab="overview" onNavigateTab={navigate}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /2 open issues/ }));
    // « Un badge sur la liste mène au run, jamais à une fenêtre flottante »
    // (`datastream-workbench-and-wizard.md`, ratified amendment 5). The router's
    // grammar stops at `/tab/{tab}` and most issues name no run at all, so the
    // gesture lands on `Runs` — as close as the grammar allows, and the gap is
    // written in the story rather than closed by widening the router.
    expect(navigate).toHaveBeenCalledWith("runs");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says the count could not be read rather than showing a zero", async () => {
    // The header the server sends when the issue store could not be read: the
    // key is ABSENT. `0` here would be a clean bill of health nobody measured.
    stubTab("overview", {}, HEADER);
    renderTab("overview");

    expect(await screen.findByText("Issue count unavailable")).toBeInTheDocument();
    expect(screen.queryByText(/0 open issue/)).not.toBeInTheDocument();
    expect(screen.queryByText("No open issue")).not.toBeInTheDocument();
  });

  it("tells a watched Datastream with nothing open from an unwatched one", async () => {
    const clean = (monitored: boolean) => ({
      ...HEADER,
      open_issues: {
        monitored,
        count: 0,
        by_severity: { blocking: 0, degrading: 0, informational: 0 },
        highest_severity: null,
      },
    });

    stubTab("overview", {}, clean(true));
    const watched = renderTab("overview");
    expect(await screen.findByText("No open issue")).toBeInTheDocument();
    watched.unmount();

    stubTab("overview", {}, clean(false));
    renderTab("overview");
    // A different sentence, because it is a different fact: nothing has looked.
    expect(await screen.findByText("No monitor watches this")).toBeInTheDocument();
  });
});
