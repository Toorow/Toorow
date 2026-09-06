/**
 * The `Cost` tab — story 58.6, epic 58.
 *
 * THE TAB IS THE CAPABILITY'S. `datastream-workbench-and-wizard.md`, amendment
 * « Une capacité activée AJOUTE son onglet » — named, never numbered, because
 * that document's line numbers moved in the commit that applied its predecessor.
 * Off, the capability appears NOWHERE: "ni onglet, ni panneau, ni colonne". So
 * this file mounts the screen TWICE, capability on then off, and the second
 * mount asserts three absences rather than one — no tab, no panel, no reserved
 * width — because a tab hidden while its address still opens a panel explaining
 * the extinction satisfies the letter of the amendment and none of it.
 *
 * THE NUMBERS ARE MEASURED, NOT CHOSEN. Every amount below is a row of
 * `main_marts.fee_tax_ladder_daily` for the fixture Project `feetax_dev_complete`,
 * read from the DuckDB build on 2026-08-07, and the two levels of the platform-fee
 * phase are that fixture's own rules — one laid at the Project, one at the
 * Datastream. A fixture invented to read well is the fault this repository pays
 * for most often, and it produces a screen that looks like it works.
 *
 * `fetch` is stubbed, never `apiFetch`: the seam guard (`apiSeamGuard.test.ts`)
 * is what proves the bearer is attached, and stubbing one level lower hides it.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import { DATASTREAM_TABS } from "../shell/pages/datastreamTabs";
import { parsePath } from "../shell/router";
import { resetCapabilityTabs } from "../shell/capabilityTabs";

const PROJECT = "proj_EXAMPLE";
const STREAM = "ds_EXAMPLE";
const ORG = "org_EXAMPLE";

function header(capabilityTabs: Array<{ tab: string; open: boolean; state: string }>) {
  return {
    schema: "datastream_workbench.header.v1",
    identity: {
      datastream_id: STREAM,
      project_id: PROJECT,
      name: "Paid media daily",
      mode: "connector_pull",
      data_role: "fact",
      owner: "owner@example.com",
      module: "meta-ads",
      source_account_ref: "sacct_EXAMPLE",
      declared_writer: null,
      business_domains: [],
    },
    axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Current" },
    versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: null, proposed_mapping: null },
    operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
    runs: { latest: "run_1", latest_state: "published" },
    publications: { candidate: null, current: "run_1", last_known_good: null },
    links: {
      source: `/org/${ORG}/project/${PROJECT}/data/sources`,
      project_settings: `/org/${ORG}/project/${PROJECT}/settings/general`,
      governance: `/org/${ORG}/project/${PROJECT}/governance/master-data`,
    },
    primary_action: { kind: "review", label: "Review mapping", reason: "Stable.", tab: "mapping" },
    capability_tabs: capabilityTabs.map((entry) => ({ capability_key: "tax_fees", ...entry })),
  };
}

/** The evidence of the fixture Project, phase for phase. */
const COST_EVIDENCE = {
  state: "available",
  capability: { key: "tax_fees", state: "ready", active: true },
  window: {
    start: "2026-07-09",
    end: "2026-08-07",
    days: 30,
    reason: "The cascade is read over the last 30 days.",
  },
  grain: {
    connector: "meta-ads",
    datastream_grain: false,
    ambiguous: false,
    datastreams_on_connector: null,
    reason: "The cascade is computed per connector, not per Datastream.",
  },
  currency: "EUR",
  currencies: ["EUR"],
  measures: [
    { key: "net_media", label: "Net media", unit: "micros", micros: 12345670000, percent: null, currency: "EUR", gap_code: null, reason: null },
    { key: "what_we_add", label: "What we add", unit: "micros", micros: 7593313266, percent: null, currency: "EUR", gap_code: null, reason: null },
    { key: "total", label: "Total", unit: "micros", micros: 19938983266, percent: null, currency: "EUR", gap_code: null, reason: null },
    { key: "uplift", label: "Uplift", unit: "percent", micros: null, percent: "61.5", currency: null, gap_code: null, reason: null },
  ],
  cascade: {
    aggregation: "phase",
    aggregation_reason:
      "The cascade is aggregated by PHASE, not by rule: the mart carries no relation of rule to contributed amount.",
    applied_rule_count: 5,
    steps: [
      {
        key: "platform_fee", label: "Platform fee", category: "PLATFORM_FEE",
        micros: 493826800, gap_code: null, reason: null,
        levels: [
          { kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 },
          { kind: "datastream", label: "Datastream", covers: "This Datastream alone.", rule_count: 1 },
        ],
        unresolved_rule_count: 0, unresolved_reason: null,
      },
      {
        key: "regulatory_tax", label: "Regulatory tax", category: "REGULATORY_TAX",
        micros: 0, gap_code: null, reason: null,
        levels: [], unresolved_rule_count: 0, unresolved_reason: null,
      },
      {
        key: "wht_gross_up", label: "Withholding gross-up", category: "WHT_GROSS_UP",
        micros: 2265793553, gap_code: null, reason: null,
        levels: [{ kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 }],
        unresolved_rule_count: 0, unresolved_reason: null,
      },
      {
        key: "agency_fee", label: "Agency fee", category: "AGENCY_FEE",
        micros: 1510529035, gap_code: null, reason: null,
        levels: [{ kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 }],
        unresolved_rule_count: 0, unresolved_reason: null,
      },
      {
        key: "sales_tax", label: "Sales tax", category: "SALES_TAX",
        micros: 3323163878, gap_code: null, reason: null,
        levels: [{ kind: "project", label: "Project", covers: "Every Datastream of this Project.", rule_count: 1 }],
        unresolved_rule_count: 0, unresolved_reason: null,
      },
    ],
  },
  levels: {
    rendered: [
      { kind: "project", label: "Project", covers: "Every Datastream of this Project." },
      { kind: "plan_version", label: "Plan version", covers: "One published version of a media plan. It is a version of a plan, not a storey of configuration." },
      { kind: "datastream", label: "Datastream", covers: "This Datastream alone." },
    ],
    absent: [
      { name: "Organisation", reason: "There is no organisation level: `scope_kind` admits project, plan version and Datastream, and nothing else." },
      { name: "Source category", reason: "A source category exists as data but not as a level a rule can be laid at." },
    ],
  },
  refused_rules: {
    state: "available",
    reason: null,
    rules: [
      {
        rule_key: "platform_fee_search",
        code: "source_type_out_of_scope",
        reason: "scoped to SEARCH; this Datastream is PAID_MEDIA",
      },
    ],
  },
  governance_owner_reference: {
    surface: "project", workspace: "governance", section: "controls-quality",
    object_type: "rule-set", object_id: null, tab: "rule-sets", action: null,
    version_id: null, evidence_id: null, global_surface: null, global_section: null,
  },
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** THE DOUBLE REFUSES. A double that always answers `ok: true` is how a screen
 *  ships that renders a broken read as an empty one. */
function stub(options: {
  capabilityTabs: Array<{ tab: string; open: boolean; state: string }>;
  cost?: unknown;
  costStatus?: number;
}) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/cost")) {
        const status = options.costStatus ?? 200;
        if (status !== 200) return Promise.resolve(response(options.cost, status));
        return Promise.resolve(
          response({
            schema: "datastream_workbench.cost.v1",
            tab: "cost",
            project_id: PROJECT,
            datastream_id: STREAM,
            evidence: options.cost ?? COST_EVIDENCE,
          }),
        );
      }
      if (url.endsWith("/overview")) {
        return Promise.resolve(
          response({
            schema: "datastream_workbench.overview.v1",
            tab: "overview",
            project_id: PROJECT,
            datastream_id: STREAM,
            evidence: { state: "available", stage_coverage: [], downstream_count: 0 },
          }),
        );
      }
      return Promise.resolve(response(header(options.capabilityTabs)));
    }),
  );
}

function renderTab(tab: "cost" | "overview") {
  return render(
    <DatastreamWorkbenchRoute
      projectId={PROJECT}
      datastreamId={STREAM}
      tab={tab}
      onNavigateTab={vi.fn()}
      onOpenOwner={vi.fn()}
    />,
  );
}

const OPEN = [{ tab: "cost", open: true, state: "ready" }];
const CLOSED = [{ tab: "cost", open: false, state: "disabled" }];

describe("Datastream Cost tab", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    resetCapabilityTabs();
  });
  afterEach(() => resetCapabilityTabs());

  it("shows the four measures, the cascade with its level per phase, and the refusals", async () => {
    stub({ capabilityTabs: OPEN });
    renderTab("cost");

    // The four headline measures, read from the mart and formatted from micros.
    const headline = within(await screen.findByRole("group", { name: "Cost headline measures" }));
    expect(headline.getByText("Net media")).toBeInTheDocument();
    expect(headline.getByText("12345.670000 EUR")).toBeInTheDocument();
    expect(headline.getByText("7593.313266 EUR")).toBeInTheDocument();
    expect(headline.getByText("19938.983266 EUR")).toBeInTheDocument();
    expect(headline.getByText("61.5 %")).toBeInTheDocument();

    // The cascade: five phases, each with its amount AND the levels that laid it.
    const cascade = within(screen.getByRole("region", { name: "Cost cascade phases" }));
    expect(cascade.getAllByRole("row")).toHaveLength(6); // header + five phases
    const platform = cascade.getByRole("row", { name: /Platform fee/ });
    expect(within(platform).getByText("493.826800 EUR")).toBeInTheDocument();
    // THE READING JEAN ASKED FOR: this phase was laid at two levels.
    expect(within(platform).getByText("Project · 1 rule(s)")).toBeInTheDocument();
    expect(within(platform).getByText("Datastream · 1 rule(s)")).toBeInTheDocument();
    // And a phase no rule reached says so instead of showing an empty level.
    const regulatory = cascade.getByRole("row", { name: /Regulatory tax/ });
    expect(within(regulatory).getByText("No rule reached this phase")).toBeInTheDocument();
    expect(within(regulatory).getByText("0.000000 EUR")).toBeInTheDocument();

    // The screen SAYS what it is aggregating, rather than implying a step per rule.
    expect(screen.getByText(/aggregated by PHASE, not by rule/)).toBeInTheDocument();

    // The footer: examined and not applied, with its code and its reason.
    const refusals = within(screen.getByRole("region", { name: "Refused rules" }));
    expect(refusals.getByText("platform_fee_search")).toBeInTheDocument();
    expect(refusals.getByText("source_type_out_of_scope")).toBeInTheDocument();
    expect(refusals.getByText("scoped to SEARCH; this Datastream is PAID_MEDIA")).toBeInTheDocument();
  });

  it("names the three levels the store carries and the two it does not", async () => {
    stub({ capabilityTabs: OPEN });
    renderTab("cost");

    expect(await screen.findByText("Project")).toBeInTheDocument();
    expect(screen.getByText("Plan version")).toBeInTheDocument();
    // `plan_version` is rendered for WHAT IT IS. Presenting it as a storey of
    // configuration would be the fourth level, fabricated.
    expect(screen.getByText(/not a storey of configuration/)).toBeInTheDocument();
    // The two absences, with their substance rather than a shrug.
    expect(screen.getByText("Organisation")).toBeInTheDocument();
    expect(screen.getByText(/no organisation level/)).toBeInTheDocument();
    expect(screen.getByText("Source category")).toBeInTheDocument();
    expect(screen.getByText(/exists as data but not as a level/)).toBeInTheDocument();
  });

  it("prints no rule identifier beside a phase amount", async () => {
    // THE PAYLOAD CARRIES THE IDENTIFIERS, or this assertion cannot fail.
    //
    // The first version of this test looked for `ftr_` in a fixture whose `steps`
    // held no identifier at all: it passed whatever the component rendered, which
    // is a guard that exists to be green. The mart really does carry them --
    // `applied_rule_ids` is a `STRING_AGG(rule_id, '|')` per row -- so they are put
    // where a component could reach them, at the cascade AND on each step, and the
    // assertion is that none of them is drawn beside a phase amount.
    const applied = [
      "ftr_EXAMPLE_platform", "ftr_EXAMPLE_ds", "ftr_EXAMPLE_wht",
      "ftr_EXAMPLE_agency", "ftr_EXAMPLE_vat",
    ];
    stub({
      capabilityTabs: OPEN,
      cost: {
        ...COST_EVIDENCE,
        cascade: {
          ...COST_EVIDENCE.cascade,
          applied_rule_ids: applied.join("|"),
          steps: COST_EVIDENCE.cascade.steps.map((step) => ({
            ...step,
            rule_ids: step.key === "platform_fee" ? applied.slice(0, 2) : [],
          })),
        },
      },
    });
    renderTab("cost");

    const cascade = within(await screen.findByRole("region", { name: "Cost cascade phases" }));
    // A name next to a number invites reading the number as that rule's
    // contribution, and a false attribution is worse than an aggregation that
    // admits what it is (arbitrage 3).
    for (const ruleId of [...applied, "ftr_"]) {
      expect(cascade.queryByText(new RegExp(ruleId))).toBeNull();
    }
    // What the phase DOES carry is the level and a count of rules.
    expect(cascade.getAllByText("Project · 1 rule(s)").length).toBeGreaterThan(0);
    // And the footer, where identifiers are legitimate BECAUSE nothing there
    // carries an amount, still names its refused rule.
    const refusals = within(screen.getByRole("region", { name: "Refused rules" }));
    expect(refusals.getByText("platform_fee_search")).toBeInTheDocument();
    expect(refusals.queryByText(/EUR/)).toBeNull();
  });

  it("says a total is absent rather than showing a zero", async () => {
    stub({
      capabilityTabs: OPEN,
      cost: {
        ...COST_EVIDENCE,
        measures: [
          COST_EVIDENCE.measures[0],
          { ...COST_EVIDENCE.measures[1], micros: null, gap_code: "phase_not_evaluated", reason: "At least one phase could not be evaluated on this window." },
          { ...COST_EVIDENCE.measures[2], micros: null, gap_code: "ladder_incomplete", reason: "The ladder is incomplete on at least one row of this window, so no total is composed." },
          { ...COST_EVIDENCE.measures[3], percent: null, gap_code: "phase_not_evaluated", reason: "What is added is not known in full." },
        ],
      },
    });
    renderTab("cost");

    const headline = within(await screen.findByRole("group", { name: "Cost headline measures" }));
    expect(headline.getAllByText("Not composed")).toHaveLength(3);
    expect(headline.getByText(/no total is composed/)).toBeInTheDocument();
    // Net media is a read of a real fact and stays readable: you may look at the
    // parts, you may not read a total we could not compute.
    expect(headline.getByText("12345.670000 EUR")).toBeInTheDocument();
    // And nowhere is a `0` standing in for the absence.
    expect(headline.queryByText(/^0(\.0+)? EUR$/)).toBeNull();
  });

  it("tells an empty Project from a broken read, in two sentences that share no word", async () => {
    stub({
      capabilityTabs: OPEN,
      cost: {
        ...COST_EVIDENCE,
        state: "empty",
        empty_code: "no_rule_published",
        reason: "No fee or tax rule has been published for this Project",
        owner: "Governance",
        measures: null,
        cascade: null,
      },
    });
    const empty = renderTab("cost");
    expect(
      await screen.findByText("No fee or tax rule has been published for this Project"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Governance is where a rule is published/)).toBeInTheDocument();
    // No cascade is drawn at all: an empty table reads as "nothing is added".
    expect(screen.queryByRole("region", { name: "Cost cascade phases" })).toBeNull();
    empty.unmount();

    stub({
      capabilityTabs: OPEN,
      costStatus: 503,
      cost: { code: "cost_cascade_unavailable", message: "The cost cascade could not be read" },
    });
    renderTab("cost");
    await waitFor(() =>
      expect(screen.getByText("The cost cascade could not be read")).toBeInTheDocument());
    expect(screen.queryByText(/has been published/)).toBeNull();
  });

  it("says whose cascade it is when two Datastreams share the connector", async () => {
    stub({
      capabilityTabs: OPEN,
      cost: {
        ...COST_EVIDENCE,
        grain: {
          ...COST_EVIDENCE.grain,
          ambiguous: true,
          datastreams_on_connector: 3,
          reason: "3 Datastreams of this Project collect from meta-ads, and the cascade carries no Datastream discriminator.",
        },
      },
    });
    renderTab("cost");

    expect(
      await screen.findByText("This cascade is the connector's, not this Datastream's"),
    ).toBeInTheDocument();
    expect(screen.getByText(/3 Datastreams of this Project/)).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // The capability off: ni onglet, ni panneau, ni largeur réservée.
  // -------------------------------------------------------------------------

  it("has no tab, no panel and no reserved width when the capability is off", async () => {
    stub({ capabilityTabs: CLOSED });
    renderTab("overview");

    const band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    expect(band.queryByRole("button", { name: "Cost" })).toBeNull();
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Processing", "Runs", "Outputs",
    ]);
    // No panel, and no empty slot holding a width for one.
    expect(screen.queryByRole("region", { name: "Cost cascade phases" })).toBeNull();
    expect(screen.queryByRole("group", { name: "Cost headline measures" })).toBeNull();
    expect(screen.queryByText(/cost/i)).toBeNull();
  });

  it("shows the tab, in its ratified place, when the capability is on", async () => {
    stub({ capabilityTabs: OPEN });
    renderTab("overview");

    const band = within(await screen.findByRole("navigation", { name: "Datastream" }));
    // The order of the amendment: Overview, Mapping, Data, [Cost], Processing,
    // Runs, Outputs.
    expect(band.getAllByRole("button").map((tab) => tab.textContent)).toEqual([
      "Overview", "Mapping", "Data", "Cost", "Processing", "Runs", "Outputs",
    ]);
    expect(DATASTREAM_TABS.indexOf("cost")).toBe(3);
  });

  it("refuses the typed address of a tab whose capability is off", async () => {
    const address = `/org/${ORG}/project/${PROJECT}/data/datastreams/object/datastream/${STREAM}/tab/cost`;

    // Nobody has answered yet: the address is HELD, not refused — refusing it
    // before the answer lands would kill every deep link into a capability that
    // IS on, because the screen that would answer never mounts.
    expect(parsePath(address, "")).toMatchObject({ kind: "resolved" });

    // The Workbench loads and the Project says the capability is off.
    stub({ capabilityTabs: CLOSED });
    renderTab("overview");
    await screen.findByRole("navigation", { name: "Datastream" });

    // A URL that reaches a panel explaining the extinction IS a panel. It reaches
    // nothing at all.
    await waitFor(() =>
      expect(parsePath(address, "")).toMatchObject({
        kind: "unknown",
        reason: "Unknown object tab",
      }));

    // The tab of a capability that is ON stays addressable, and so does every
    // unconditional tab of the same object.
    expect(parsePath(address.replace("/tab/cost", "/tab/mapping"), "")).toMatchObject({
      kind: "resolved",
    });
  });
});
