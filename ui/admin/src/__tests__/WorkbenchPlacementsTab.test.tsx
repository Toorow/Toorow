/**
 * The `Placements` tab — stories 61.1, 61.2 and 61.3.
 *
 * WHAT STORY 61.3 ADDED, and each line of it is one of its five:
 *
 *   * every match says HOW it was obtained, in the server's word — `Exact code`,
 *     `Normalized name`, `Name similarity`, `Matched by hand` — and a NUMBER
 *     appears beside the third alone, because the first two are 1.0 by
 *     construction and a tautology printed as a measurement is worse than
 *     nothing;
 *   * the word `AI` is asserted ABSENT from the DOM. `epic-61:41` calls the fuzzy
 *     tier a fuzzy AI proposal; behind it is `difflib` at 0.88;
 *   * a match written before migration 246 renders as a named absence and NEVER
 *     as `Matched by hand`, which would claim a person typed it;
 *   * `To arbitrate` is drawn only on the payload that carries its candidates,
 *     and that payload is fetched when somebody ASKS — nothing sweeps on load;
 *   * a proposed match is confirmed behind a dialog naming the count of matches
 *     that line carries BEFORE the write, and the console sends no level.
 *
 * WHAT STORY 61.2 ADDED, and each line of it is one of its five:
 *
 *   * ONE state word per plan line and per out-of-plan spend row, LABELLED BY THE
 *     SERVER — the screen used to compose four readings from a boolean, a count,
 *     a separate panel and nothing at all;
 *   * no raw `active` / `orphaned` in the DOM: those are a database lifecycle,
 *     and what a person reads is what it costs them;
 *   * no `Ambiguous` badge on a line the payload carries no candidate for — the
 *     assertion 61.3 kept, turned from "nothing computes one" into "nothing on
 *     THIS payload computes one";
 *   * `Accept as unplanned` reaches a route, behind a confirmation that names
 *     what it does NOT change and what nothing here can undo;
 *   * the two `Vide` sentences of 61.2 are the server's, and the reason a spend
 *     row is listed is the server's too — this file used to hold the only copy of
 *     that sentence while the wire carried a French token.
 *
 * WHAT THIS FILE HOLDS FROM 61.1, and each line of it is a `Refuse` of the
 * ratified contract:
 *
 *   * every line of the plan is drawn, matched or not, and the count says how
 *     many of how many — hiding the unmatched ones is what made this tab
 *     unbuildable, because the unmatched line IS the work;
 *   * a line unfolds on its campaigns and each campaign on its placements —
 *     three levels, and several placements on one line is the NORMAL case, never
 *     an ambiguity to arbitrate;
 *   * the two `Vide` sentences and the `Cassé` sentence are three different
 *     sentences, and "we could not look" never renders as "there is nothing";
 *   * a connector that declares no placement dimension is NAMED, with its
 *     reason, and offers no control — never an empty picker implying a choice;
 *   * a count nobody could read renders as such and never as `0`;
 *   * attach and detach have a control, and the control reaches a route.
 *
 * `fetch` is stubbed rather than `apiFetch`: the seam guard
 * (`apiSeamGuard.test.ts`) is what proves the bearer is attached, and stubbing
 * one level lower would hide it.
 *
 * THE FIXTURE IS THE SERVER'S OWN SHAPE. Every key below is one
 * `core/datastream_workbench_placements.read_placements_evidence` composes, and
 * `placement_id` is `cm360`'s declared dimension — one of the only two of the 39
 * manifests that declare one.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import { resetCapabilityTabs } from "../shell/capabilityTabs";

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_1",
    project_id: "project_1",
    name: "Display buy",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "cm360",
    connector: "cm360",
    source_account_ref: "acct_1",
    declared_writer: null,
    business_domains: [],
  },
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Current" },
  versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: null, proposed_mapping: null },
  operations_evidence: { next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [] },
  runs: { latest: "run_1", latest_state: "published" },
  publications: { candidate: null, current: "run_1", last_known_good: "run_0" },
  links: {
    source: "/org/org_1/project/project_1/data/sources",
    project_settings: "/org/org_1/project/project_1/settings/general",
    governance: "/org/org_1/project/project_1/governance/master-data",
  },
  primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Stable.", tab: "processing" },
  capability_tabs: [
    {
      capability_key: "placement_mapping", tab: "placements", availability: "optional",
      state: "ready", open: true,
    },
  ],
};

const PLAN_A = "1b6bd5c0-0000-4000-8000-000000000001";
const PLAN_B = "1b6bd5c0-0000-4000-8000-000000000002";

/** THREE lines, ONE of them matched. Without the two unmatched ones, the
 *  assertion that an unmatched line is drawn cannot fail. */
const EVIDENCE = {
  state: "available",
  capability: { key: "placement_mapping", state: "ready", active: true },
  reason: null,
  empty_code: null,
  owner: null,
  placement_dimension: {
    declared: true, dimension: "placement_id", source_field: "placement_id", reason: null,
  },
  plans: [
    { id: PLAN_A, name: "Brand Q3", currency: "EUR", created_at: "2026-07-01T08:00:00Z" },
    { id: PLAN_B, name: "Always-on", currency: "EUR", created_at: "2026-01-01T08:00:00Z" },
  ],
  selected_plan: { id: PLAN_A, name: "Brand Q3", currency: "EUR", created_at: "2026-07-01T08:00:00Z" },
  grain: {
    connector: "cm360",
    datastream_grain: false,
    reason_code: "datastream_scope_ambiguous",
    datastreams_on_connector: 3,
    reason:
      "This reading is the cm360 slice of the Project, not this Datastream's: the mart carries no Datastream discriminator, and 3 Datastream(s) of this Project collect from this connector.",
  },
  line_counts: { total: 3, matched: 1, nothing_awaiting_message: null },
  // Story 61.3: `ambiguous` HAS a source now, and the reason is a cost rather
  // than an absence. The engine has a route; what it does not have is a licence
  // to sweep lines × campaigns every time somebody opens the tab.
  ambiguity: {
    available: false,
    action_label: "Suggest matches",
    reason:
      "No plan line is reported as an ambiguity here: the candidate campaigns that would justify one are computed on request (core/plan_mapping_suggest.py sweeps every line against every campaign of this connector over the plan's window), and paying that sweep on every open would charge it to people who never asked. Ask for the matches to see the candidates and the arbitrations. Several placements on one line is the normal case and is never an ambiguity.",
  },
  match_level_unrecorded_reason:
    "This match was made before the level was recorded, so how it was obtained is not known. It is not a manual match: nothing states who or what proposed it.",
  lines: [
    {
      line_key: "line_display_q3", label: "Display Q3", channel: "display",
      start_date: "2026-07-01", end_date: "2026-09-30", budget: "120000.00",
      buy_mode: "cpm", is_plan_only: false,
      matching_state: "matched", matching_state_label: "Matched",
      campaigns: [
        {
          campaign_ref: "camp_EXAMPLE_1", split_weight: "1.000000", status: "active",
          status_label: "Ventilating spend",
          // Story 61.3. `similarity` on purpose: it is the ONLY level that
          // carries a number to a screen, so a fixture built on `exact` could
          // not fail the assertion that the other three print none.
          match_method: "similarity", match_method_label: "Name similarity",
          match_score: 0.91,
          // No `status` on a placement — story 61.2, arbitrage A6: it has only
          // ever held `active`, so it no longer crosses the wire.
          placements: [
            {
              id: "3f0e0000-0000-4000-8000-00000000000a",
              breakdown_dimension: "placement_id", breakdown_value: "plc_EXAMPLE_feed",
            },
            {
              id: "3f0e0000-0000-4000-8000-00000000000b",
              breakdown_dimension: "placement_id", breakdown_value: "plc_EXAMPLE_marketplace",
            },
          ],
        },
      ],
    },
    {
      line_key: "line_video_q3", label: "Video Q3", channel: "video",
      start_date: "2026-07-01", end_date: "2026-09-30", budget: "80000.00",
      buy_mode: "cpv", is_plan_only: false,
      matching_state: "unmatched", matching_state_label: "Nothing observed",
      campaigns: [],
    },
    {
      line_key: "line_search_q3", label: "Search Q3", channel: "search",
      start_date: "2026-07-01", end_date: "2026-09-30", budget: "40000.00",
      buy_mode: "cpc", is_plan_only: false,
      matching_state: "unmatched", matching_state_label: "Nothing observed",
      campaigns: [],
    },
  ],
  unmapped: {
    window: { start: "2026-07-01", end: "2026-09-30" },
    rows: [
      {
        connector: "cm360", campaign_ref: "camp_EXAMPLE_9", spend: 4210.5,
        reason: "no_match",
        reason_label: "No plan line of this plan matches this campaign.",
        matching_state: "unmatched", matching_state_label: "Outside the plan",
        decision: null,
      },
    ],
    counts: { total: 1, accepted: 0, awaiting_decision: 1 },
    reason:
      "Spend this connector reported inside the plan's window that no active match ventilates. It is a decision to take, not an error.",
    empty_message: "No spend of this connector falls outside this plan",
    reason_required_message:
      "Accepting spend as unplanned requires a reason: it is what makes the row a decision rather than a click.",
  },
  observed_placements: {
    state: "available",
    values: [
      { breakdown_dimension: "placement_id", breakdown_value: "plc_EXAMPLE_feed", row_count: 92 },
      { breakdown_dimension: "placement_id", breakdown_value: "plc_EXAMPLE_sidebar", row_count: 61 },
    ],
    reason: null,
  },
  governance_owner_reference: {
    workspace: "governance", section: "master-data", lens: null,
    object_type: null, object_id: null, tab: null, version_id: null, action: null,
  },
  // Story 61.4. Which currency each amount on this tab is in — composed by the
  // server, because three currencies exist here and deciding between them on a
  // screen would be a second authority on the question the marts answer.
  money: {
    state: "aligned",
    plan_currency: "EUR",
    spend_currency: "EUR",
    reporting_currency: "EUR",
    money_policy_version_id: "rsv_EXAMPLE_money",
    comparable: true,
    message:
      "This plan's budget and this connector's spend are both in EUR, so the two can be read against each other.",
    analyze_reference: {
      surface: "project", workspace: "analyze", section: "reports", lens: "pacing",
      global_surface: null, global_section: null,
      object_type: null, object_id: null, tab: null, action: null,
      version_id: null, evidence_id: null,
    },
    analyze_label: "Open pacing",
    analyze_reason:
      "Planned versus observed -- the consumed share, the pace against the allocation, the remainder and the extrapolation -- is an Analyze reading, with the reporting currency and its FX provenance. It is not read on this tab, because one figure with two homes is two answers. Both the MCP App and the console now carry the same Result: the context card `mediaplan_pacing` there, Analyze > Reports > Pacing here.",
    kpi_variance_reason:
      "No KPI target is stored anywhere, so no KPI variance can be computed. A media plan line carries eleven columns -- id, version, key, label, channel, start date, end date, budget, buy mode, plan-only flag, sort order -- and none of them is a target for impressions, clicks, CPM or conversions. Only the budget can be compared.",
  },
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function tabBody(evidence: unknown) {
  return {
    schema: "datastream_workbench.placements.v1",
    tab: "placements",
    project_id: "project_1",
    datastream_id: "ds_1",
    evidence,
  };
}

/** The header, the tab, and whatever the writes answer. Returns the mock so a
 *  test can read WHICH addresses were really called. */
function stub(evidence: unknown, extra?: (url: string, init?: RequestInit) => Response | null) {
  const mock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const custom = extra?.(url, init);
    if (custom) return Promise.resolve(custom);
    if (url.includes("/workbench/placements")) return Promise.resolve(response(tabBody(evidence)));
    return Promise.resolve(response(HEADER));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function renderTab(onOpenOwner?: (owner: unknown) => void) {
  return render(
    <DatastreamWorkbenchRoute
      projectId="project_1" datastreamId="ds_1" tab="placements" onNavigateTab={vi.fn()}
      // Story 61.4: the shell's resolver. When it is ABSENT the tab draws no
      // door at all — an address nothing can resolve is an absence, never a dead
      // link — which is why the two Analyze cases below pass one in.
      onOpenOwner={onOpenOwner as never}
    />,
  );
}

describe("Datastream Workbench — Placements", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    resetCapabilityTabs();
  });

  it("draws every line of the plan and says which of them this connector is attached to", async () => {
    stub(EVIDENCE);
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    // THE ASSERTION THE STORY EXISTS FOR. Three lines in the plan, three lines on
    // screen — the two nobody has matched yet are the material of this workshop,
    // and a tab that showed only the matched one would be a tab you cannot work in.
    for (const label of ["Display Q3", "Video Q3", "Search Q3"]) {
      expect(table.getByRole("button", { name: label })).toBeInTheDocument();
    }
    // ONE WORD PER LINE, and it is the server's — story 61.2. It used to be a
    // badge composed here from a boolean and a length.
    expect(table.getAllByText("Nothing observed")).toHaveLength(2);
    expect(table.getByText("Matched")).toBeInTheDocument();
    expect(table.queryByText("Not attached")).not.toBeInTheDocument();
    // And the count is the server's "1 of 3", not a length recomputed here.
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
  });

  it("never prints a raw database status, and draws no ambiguity badge before one is asked for", async () => {
    stub(EVIDENCE);
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    const detail = within(await screen.findByRole("group", { name: /Campaigns attached to Display Q3/i }));
    // `active` / `orphaned` is the lifecycle of `app.plan_line_mappings`. What a
    // person needs is what it costs them, which is whether money still flows.
    expect(detail.getByText("Ventilating spend")).toBeInTheDocument();
    expect(detail.queryByText("active")).not.toBeInTheDocument();
    expect(detail.queryByText("orphaned")).not.toBeInTheDocument();
    // STILL no badge on a line: the candidates are not on this payload, and a
    // badge with nothing behind it is a decoration. What CHANGED in story 61.3 is
    // that the reason is a cost, not "no engine calls it" — and the control that
    // pays that cost is beside the sentence.
    expect(table.queryByText(/^To arbitrate$/i)).not.toBeInTheDocument();
    expect(table.queryByText(/^Ambiguous$/i)).not.toBeInTheDocument();
    expect(screen.getByText(/computed on request/)).toBeInTheDocument();
    expect(screen.queryByText(/no caller outside its own test/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Suggest matches" })).toBeInTheDocument();
  });

  it("shows a line whose every match is orphaned as still waiting, not as matched", async () => {
    stub({
      ...EVIDENCE,
      line_counts: { total: 3, matched: 0, nothing_awaiting_message: null },
      lines: [
        {
          ...EVIDENCE.lines[0],
          matching_state: "unmatched", matching_state_label: "Nothing observed",
          campaigns: [
            {
              campaign_ref: "camp_EXAMPLE_1", split_weight: "1.000000", status: "orphaned",
              status_label: "No longer in the plan's active version", placements: [],
              // A match written before migration 246: no level at all.
              match_method: null, match_method_label: "Level not recorded", match_score: null,
            },
          ],
        },
        EVIDENCE.lines[1], EVIDENCE.lines[2],
      ],
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    // `plan_vs_actual_daily.sql` gives an orphaned match no money, so a line that
    // receives none must not read as covered.
    expect(table.getAllByText("Nothing observed")).toHaveLength(3);
    expect(screen.getByText("0 / 3")).toBeInTheDocument();

    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));
    const detail = within(await screen.findByRole("group", { name: /Campaigns attached to Display Q3/i }));
    // The campaign is still listed — it is the work — with the sentence, never the word.
    expect(detail.getByText("No longer in the plan's active version")).toBeInTheDocument();
    expect(detail.queryByText("orphaned")).not.toBeInTheDocument();
  });

  it("unfolds a line onto its campaigns and each campaign onto its placements", async () => {
    stub(EVIDENCE);
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    const detail = within(await screen.findByRole("group", { name: /Campaigns attached to Display Q3/i }));
    expect(detail.getByText("camp_EXAMPLE_1")).toBeInTheDocument();
    // The exact NUMERIC string, never a float rounded on the way to the screen.
    expect(detail.getByText("split 1.000000")).toBeInTheDocument();
    // TWO placements on one line, and neither is flagged as an ambiguity:
    // « Feed + Marketplace + Search results sur une seule ligne est le cas
    // normal, pas une ambiguïté à arbitrer » (ratified amendment 4).
    expect(detail.getByText("plc_EXAMPLE_feed")).toBeInTheDocument();
    expect(detail.getByText("plc_EXAMPLE_marketplace")).toBeInTheDocument();
    expect(detail.queryByText(/ambiguous|arbitrate|conflict/i)).not.toBeInTheDocument();
  });

  it("gives the spend no line matches its own panel, with the reason per row", async () => {
    stub(EVIDENCE);
    renderTab();

    const panel = within(await screen.findByRole("region", { name: /Unmatched spend/i }));
    expect(panel.getByText("camp_EXAMPLE_9")).toBeInTheDocument();
    // The state everybody forgets, and the only one that reveals unplanned spend.
    // THE SENTENCE IS THE SERVER'S — this file used to hold the only copy of it
    // while the wire carried `sans_mapping`.
    expect(panel.getByText("No plan line of this plan matches this campaign.")).toBeInTheDocument();
    expect(panel.getByText("Outside the plan")).toBeInTheDocument();
  });

  it("accepts an out-of-plan spend through a route, behind a confirmation that names what it does not change", async () => {
    const calls: { url: string; method?: string; body?: string }[] = [];
    const mock = stub(EVIDENCE, (url, init) => {
      calls.push({ url, method: init?.method, body: String(init?.body ?? "") });
      if (init?.method === "POST") {
        return response({ id: "7c2a0000-0000-4000-8000-00000000000a", created: true }, 201);
      }
      return null;
    });
    renderTab();

    const panel = within(await screen.findByRole("region", { name: /Unmatched spend/i }));
    fireEvent.click(panel.getByRole("button", { name: "Accept as unplanned" }));

    // THE SCOPE, WITH THE COUNT TAKEN BEFORE THE CHANGE, and what it does NOT do.
    const dialog = within(await screen.findByRole("dialog"));
    expect(
      // Story 61.4: the amount the confirmation quotes carries its currency.
      // A gesture confirmed against a bare figure is a gesture confirmed
      // against an unknown quantity.
      screen.getByText(/camp_EXAMPLE_9 carries 4,210.5 EUR of spend no line of this plan matches/),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 campaign\(s\) of this connector are waiting for a decision/)).toBeInTheDocument();
    expect(screen.getByText(/Accepting changes no amount/)).toBeInTheDocument();
    // An irreversible gesture that reads as reversible is one people take by
    // accident. AMENDED 2026-08-18: the limit is stated against the STORE that
    // enforces it — `plan_spend_decisions` has no withdrawal and its insert is
    // `ON CONFLICT DO NOTHING` — and it names no gesture, because there is none
    // to name anywhere in the product.
    expect(screen.getByText(/nothing in the product withdraws it/)).toBeInTheDocument();
    expect(
      screen.getByText(/a later decision on the same campaign returns this one rather than replacing it/),
    ).toBeInTheDocument();
    // Nothing has been written yet.
    expect(calls.some((call) => call.method === "POST")).toBe(false);

    // A reason is what makes the row a decision — refused with the SERVER'S
    // sentence, and without spending a round trip on it.
    fireEvent.click(dialog.getByRole("button", { name: "Accept as unplanned" }));
    expect(
      await screen.findByText(/requires a reason: it is what makes the row a decision/),
    ).toBeInTheDocument();
    expect(calls.some((call) => call.method === "POST")).toBe(false);

    fireEvent.change(screen.getByRole("textbox", { name: /Why this spend is accepted as unplanned/i }), {
      target: { value: "Brand takeover bought outside the plan cycle." },
    });
    fireEvent.click(dialog.getByRole("button", { name: "Accept as unplanned" }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST" && call.url.endsWith("/placements/spend-decisions"))).toBe(true));
    const written = calls.find((call) => call.method === "POST");
    expect(written?.body).toContain("camp_EXAMPLE_9");
    expect(written?.body).toContain("Brand takeover bought outside the plan cycle.");
    // The connector is NOT sent: the server reads it from `app.datastreams`.
    expect(written?.body).not.toContain("cm360");
    // And the reading is taken again, so the screen shows what the server holds.
    await waitFor(() =>
      expect(mock.mock.calls.filter(([input]) => String(input).includes("/workbench/placements?")).length)
        .toBeGreaterThan(0));
  });

  it("keeps an accepted spend listed, with its amount, and names who decided", async () => {
    stub({
      ...EVIDENCE,
      unmapped: {
        ...EVIDENCE.unmapped,
        rows: [
          {
            ...EVIDENCE.unmapped.rows[0],
            matching_state: "accepted", matching_state_label: "Accepted as unplanned",
            decision: {
              campaign_ref: "camp_EXAMPLE_9",
              reason: "Brand takeover bought outside the plan cycle.",
              decided_by: "owner@example.com",
              decided_at: "2026-08-09T10:00:00Z",
            },
          },
        ],
        counts: { total: 1, accepted: 1, awaiting_decision: 0 },
      },
    });
    renderTab();

    const panel = within(await screen.findByRole("region", { name: /Unmatched spend/i }));
    // Accepting closes a decision; it does not make unplanned money disappear.
    expect(panel.getByText("camp_EXAMPLE_9")).toBeInTheDocument();
    // Story 61.4: with its currency, and the bare figure is GONE — the tab
    // drew two amounts of two different scales side by side and named
    // neither.
    expect(panel.getByText("4,210.5 EUR")).toBeInTheDocument();
    expect(panel.queryByText("4,210.5")).toBeNull();
    expect(panel.getByText("Accepted as unplanned")).toBeInTheDocument();
    expect(panel.getByText(/owner@example.com — Brand takeover bought outside the plan cycle./)).toBeInTheDocument();
    // And a decided row offers the gesture no more.
    expect(panel.queryByRole("button", { name: "Accept as unplanned" })).not.toBeInTheDocument();
  });

  it("says no spend falls outside the plan, and keeps the window it read", async () => {
    stub({
      ...EVIDENCE,
      line_counts: { total: 3, matched: 3, nothing_awaiting_message: "No plan line of this plan is waiting for a decision" },
      unmapped: {
        ...EVIDENCE.unmapped,
        rows: [],
        counts: { total: 0, accepted: 0, awaiting_decision: 0 },
      },
    });
    renderTab();

    // Two emptinesses, two sentences, and neither shares a word with
    // "The plan-versus-actual reading could not be completed".
    expect(await screen.findByText("No spend of this connector falls outside this plan")).toBeInTheDocument();
    expect(screen.getByText(/No plan line of this plan is waiting for a decision/)).toBeInTheDocument();
    // An empty state is a MEASUREMENT: it keeps the window it looked over.
    expect(screen.getByText(/Read over 2026-07-01 → 2026-09-30/)).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("says a plan has no line for this connector, without hiding the plan's lines", async () => {
    stub({
      ...EVIDENCE,
      empty_code: "no_line_names_this_connector",
      reason: "No plan line names this connector yet",
      owner: "Governance",
      line_counts: { total: 3, matched: 0, nothing_awaiting_message: null },
      lines: EVIDENCE.lines.map((line) => ({
        ...line,
        matching_state: "unmatched", matching_state_label: "Nothing observed",
        campaigns: [],
      })),
    });
    renderTab();

    expect(await screen.findByText("No plan line names this connector yet")).toBeInTheDocument();
    // The lines are still there. « Vide » is a sentence, never a table that
    // vanishes — the three lines are exactly what has to be matched.
    const table = within(screen.getByRole("region", { name: /Plan lines and their matches/i }));
    expect(table.getAllByText("Nothing observed")).toHaveLength(3);

    /**
     * AND IT DOES NOT SEND ANYBODY TO GOVERNANCE FOR IT — 2026-08-24.
     * The sentence ended « Matching a campaign to a line is done in Governance »,
     * reading `evidence.owner`. No Governance screen matches a campaign to a plan
     * line — `governance` > `master-data` carries six lenses and not one is a
     * media plan — and this tab DOES: `Suggest matches`, then `Confirm match` on
     * `POST …/placements/matches`. The payload still carries `owner:
     * "Governance"` above, and the screen must not repeat it.
     */
    expect(screen.queryByText(/Matching a campaign to a line is done in/)).not.toBeInTheDocument();
    expect(screen.getByText(/Matching one is a gesture of this tab/)).toBeInTheDocument();
    // It names the control by the SERVER'S label, and does not duplicate it: the
    // one `Suggest matches` button on the screen is the ambiguity banner's.
    expect(screen.getByText(/use Suggest matches below/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Suggest matches" })).toHaveLength(1);
  });

  it("tells a line with no campaign to match one HERE, never in Governance", async () => {
    stub({
      ...EVIDENCE,
      empty_code: "no_line_names_this_connector",
      reason: "No plan line names this connector yet",
      owner: "Governance",
      line_counts: { total: 3, matched: 0, nothing_awaiting_message: null },
      lines: EVIDENCE.lines.map((line) => ({
        ...line,
        matching_state: "unmatched", matching_state_label: "Nothing observed",
        campaigns: [],
      })),
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    /**
     * WHAT THIS CELL USED TO SAY, AND WHY IT WAS THE SAME DEFECT TWICE.
     * « Matching a campaign to a plan line is a Governance decision; this tab
     * attaches the placements of a campaign that is already matched. » Somebody
     * unfolds a line precisely to work on it, finds no campaign, and is told the
     * act belongs to a screen that does not exist — for the act this page's own
     * two controls perform.
     */
    expect(await screen.findByText(/No cm360 campaign is attached to this line/)).toBeInTheDocument();
    expect(screen.queryByText(/is a Governance decision/)).not.toBeInTheDocument();
    expect(screen.getByText(/Match one first, on this tab/)).toBeInTheDocument();
    // The order is the real one: a placement hangs from a matched campaign.
    expect(
      screen.getByText(/Placements are attached afterwards, to a campaign that is already matched/),
    ).toBeInTheDocument();
    // And the control is still asked for ONCE on the screen.
    expect(screen.getAllByRole("button", { name: "Suggest matches" })).toHaveLength(1);
  });

  it("offers no door to a Governance media plan screen once a plan exists", async () => {
    /**
     * THE REST OF THE 67.26 LINE. Story 67.26 repaired the branch where a Project
     * has NO plan and stopped there: as soon as `plans` carried one, the header
     * still drew `Open media plans in Governance`, built from
     * `governance_owner_reference` (`governance` > `master-data`). Measured
     * 2026-08-24: `grep -rln "mediaplan" ui/admin/src/governance/` returns no
     * file, and `shell/navigation/governance.ts` gives that section six lenses —
     * Business Domains, Classifications, Products, Activities, Registries,
     * Competitor Registry. None of them is a media plan.
     *
     * `onOpenOwner` IS PASSED IN, so this proves the door is gone rather than
     * merely unresolvable: the Analyze door built from the same resolver is
     * still drawn beside it.
     */
    stub(EVIDENCE);
    const onOpenOwner = vi.fn();
    renderTab(onOpenOwner);

    expect(await screen.findByRole("region", { name: /Plan lines and their matches/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open media plans in Governance/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Governance/ })).not.toBeInTheDocument();
    // The resolver still serves the door that DOES land on a reading.
    expect(screen.getByRole("button", { name: "Open pacing" })).toBeInTheDocument();
  });

  it("says the Project has no media plan at all, and OFFERS the carrier's Workbench", async () => {
    stub({
      ...EVIDENCE,
      state: "no_plan",
      empty_code: "no_media_plan",
      reason: "No media plan has been imported for this Project",
      owner: "The Workbench of the Datastream that carries the plan",
      // L'ADRESSE, arbitrée le 2026-08-24. Le serveur la compose depuis la
      // déclaration du gabarit (`landing_target: plan_store`) ; l'écran ne la
      // devine pas.
      carriers: [{ datastream_id: "ds_EXAMPLE_PLAN", name: "Agency plan file", plan_id: null, plan_name: null }],
      plans: [],
      selected_plan: null,
      lines: null,
      line_counts: null,
      unmapped: null,
      observed_placements: null,
    });
    const onOpenOwner = vi.fn();
    renderTab(onOpenOwner);

    expect(await screen.findByText("No media plan has been imported for this Project")).toBeInTheDocument();

    // LA PORTE MÈNE AU WORKBENCH DU PORTEUR, et à son onglet `data` — l'écran
    // ratifié pour créer le plan et importer ses révisions datées.
    const door = screen.getByTestId("placements-open-carrier");
    expect(door).toHaveTextContent("Open Agency plan file");
    door.click();
    expect(onOpenOwner).toHaveBeenCalledWith(
      expect.objectContaining({
        surface: "project",
        workspace: "data",
        section: "datastreams",
        object_type: "datastream",
        object_id: "ds_EXAMPLE_PLAN",
        tab: "data",
      }),
    );

    /**
     * CE QUE CE TEST TENAIT, ET POURQUOI C'ETAIT FAUX. Il exigeait la phrase
     * << Governance is where a media plan is imported and versioned >>. Mesure
     * du 2026-08-22 : `grep -rln "mediaplan" ui/admin/src/governance/` ne rend
     * AUCUN fichier -- cet ecran n'existe pas. Le test gardait donc une
     * destination inventee, et l'ecran voisin (`PacingReport`) en nommait une
     * AUTRE, tout aussi fausse (<< the media plan API >>, un terme de plomberie).
     * Une phrase fausse est deja mauvaise ; deux qui se contredisent apprennent
     * a la personne que le produit ne sait pas lui-meme.
     */
    expect(screen.queryByText(/Governance is where a media plan is imported/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open media plans in Governance/ })).not.toBeInTheDocument();
    // LA MEME phrase que `PacingReport` -- une seule source
    // (`mediaPlanAbsence.ts`) -- et elle nomme desormais le Workbench porteur.
    expect(
      screen.getByText(/Workbench of the file-source Datastream that carries it/),
    ).toBeInTheDocument();
    // Et elle ne redit PLUS que le geste n'existe pas : il existe.
    expect(
      screen.queryByText(/Bringing one in is not yet a gesture of this console/),
    ).not.toBeInTheDocument();
    // A DIFFERENT sentence from the one above, because it is a different door.
    expect(screen.queryByText("No plan line names this connector yet")).not.toBeInTheDocument();
  });

  it("names the gesture that MAKES a carrier when the Project has none, and draws no door", async () => {
    stub({
      ...EVIDENCE,
      state: "no_plan",
      empty_code: "no_media_plan",
      reason: "No media plan has been imported for this Project",
      owner: "The Workbench of the Datastream that carries the plan",
      carriers: [],
      plans: [],
      selected_plan: null,
      lines: null,
      line_counts: null,
      unmapped: null,
      observed_placements: null,
    });
    renderTab(vi.fn());

    expect(await screen.findByText("No media plan has been imported for this Project")).toBeInTheDocument();
    // LE GESTE, PAS UNE ADRESSE INVENTEE. Cet onglet ne cree pas de Datastream,
    // donc il ne dessine aucun bouton : offrir une porte ici remettrait
    // exactement le defaut que la story 67.26 a retire de trois emplacements.
    expect(
      screen.getByText(/No Datastream of this Project reads a file as plan lines yet/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("placements-open-carrier")).not.toBeInTheDocument();
  });

  it("reports a broken reading in the server's own words, never as an empty plan", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) =>
      Promise.resolve(
        String(input).includes("/workbench/placements")
          ? response(
              {
                code: "placement_evidence_unavailable",
                message: "The plan-versus-actual reading could not be completed",
              },
              503,
            )
          : response(HEADER),
      )));
    renderTab();

    await waitFor(() =>
      expect(screen.getByText("The plan-versus-actual reading could not be completed")).toBeInTheDocument());
    // "We could not look" must never be rendered as "there is nothing".
    expect(screen.queryByText("No media plan has been imported for this Project")).not.toBeInTheDocument();
    expect(screen.queryByText("No plan line names this connector yet")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Plan lines and their matches/i })).not.toBeInTheDocument();
  });

  it("names the absence of a placement dimension and offers no control for it", async () => {
    stub({
      ...EVIDENCE,
      placement_dimension: {
        declared: false, dimension: null, source_field: null,
        reason:
          "This connector declares no placement dimension in its manifest, so no placement can be observed on it and none can be attached. Only 2 of the 39 connectors declare one.",
      },
      lines: [
        {
          ...EVIDENCE.lines[0],
          campaigns: [{
            campaign_ref: "camp_EXAMPLE_1", split_weight: "1.000000", status: "active",
            status_label: "Ventilating spend", placements: [],
            match_method: "manual", match_method_label: "Matched by hand", match_score: null,
          }],
        },
      ],
      line_counts: { total: 1, matched: 1, nothing_awaiting_message: null },
      observed_placements: { state: "not_applicable", values: [], reason: null },
    });
    renderTab();

    expect(await screen.findByText(/Only 2 of the 39 connectors declare one/)).toBeInTheDocument();
    // No empty picker implying something could be chosen, and no fabricated row.
    const table = within(screen.getByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));
    expect(screen.queryByRole("combobox", { name: /Placement to attach/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Attach placement" })).not.toBeInTheDocument();
    expect(screen.getByText("None observable")).toBeInTheDocument();
  });

  it("says the Datastream count could not be read instead of showing a zero", async () => {
    stub({
      ...EVIDENCE,
      grain: {
        ...EVIDENCE.grain,
        datastreams_on_connector: null,
        reason:
          "The number of Datastreams collecting from this connector could not be read, so it is not stated. This is not a zero.",
      },
    });
    renderTab();

    expect(await screen.findByText("Not counted")).toBeInTheDocument();
    expect(screen.getByText(/This is not a zero/)).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("reads another plan when one is chosen, instead of concatenating them", async () => {
    const mock = stub(EVIDENCE);
    renderTab();

    const selector = await screen.findByRole("combobox", { name: "Media plan" });
    // The selector exists because no Project designates an active plan — only a
    // VERSION carries `is_active` — and merging 62 plans would destroy the
    // meaning of "1 line of 3".
    expect(within(selector).getByRole("option", { name: "Always-on" })).toBeInTheDocument();

    fireEvent.change(selector, { target: { value: PLAN_B } });
    await waitFor(() =>
      expect(
        mock.mock.calls.map(([input]) => String(input)).some((url) => url.includes(`plan_id=${PLAN_B}`)),
      ).toBe(true));
  });

  it("attaches observed placements through a route, one act for however many are ticked", async () => {
    // AMENDED 2026-08-18. The control was a one-value picker, and « une ligne de
    // plan porte PLUSIEURS placements ... c'est le cas normal » is the ratified
    // reading — so the normal case cost three round trips and could stop half
    // way. It is now a set of ticks and ONE request carrying `breakdown_values`,
    // which the route runs inside a single transaction.
    const calls: { url: string; method?: string; body?: string }[] = [];
    const mock = stub(EVIDENCE, (url, init) => {
      calls.push({ url, method: init?.method, body: String(init?.body ?? "") });
      if (init?.method === "POST") {
        return response({ count: 1, attached: [{ id: "3f0e0000-0000-4000-8000-00000000000c" }] }, 201);
      }
      return null;
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    // The candidates are the values OBSERVED on the declared dimension, never a
    // free-text field: an identity nobody observed cannot be matched to a fact.
    const tick = await screen.findByRole("checkbox", {
      name: /Attach plc_EXAMPLE_sidebar to camp_EXAMPLE_1/i,
    });
    // Nothing ticked, nothing to attach — and the control says so rather than
    // sending an empty write.
    expect(screen.getByRole("button", { name: /Attach .*placement/i })).toBeDisabled();

    fireEvent.click(tick);
    // THE COUNT IS ON THE CONTROL, before the act.
    const attach = await screen.findByRole("button", { name: "Attach 1 placement(s)" });
    fireEvent.click(attach);

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST" && call.url.endsWith("/placements/attachments"))).toBe(true));
    const written = calls.find((call) => call.method === "POST");
    // THE PLURAL IS WHAT TRAVELS, even for one: one shape on the wire is one
    // shape the route has to keep honest.
    expect(written?.body).toContain("breakdown_values");
    expect(written?.body).toContain("plc_EXAMPLE_sidebar");
    // And the reading is taken again, so the screen shows what the server holds
    // rather than what this component hoped it wrote.
    await waitFor(() =>
      expect(mock.mock.calls.filter(([input]) => String(input).includes("/workbench/placements?")).length)
        .toBeGreaterThan(0));
  });

  it("confirms a detach with the count BEFORE it, and only then calls the route", async () => {
    const calls: { url: string; method?: string }[] = [];
    stub(EVIDENCE, (url, init) => {
      calls.push({ url, method: init?.method });
      if (init?.method === "DELETE") return response({ id: "3f0e0000-0000-4000-8000-00000000000a" });
      return null;
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));
    fireEvent.click((await screen.findAllByRole("button", { name: "Detach" }))[0]);

    // THE SCOPE, NAMED WITH THE COUNT BEFORE THE CHANGE. "1 placement left" after
    // the fact describes a scope nobody was asked about.
    expect(
      await screen.findByText(/Display Q3 carries 2 placement\(s\) on campaign camp_EXAMPLE_1/),
    ).toBeInTheDocument();
    // And it says what is NOT changed, because a detach that looked like it moved
    // money would never be clicked.
    expect(screen.getByText(/leaves the campaign, its split and its spend untouched/)).toBeInTheDocument();
    // Nothing has been deleted yet.
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);

    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Detach" }));
    await waitFor(() => expect(calls.some((call) => call.method === "DELETE")).toBe(true));
    const deletion = calls.find((call) => call.method === "DELETE");
    // Scoped by plan on the wire too: the identifier alone would let a guessed
    // UUID reach another Project's plan.
    expect(deletion?.url).toContain(`plan_id=${PLAN_A}`);
  });

  it("draws no panel at all when the capability is off", async () => {
    stub({
      state: "capability_inactive",
      capability: { key: "placement_mapping", state: "disabled", active: false },
      reason:
        "Placement Mapping is not active on this Project, so no media plan is read and no placement is matched.",
      empty_code: null, owner: null,
      placement_dimension: { declared: true, dimension: "placement_id", source_field: "placement_id", reason: null },
      plans: null, selected_plan: null, grain: null, lines: null, line_counts: null,
      unmapped: null, observed_placements: null, governance_owner_reference: null,
      ambiguity: null,
    });
    renderTab();

    expect(await screen.findByText("Placement Mapping is not active on this Project")).toBeInTheDocument();
    // « Éteinte, la capacité n'apparaît nulle part — ni onglet, ni panneau, ni
    // colonne ». Not an empty table that reads as "nothing is planned".
    expect(screen.queryByRole("region", { name: /Plan lines and their matches/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Media plan" })).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Story 61.3 — how a match was obtained, and the fourth state it makes
  // possible.
  // -------------------------------------------------------------------------

  it("says by which level each match was obtained, in the server's word", async () => {
    stub({
      ...EVIDENCE,
      line_counts: { total: 3, matched: 3, nothing_awaiting_message: null },
      lines: [
        {
          ...EVIDENCE.lines[0],
          campaigns: [{
            campaign_ref: "camp_EXAMPLE_1", split_weight: "1.000000", status: "active",
            status_label: "Ventilating spend", placements: [],
            match_method: "exact", match_method_label: "Exact code", match_score: null,
          }],
        },
        {
          ...EVIDENCE.lines[1],
          matching_state: "matched", matching_state_label: "Matched",
          campaigns: [{
            campaign_ref: "camp_EXAMPLE_2", split_weight: "1.000000", status: "active",
            status_label: "Ventilating spend", placements: [],
            match_method: "normalized", match_method_label: "Normalized name", match_score: null,
          }],
        },
        {
          ...EVIDENCE.lines[2],
          matching_state: "matched", matching_state_label: "Matched",
          campaigns: [{
            campaign_ref: "camp_EXAMPLE_3", split_weight: "1.000000", status: "active",
            status_label: "Ventilating spend", placements: [],
            match_method: "manual", match_method_label: "Matched by hand", match_score: null,
          }],
        },
      ],
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    for (const [line, level] of [
      ["Display Q3", "Exact code"],
      ["Video Q3", "Normalized name"],
      ["Search Q3", "Matched by hand"],
    ]) {
      fireEvent.click(table.getByRole("button", { name: line }));
      const detail = within(await screen.findByRole("group", { name: new RegExp(`Campaigns attached to ${line}`, "i") }));
      // A SCORE ON NONE OF THESE THREE. `exact` and `normalized` are 1.0 by
      // construction and `manual` has nothing to measure; printing 1.0 would
      // dress a tautology as a measurement. An EXACT text match is what proves
      // it: a badge reading "Exact code 1" would not answer to this.
      expect(detail.getByText(level)).toBeInTheDocument();
      expect(detail.queryByText(new RegExp(`${level}\\s+[\\d.]`))).not.toBeInTheDocument();
      fireEvent.click(table.getByRole("button", { name: line }));
    }
    // AND THE WORD THE PLAN USES AND THE CODE DOES NOT KEEP. Behind the fuzzy
    // tier is `difflib` at 0.88 — no model, no network call.
    expect(document.body.textContent).not.toMatch(/\bAI\b/);
  });

  it("prints a number beside the fuzzy level and beside no other", async () => {
    stub(EVIDENCE);
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    const detail = within(await screen.findByRole("group", { name: /Campaigns attached to Display Q3/i }));
    // The label and the ratio it was judged on, together, and the label is
    // `Name similarity` — never `AI proposal`.
    expect(detail.getByText("Name similarity 0.91")).toBeInTheDocument();
    expect(detail.queryByText(/AI/)).not.toBeInTheDocument();
  });

  it("renders an unrecorded level as an absence, never as a manual match", async () => {
    // The orphaned fixture is the one carrying no level: every match written
    // before migration 246 has none, which is all 49 of them.
    stub({
      ...EVIDENCE,
      line_counts: { total: 3, matched: 0, nothing_awaiting_message: null },
      lines: [
        {
          ...EVIDENCE.lines[0],
          matching_state: "unmatched", matching_state_label: "Nothing observed",
          campaigns: [{
            campaign_ref: "camp_EXAMPLE_1", split_weight: "1.000000", status: "active",
            status_label: "Ventilating spend", placements: [],
            match_method: null, match_method_label: "Level not recorded", match_score: null,
          }],
        },
        EVIDENCE.lines[1], EVIDENCE.lines[2],
      ],
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    const detail = within(await screen.findByRole("group", { name: /Campaigns attached to Display Q3/i }));
    expect(detail.getByText("Level not recorded")).toBeInTheDocument();
    expect(detail.queryByText("Matched by hand")).not.toBeInTheDocument();
    // And WHY, said once at the top rather than beside every row that has none.
    expect(screen.getByText(/It is not a manual match/)).toBeInTheDocument();
  });

  it("draws nothing at all for a level the server did not name", async () => {
    // `match_method_label` is `null` when the database holds a word this product
    // has never named — a fifth level somebody added. The screen prints neither
    // the token nor a stand-in: a fabricated word is the whole fault this axis
    // exists to refuse.
    stub({
      ...EVIDENCE,
      lines: [
        {
          ...EVIDENCE.lines[0],
          campaigns: [{
            campaign_ref: "camp_EXAMPLE_1", split_weight: "1.000000", status: "active",
            status_label: "Ventilating spend", placements: [],
            match_method: "psychic", match_method_label: null, match_score: 0.4,
          }],
        },
        EVIDENCE.lines[1], EVIDENCE.lines[2],
      ],
    });
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    fireEvent.click(table.getByRole("button", { name: "Display Q3" }));

    const detail = within(await screen.findByRole("group", { name: /Campaigns attached to Display Q3/i }));
    expect(detail.queryByText(/psychic/)).not.toBeInTheDocument();
    expect(detail.queryByText(/Matched by hand/)).not.toBeInTheDocument();
    expect(detail.queryByText(/Level not recorded/)).not.toBeInTheDocument();
    // Nor the score alone: a number with no level beside it says nothing at all.
    expect(detail.queryByText(/0\.4/)).not.toBeInTheDocument();
  });

  const SUGGESTIONS = {
    state: "available",
    capability: { key: "placement_mapping", state: "ready", active: true },
    plan_id: PLAN_A,
    connector: "cm360",
    computed_at: "2026-08-09T09:00:00Z",
    window: { start: "2026-07-01", end: "2026-09-30" },
    similarity_threshold: 0.88,
    writes: {
      any: false,
      reason:
        "Asking for matches reads the plan and the observed campaigns and writes nothing. A match exists only once a named person confirms one, and confirming replaces every match that line already carries.",
      // Amended 2026-08-24: the refusal named `Governance` for a match no
      // Governance screen makes. It names the repairing gesture instead, and
      // that gesture is a control of this tab.
      refusal_message:
        "This campaign is not among the matches proposed for this plan line, so there is no level to record for it. Ask for the matches again and confirm one of the campaigns proposed for this line.",
    },
    counts: { lines: 3, candidates: 3, lines_to_arbitrate: 1, contested_campaigns: 1 },
    ambiguity: {
      available: true,
      reason:
        "A plan line no campaign ventilates yet, with two or more candidates, is an arbitration somebody owes. So is a campaign two or more plan lines claim: the engine pairs both ways, and naming only the first side would have hidden half of them.",
    },
    empty_message: null,
    notes: [],
    lines: [
      {
        line_key: "line_video_q3", label: "Video Q3",
        matching_state: "ambiguous", matching_state_label: "To arbitrate",
        matches_today: 0,
        candidates: [
          {
            connector: "cm360", campaign_ref: "camp_EXAMPLE_4",
            match_method: "similarity", match_method_label: "Name similarity", match_score: 0.93,
            already_matched: false,
            claimed_by_line_keys: ["line_video_q3"], claimed_by_line_count: 1,
            campaign_state: null, campaign_state_label: null,
          },
          {
            connector: "cm360", campaign_ref: "camp_EXAMPLE_5",
            match_method: "exact", match_method_label: "Exact code", match_score: null,
            already_matched: false,
            claimed_by_line_keys: ["line_search_q3", "line_video_q3"], claimed_by_line_count: 2,
            campaign_state: "ambiguous", campaign_state_label: "Claimed by several plan lines",
          },
        ],
      },
      {
        line_key: "line_search_q3", label: "Search Q3",
        matching_state: "unmatched", matching_state_label: "Nothing observed",
        matches_today: 2,
        candidates: [
          {
            connector: "cm360", campaign_ref: "camp_EXAMPLE_5",
            match_method: "exact", match_method_label: "Exact code", match_score: null,
            already_matched: false,
            claimed_by_line_keys: ["line_search_q3", "line_video_q3"], claimed_by_line_count: 2,
            campaign_state: "ambiguous", campaign_state_label: "Claimed by several plan lines",
          },
        ],
      },
      {
        line_key: "line_display_q3", label: "Display Q3",
        matching_state: "matched", matching_state_label: "Matched",
        matches_today: 1, candidates: [],
      },
    ],
  };

  /** The tab, plus whatever the suggestion address answers. */
  function stubWithSuggestions(body: unknown, status = 200) {
    const calls: { url: string; method?: string; body?: string }[] = [];
    const mock = stub(EVIDENCE, (url, init) => {
      calls.push({ url, method: init?.method, body: String(init?.body ?? "") });
      if (url.includes("/placements/suggestions")) {
        return new Response(JSON.stringify(body), {
          status, headers: { "Content-Type": "application/json" },
        });
      }
      if (init?.method === "POST" && url.endsWith("/placements/matches")) {
        return response({ line_key: "line_video_q3", replaced: 0 }, 201);
      }
      return null;
    });
    return { calls, mock };
  }

  it("computes the candidates only when asked, and draws the fourth state with them", async () => {
    const { calls } = stubWithSuggestions({ evidence: SUGGESTIONS });
    renderTab();

    // NOTHING IS SWEPT ON LOAD. The cost is paid by the person who wants it.
    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    expect(calls.some((call) => call.url.includes("/placements/suggestions"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Suggest matches" }));

    const panel = within(await screen.findByRole("group", { name: /Proposed matches/i }));
    // THE BADGE AND ITS CANDIDATES, IN THE SAME PAYLOAD — acceptance 6.
    expect(panel.getByText("To arbitrate")).toBeInTheDocument();
    expect(panel.getByText("camp_EXAMPLE_4")).toBeInTheDocument();
    expect(panel.getAllByText("camp_EXAMPLE_5")).toHaveLength(2);
    // A score on the fuzzy candidate, none on the exact one.
    expect(panel.getByText("Name similarity 0.93")).toBeInTheDocument();
    expect(panel.getAllByText("Exact code")).toHaveLength(2);
    // THE OTHER SIDE OF THE AMBIGUITY — a campaign two plan lines claim.
    expect(panel.getAllByText("Claimed by several plan lines")).toHaveLength(2);
    // And the reading says it wrote nothing, over what window and how close.
    expect(screen.getByText(/writes nothing/)).toBeInTheDocument();
    expect(screen.getByText(/Read over 2026-07-01 → 2026-09-30/)).toBeInTheDocument();
    expect(screen.getByText(/at or above 0.88/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\bAI\b/);
  });

  it("confirms a proposed match behind a confirmation naming the count BEFORE it", async () => {
    const { calls } = stubWithSuggestions({ evidence: SUGGESTIONS });
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    fireEvent.click(screen.getByRole("button", { name: "Suggest matches" }));

    const panel = within(await screen.findByRole("group", { name: /Proposed matches/i }));
    // The second line carries TWO matches today: confirming rewrites them.
    fireEvent.click(panel.getAllByRole("button", { name: "Confirm match" })[2]);

    expect(
      await screen.findByText(/Search Q3 carries 2 match\(es\) today; confirming rewrites them/),
    ).toBeInTheDocument();
    // The level is NAMED before the act, and it is the server's word.
    expect(screen.getByText(/recorded as obtained by: Exact code/)).toBeInTheDocument();
    // Nothing has been written yet.
    expect(calls.some((call) => call.method === "POST")).toBe(false);

    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirm match" }));
    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST" && call.url.endsWith("/placements/matches"))).toBe(true));

    const written = calls.find((call) => call.method === "POST");
    expect(written?.body).toContain("camp_EXAMPLE_5");
    // THE LEVEL IS NOT SENT. The server asks the engine again; a console able to
    // state one could write an exact code over a 0.89 resemblance.
    expect(written?.body).not.toContain("match_method");
    expect(written?.body).not.toContain("match_score");
  });

  it("says nothing resembles anything, with the threshold and the window it read", async () => {
    stubWithSuggestions({
      evidence: {
        ...SUGGESTIONS,
        lines: [],
        counts: { lines: 3, candidates: 0, lines_to_arbitrate: 0, contested_campaigns: 0 },
        empty_message:
          "No campaign of this connector resembles a line of this plan closely enough to propose a match",
      },
    });
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    fireEvent.click(screen.getByRole("button", { name: "Suggest matches" }));

    // AN EMPTY ANSWER IS A MEASUREMENT, not a breakdown: it says how close things
    // had to be and over what it looked.
    expect(
      await screen.findByText(
        "No campaign of this connector resembles a line of this plan closely enough to propose a match",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/at or above 0.88/)).toBeInTheDocument();
    expect(screen.queryByText("The plan-versus-actual reading could not be completed")).not.toBeInTheDocument();
  });

  it("reports a broken sweep in the server's words, never as an absence of candidates", async () => {
    stubWithSuggestions(
      {
        code: "placement_evidence_unavailable",
        message: "The plan-versus-actual reading could not be completed",
      },
      503,
    );
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    fireEvent.click(screen.getByRole("button", { name: "Suggest matches" }));

    await waitFor(() =>
      expect(screen.getByText("The plan-versus-actual reading could not be completed")).toBeInTheDocument());
    // "We could not look" must never render as "nothing resembles anything".
    expect(screen.queryByText(/closely enough to propose a match/)).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /Proposed matches/i })).not.toBeInTheDocument();
  });
  // -------------------------------------------------------------------------
  // STORY 61.4 — EVERY AMOUNT SAYS ITS CURRENCY, AND THE COMPARISON LIVES IN
  // ANALYZE.
  //
  // Measured before: `grep -n "currency" WorkbenchPlacementsPage.tsx` returned 0
  // while the tab drew a line budget (exact, in the plan's currency) beside a
  // campaign spend (converted into the Project's). Two scales, no label on
  // either. And behind them the pacing marts served a figure composed of both,
  // stamped with the plan's currency, to four production callers.
  // -------------------------------------------------------------------------

  function withMoney(overrides: Record<string, unknown>) {
    return { ...EVIDENCE, money: { ...(EVIDENCE as Record<string, any>).money, ...overrides } };
  }

  it("draws no amount without a currency, budget included", async () => {
    stub(EVIDENCE);
    renderTab();

    const table = within(await screen.findByRole("region", { name: /Plan lines and their matches/i }));
    // The budget arrives EXACT and now says what it is denominated in.
    expect(table.getByText("120000.00 EUR")).toBeInTheDocument();
    // And the bare figure is gone: it is the shape a reader completes themselves.
    expect(table.queryByText("120000.00")).toBeNull();
  });

  it("refuses the comparison when the two currencies differ, names both, and shows no pacing figure", async () => {
    stub(
      withMoney({
        state: "plan_currency_mismatch",
        plan_currency: "USD",
        spend_currency: "EUR",
        comparable: false,
        message:
          "This plan's budget is in USD and this connector's spend is converted into EUR. The two amounts are each shown under their own currency and nothing is computed across them -- a budget in USD minus a spend in EUR is not a remainder.",
      }),
    );
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    const notice = screen.getByText(/This plan's budget is in USD/);
    expect(notice).toBeInTheDocument();
    // BOTH codes, because a refusal that does not say which two things disagree
    // is a refusal nobody can act on.
    expect(notice.textContent).toContain("USD");
    expect(notice.textContent).toContain("EUR");
    // AND NOT ONE COMPOSED FIGURE. No pacing percentage, no remainder, no
    // extrapolation — this tab draws none of them, and 61.4 added none. Asserted
    // on the whole document rather than by label, because the failure mode is a
    // number appearing SOMEWHERE, not a heading appearing.
    const rendered = document.body.textContent ?? "";
    expect(rendered).not.toMatch(/\d\s?%/);
    expect(rendered).not.toMatch(/Remaining budget/i);
    expect(rendered).not.toMatch(/Extrapolated/i);
    // And the budget keeps its own currency rather than borrowing the spend's.
    expect(screen.getByText("120000.00 USD")).toBeInTheDocument();
  });

  it("prints no figure at all when the server could not name the spend's currency", async () => {
    stub(withMoney({ state: "reporting_currency_unresolved", spend_currency: null, comparable: false }));
    renderTab();

    const panel = within(await screen.findByRole("region", { name: /Unmatched spend/i }));
    expect(panel.getByText("Amount in an unnamed currency")).toBeInTheDocument();
    // NEVER a `0`, and never the bare number with the code left implied.
    expect(panel.queryByText("4,210.5")).toBeNull();
    expect(panel.queryByText("0")).toBeNull();
  });

  it("says where the planned-versus-observed reading lives, and opens a real address", async () => {
    const openOwner = vi.fn();
    stub(EVIDENCE);
    renderTab(openOwner);

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    // The tab names the reading and does NOT draw it: two ratified documents put
    // pacing in Analyze, and a second place to read one figure is a second answer.
    expect(screen.getByText(/is an Analyze reading/)).toBeInTheDocument();
    // Since 67.26 the console carries the Result too (Analyze > Reports > Pacing),
    // and the sentence says so instead of naming a gap that has closed. This test
    // used to assert the pre-67.26 copy of the payload -- an instrument measuring
    // its own fixture (found 2026-08-30).
    expect(screen.getByText(/Analyze > Reports > Pacing here/)).toBeInTheDocument();
    expect(screen.queryByText(/owed and not delivered/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Open pacing" }));
    // A SEMANTIC reference, resolved by the shell — not an href composed here.
    // It names the reading itself: `analyze` > `reports`, lens `pacing`, which
    // `ui/admin/src/shell/navigation/analyze.ts` declares beside `topics`.
    expect(openOwner).toHaveBeenCalledTimes(1);
    const reference = openOwner.mock.calls[0][0] as Record<string, unknown>;
    expect(reference.workspace).toBe("analyze");
    expect(reference.section).toBe("reports");
    expect(reference.lens).toBe("pacing");
    expect(reference.object_type).toBeNull();
    expect(reference.tab).toBeNull();
  });

  it("draws no door at all when the shell cannot resolve an address", async () => {
    stub(EVIDENCE);
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    // The sentence stays — knowing WHERE the reading lives is worth something on
    // its own — and the button that would go nowhere is not drawn.
    expect(screen.getByText(/is an Analyze reading/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open pacing" })).toBeNull();
  });

  it("names the KPI variance as non-existent, with the count that proves it", async () => {
    stub(EVIDENCE);
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    expect(screen.getByText(/eleven columns/)).toBeInTheDocument();
    expect(screen.getByText(/no KPI variance can be computed/)).toBeInTheDocument();
  });

  it("keeps the money sentence out of the vocabulary of the broken one", async () => {
    stub(withMoney({ comparable: false, state: "plan_currency_mismatch" }));
    renderTab();

    await screen.findByRole("region", { name: /Plan lines and their matches/i });
    // "We could not convert" and "we could not look" are different reports, and
    // the second is a 503. Neither may be rendered for the other.
    expect(screen.queryByText("The plan-versus-actual reading could not be completed")).toBeNull();
  });
});
