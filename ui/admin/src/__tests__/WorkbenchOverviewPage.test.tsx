/**
 * The `Overview` IS the flow — story 58.9.
 *
 * What this file holds open, and what each claim cost when it was not held:
 *
 *   * the four stages `Collect · Map · Check · Publish` in the ratified order
 *     (`datastream-workbench-and-wizard.md`, « Et le vocabulaire est le nôtre »);
 *   * a stage with nothing placed shows its INVITATION and never a `0` — the tab
 *     printed `0 / 4 stages` on every Datastream of preprod, a fraction of a
 *     measurement nobody took;
 *   * `currency_fx` and `reporting_timezone` carry no switch, because migration
 *     131 REFUSES `disabled` for them and a control the database throws out is
 *     worse than no control;
 *   * the switch PREPARES a Change Set and says so — it never activates, and it
 *     never renders `on` at the click, because `_activated_capability_state`
 *     answers `ready` OR `degraded` and the clicker chooses neither;
 *   * an empty ledger and an unreadable one are two sentences, never one.
 *
 * `fetch` is stubbed rather than `apiFetch`: the seam guard
 * (`apiSeamGuard.test.ts`) is what proves the bearer is attached, and stubbing
 * one level lower would hide it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkbenchOverviewPage from "../datastreams/workbench/pages/WorkbenchOverviewPage";
import type { OwnerReference } from "../shell/pages/ProjectSettings";

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_EXAMPLE",
    project_id: "proj_EXAMPLE",
    name: "Search Console — daily",
    mode: "connector_pull",
    data_role: "fact",
    owner: "owner@example.com",
    module: "search-console",
    connector: "search-console",
    source_account_ref: "sacc_EXAMPLE",
    declared_writer: null,
    business_domains: [],
  },
  axes: {},
  versions: {},
  runs: { latest: "run_EXAMPLE", latest_state: "Ready" },
  publications: { candidate: null, current: null, last_known_good: null },
  links: {},
  primary_action: {
    kind: "prepare_change", label: "Prepare change", reason: "Settled.", tab: "mapping",
  },
} as never;

/** The six entries the server sends since 61.5, at the states it measured. */
function capabilityTabs(overrides: Record<string, Partial<{ state: string; open: boolean }>> = {}) {
  const declared = [
    { capability_key: "country", tab: null, availability: "optional" },
    { capability_key: "currency_fx", tab: null, availability: "always_present" },
    { capability_key: "reporting_timezone", tab: null, availability: "always_present" },
    { capability_key: "tax_fees", tab: "cost", availability: "optional" },
    { capability_key: "competitors", tab: null, availability: "optional" },
    { capability_key: "placement_mapping", tab: "placements", availability: "optional" },
  ];
  return declared.map((entry) => ({
    ...entry,
    state: "disabled",
    open: false,
    ...(overrides[entry.capability_key] ?? {}),
  }));
}

/** The ledger, the change-set creation and its preparation — the three addresses
 *  this tab can reach. Anything else is refused loudly rather than answered. */
function stubApi(options: {
  ledger?: unknown;
  ledgerStatus?: number;
  changeSetStatus?: number;
} = {}) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const json = (body: unknown, status = 200) =>
      Promise.resolve(new Response(JSON.stringify(body), {
        status, headers: { "Content-Type": "application/json" },
      }));
    if (url.includes("/ledger")) {
      if (options.ledgerStatus && options.ledgerStatus !== 200) {
        return json({ code: "unavailable", message: "ledger down" }, options.ledgerStatus);
      }
      return json({ ledger: options.ledger ?? [] });
    }
    if (url.endsWith("/prepare")) return json({ id: "pcs_EXAMPLE", state: "prepared" });
    if (url.endsWith("/change-sets")) {
      if (options.changeSetStatus && options.changeSetStatus !== 200) {
        return json(
          { code: "forbidden", message: "You cannot change this Project's capabilities." },
          options.changeSetStatus,
        );
      }
      return json({ id: "pcs_EXAMPLE", state: "draft" });
    }
    if (url.includes("/schedule")) {
      return json({
        cadence: "nightly", enabled: true, lifecycle_state: "active",
        // Lot D1: `run_state` is derived server-side from `enabled` +
        // `lifecycle_state` + `archived_at`, and the panel renders that word
        // rather than recombining the columns itself.
        run_state: "running", archived: false,
        window_days: 7, window_offset_days: 1, window_source: "explicit",
        arrival_hour_local: null, arrival_hour_source: "unset",
        timezone: "UTC", timezone_source: "fallback",
        on_failure: "retry_at_next_hour", retry_count: 0,
        next_run_at: null, last_run_at: null, never_ran: true,
      });
    }
    return json({ code: "not_stubbed", message: `unexpected call to ${url}` }, 500);
  }));
  return calls;
}

function mount(
  evidence: Record<string, unknown>,
  header: unknown = HEADER,
  handlers: {
    onOpenOwner?: (owner: OwnerReference) => void;
    onNavigateTab?: (tab: never) => void;
  } = {},
) {
  return render(
    <WorkbenchOverviewPage onRetryCapabilities={() => {}}
      header={header as never}
      payload={{ evidence: { state: "available", ...evidence } } as never}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      connector="search-console"
      sourceAccountRef="sacc_EXAMPLE"
      onOpenOwner={handlers.onOpenOwner}
      onNavigateTab={handlers.onNavigateTab as never}
    />,
  );
}

const EMPTY_EVIDENCE = {
  schedule: null,
  mapping_health: { version_id: null },
  dq_monitors: { count: 0, published: 0 },
  downstream_count: 0,
  stage_coverage: [],
};

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("the four stages of the flow", () => {
  it("names them in the ratified order, and each one says what is placed on it", () => {
    stubApi();
    mount({
      schedule: { next_run_at: "2026-08-09T04:00:00Z" },
      mapping_health: { version_id: "dmap_EXAMPLE", executable: true, blocking_count: 0 },
      dq_monitors: { count: 2, published: 1 },
      downstream_count: 3,
    });

    const pipeline = within(screen.getByTestId("pipeline"));
    // OUR four words, in order. Not `Fetch`, not `Enrich`, not `Load`.
    expect(
      pipeline.getAllByRole("heading", { level: 3 }).map((node) => node.textContent),
    ).toEqual(["Collect", "Map", "Check", "Publish"]);

    // Each stage names the OBJECT placed on it.
    expect(within(screen.getByTestId("stage-collect")).getByText(/^Next run/)).toBeInTheDocument();
    expect(
      within(screen.getByTestId("stage-map")).getByText(/An active mapping binds/),
    ).toBeInTheDocument();
    expect(within(screen.getByTestId("stage-check")).getByText("2 checks placed")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("stage-publish")).getByText("3 downstream consumers"),
    ).toBeInTheDocument();
  });

  it("shows an invitation, never a zero, on a stage with nothing placed", () => {
    stubApi();
    mount(EMPTY_EVIDENCE);

    const collect = within(screen.getByTestId("stage-collect"));
    const map = within(screen.getByTestId("stage-map"));
    const check = within(screen.getByTestId("stage-check"));
    const publish = within(screen.getByTestId("stage-publish"));

    expect(collect.getByText("No schedule state exists for the active plan.")).toBeInTheDocument();
    expect(map.getByText("No mapping is placed on this Datastream.")).toBeInTheDocument();
    expect(check.getByText("No check is placed on this Datastream.")).toBeInTheDocument();
    expect(publish.getByText("Nothing downstream reads this Datastream yet.")).toBeInTheDocument();

    // NO `0` ANYWHERE IN THE FLOW. `app.dq_monitors` is empty on both databases
    // and `downstream_count` is `0` for almost every Datastream; a stage that
    // printed the digit would report an absence as a measurement.
    for (const stage of ["collect", "map", "check", "publish"]) {
      expect(screen.getByTestId(`stage-${stage}`).textContent).not.toMatch(/\b0\b/);
      expect(screen.getByTestId(`stage-${stage}`).dataset.placed).toBe("no");
    }
  });

  it("tells a monitor count of zero apart from a count it could not read", () => {
    stubApi();
    const { unmount } = mount({ ...EMPTY_EVIDENCE, dq_monitors: { count: 0, published: 0 } });
    expect(screen.getByText("No check is placed on this Datastream.")).toBeInTheDocument();
    expect(screen.queryByText("The data-quality monitors could not be read.")).toBeNull();
    unmount();

    stubApi();
    mount({ ...EMPTY_EVIDENCE, dq_monitors: undefined });
    // Two sentences with no word in common: one is an answer, the other is the
    // absence of one.
    expect(screen.getByText("The data-quality monitors could not be read.")).toBeInTheDocument();
    expect(screen.queryByText("No check is placed on this Datastream.")).toBeNull();
  });

  it("keeps the four stage actions on one baseline when a column is taller", () => {
    stubApi();
    // `stage-collect` carries two paragraphs here and `stage-map` one, so the
    // columns are of different heights by construction.
    mount({
      ...EMPTY_EVIDENCE,
      mapping_health: { version_id: "dmap_EXAMPLE", executable: true, blocking_count: 0 },
    });

    const cells = ["collect", "map", "check", "publish"].map((stage) =>
      screen.getByTestId(`stage-${stage}`),
    );
    // jsdom lays nothing out, so the CONTRACT is what is pinned: a full-height
    // flex column with the action pushed to its bottom edge. That is what puts
    // the four buttons on one line, and it is Tailwind utilities — no rule was
    // added to `application.css`, whose volume never goes up.
    for (const cell of cells) {
      expect(cell.className).toContain("flex");
      expect(cell.className).toContain("flex-col");
      expect(cell.className).toContain("h-full");
      expect(within(cell).getByTestId("stage-action").className).toContain("mt-auto");
    }
  });

  it("opens Data Quality, and the button is named after its navigation", () => {
    stubApi();
    const onOpenOwner = vi.fn();
    mount(EMPTY_EVIDENCE, HEADER, { onOpenOwner });

    // REWRITTEN 2026-08-11, and the ratified document is what decides it by name:
    // « `Add a check` n'existe pas : aucun moniteur n'existe sur aucune base et
    // rien ici n'en pose un, donc le bouton s'appelle d'après sa navigation,
    // `Open Data Quality`, et mène à la collection `controls-quality/
    // data-quality` ; poser un moniteur appartient à l'epic 59 »
    // (`datastream-workbench-and-wizard.md`, ligne du Check stage).
    //
    // This test asserted the opposite, and it PASSED against a control that did
    // nothing at all: `object_type: "dq-monitor"` with `object_id: null` is
    // refused outright by `ContentRouter`, and neither the `controls-quality`
    // section nor the `dq-monitor` contract declares a `create` action in
    // `navigation.ts`. A mock handler cannot see a router refusal, so the
    // assertion measured the call and never the destination — which is how a
    // dead click stayed green through two review tours.
    expect(screen.queryByRole("button", { name: "Add a check" })).toBeNull();
    expect(screen.getByRole("button", { name: "Open Data Quality" })).toBeTruthy();
    fireEvent.click(screen.getByTestId("stage-check-action"));
    expect(onOpenOwner).toHaveBeenCalledWith(
      expect.objectContaining({
        workspace: "governance",
        section: "controls-quality",
        lens: "data-quality",
        object_type: null,
        object_id: null,
        action: null,
      }),
    );
  });

  it("edits the schedule from `Collect`, with cadence, arrival hour AND next run", async () => {
    // Arbitrage 7: the WHOLE panel. Embedding only the cadence and the next run
    // would send the arrival hour back to `Processing`, which is exactly what
    // the amendment of 2026-08-05 took out of it.
    stubApi();
    mount(EMPTY_EVIDENCE);

    expect(screen.queryByTestId("schedule-cadence")).toBeNull();
    fireEvent.click(screen.getByTestId("stage-collect-action"));

    expect(await screen.findByTestId("schedule-cadence")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-arrival-hour")).toBeInTheDocument();
    expect(screen.getByTestId("schedule-next-run")).toBeInTheDocument();
    // ONE component, so ONE `PUT …/schedule`: the panel `Processing` mounts is
    // the panel this stage mounts.
    expect(screen.getByTestId("schedule-save")).toBeInTheDocument();
  });
});

describe("the days collected", () => {
  it("tells an empty window apart from a ledger it could not read", async () => {
    stubApi({ ledger: [] });
    const { unmount } = mount(EMPTY_EVIDENCE);
    expect(await screen.findByText("No day in this window")).toBeInTheDocument();
    expect(screen.queryByText("Day-by-day coverage unavailable")).toBeNull();
    unmount();

    stubApi({ ledgerStatus: 503 });
    mount(EMPTY_EVIDENCE);
    // A different sentence, and no strip: an invented strip would look like
    // evidence of coverage.
    expect(await screen.findByText("Day-by-day coverage unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No day in this window")).toBeNull();
  });
});

describe("the modules", () => {
  it("gives no switch to the two capabilities the database refuses to disable", () => {
    stubApi();
    mount(EMPTY_EVIDENCE, { ...(HEADER as object), capability_tabs: capabilityTabs() });

    for (const key of ["country", "tax_fees", "competitors", "placement_mapping"]) {
      expect(screen.getByTestId(`module-switch-${key}`)).toBeInTheDocument();
    }
    // Migration 131: `CHECK (NOT (availability='always_present' AND
    // state='disabled'))`. The row stays — what is forbidden is the control.
    for (const key of ["currency_fx", "reporting_timezone"]) {
      expect(screen.queryByTestId(`module-switch-${key}`)).toBeNull();
      expect(screen.getByTestId(`module-always-present-${key}`)).toHaveTextContent(
        "Always present",
      );
      expect(screen.getByTestId(`module-${key}`)).toBeInTheDocument();
    }
  });

  it("shows six modules, and the sixth carries the scope story 61.1 measured", () => {
    stubApi();
    mount(EMPTY_EVIDENCE, {
      ...(HEADER as object),
      // `unset`, which is what the header reports for a Project whose sixth row
      // the backfill of migration 243 has not reached yet.
      capability_tabs: capabilityTabs({ placement_mapping: { state: "unset" } }),
    });

    // Six rows, from a six-row payload. The count is never written in this file.
    expect(screen.getAllByTestId(/^module-(?!switch|always)/)).toHaveLength(6);
    const row = screen.getByTestId("module-placement_mapping");
    // The label, never the raw key: `placement_mapping` on screen is a machine
    // key wearing a badge.
    expect(within(row).getByText("Placement Mapping")).toBeInTheDocument();
    expect(row.textContent).not.toContain("placement_mapping");
    expect(within(row).getByText("Never configured")).toBeInTheDocument();
    // THE SENTENCE STORY 61.1 WROTE, and it is a measurement rather than a wish.
    // Until 61.1, `project-settings.md` and `capabilities/README.md` both forbade
    // assuming the two columns this capability adds, and this row stated the
    // absence. 61.1 built the matching workbench, named the columns
    // (`plan_line_key`, `plan_line_label`) and the nullability, and wrote
    // `capabilities/placement-mapping.md`; this line is that card's one-sentence
    // form. Two assertions, so the row can neither fall back to the placeholder
    // nor drift away from what the card says.
    expect(
      within(row).queryByText("No scope sentence is declared for this capability in this build."),
    ).toBeNull();
    expect(within(row).getByText(/adds the matched line's key and label/i)).toBeInTheDocument();
    // Null, never zero, is the half of the card a screen most easily loses.
    expect(within(row).getByText(/null, never zero/i)).toBeInTheDocument();
  });

  it("names the tab a capability adds only while it adds one", () => {
    stubApi();
    const { unmount } = mount(
      EMPTY_EVIDENCE,
      {
        ...(HEADER as object),
        capability_tabs: capabilityTabs({ tax_fees: { state: "ready", open: true } }),
      },
    );
    expect(within(screen.getByTestId("module-tax_fees")).getByText("Adds the Cost tab"))
      .toBeInTheDocument();
    // The four that add none say so rather than staying silent about it.
    expect(within(screen.getByTestId("module-country")).getByText("Adds no tab."))
      .toBeInTheDocument();
    unmount();

    stubApi();
    mount(EMPTY_EVIDENCE, { ...(HEADER as object), capability_tabs: capabilityTabs() });
    // Off, the tab does not exist and the router refuses its address, so naming
    // `Cost` here would describe a destination that opens nothing.
    expect(screen.getByTestId("module-tax_fees").textContent).not.toContain("Cost");
    expect(within(screen.getByTestId("module-tax_fees")).getByText(
      "No tab is added while this capability is off.",
    )).toBeInTheDocument();
  });

  it("prepares a Change Set, says so, and never turns the switch on", async () => {
    const calls = stubApi();
    mount(EMPTY_EVIDENCE, { ...(HEADER as object), capability_tabs: capabilityTabs() });

    const toggle = screen.getByTestId("module-switch-tax_fees");
    expect(toggle).toHaveAttribute("data-state", "unchecked");
    fireEvent.click(toggle);

    // The switch ASKS before it prepares. Nothing has left this screen yet, and
    // the dialog says which capability and where the activation really happens.
    fireEvent.click(await screen.findByRole("button", { name: "Prepare change" }));

    expect(await screen.findByText(/A Change Set is prepared to turn Tax & Fees on/))
      .toBeInTheDocument();
    // THE SWITCH DID NOT MOVE. `_activated_capability_state` answers `ready` OR
    // `degraded`, so the person clicking chooses neither, and a switch showing
    // `on` would be inventing the outcome.
    expect(screen.getByTestId("module-switch-tax_fees")).toHaveAttribute("data-state", "unchecked");

    // The two calls `ProjectSettings` already makes, and ONLY those two: a third
    // and a fourth would make this screen a second activation authority.
    const settings = calls.filter((call) => call.url.includes("/settings/change-sets"));
    expect(settings.map((call) => call.url.replace(/^.*\/api/, "/api"))).toEqual([
      "/api/projects/proj_EXAMPLE/settings/change-sets",
      "/api/projects/proj_EXAMPLE/settings/change-sets/pcs_EXAMPLE/prepare",
    ]);
    expect(JSON.parse(String(settings[0].init?.body))).toEqual({
      intent: { capabilities: { tax_fees: "enabled" } },
    });
    // NO CONFIRMATION SECRET LEAVES THIS SCREEN, in any call it makes.
    for (const call of calls) {
      expect(String(call.init?.body ?? "")).not.toContain("confirmation_secret");
      expect(call.url).not.toContain("/confirmations");
      expect(call.url).not.toMatch(/\/confirm$/);
    }
  });

  it("says nothing was prepared when the preparation was refused", async () => {
    stubApi({ changeSetStatus: 403 });
    mount(EMPTY_EVIDENCE, { ...(HEADER as object), capability_tabs: capabilityTabs() });

    fireEvent.click(screen.getByTestId("module-switch-country"));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare change" }));

    expect(await screen.findByText("No Change Set was prepared for Country")).toBeInTheDocument();
    // The server's own sentence, not a generic failure.
    expect(screen.getByText("You cannot change this Project's capabilities.")).toBeInTheDocument();
    expect(screen.getByTestId("module-switch-country")).toHaveAttribute("data-state", "unchecked");
  });

  it("tells a Project with no capability row apart from a header that carried none", async () => {
    stubApi();
    const { unmount } = mount(EMPTY_EVIDENCE, {
      ...(HEADER as object), capability_tabs: [],
    });
    expect(screen.getByText("No capability is declared on this Project")).toBeInTheDocument();
    expect(screen.queryByText("Project capabilities could not be read")).toBeNull();
    unmount();

    stubApi();
    mount(EMPTY_EVIDENCE, HEADER);
    await waitFor(() =>
      expect(screen.getByText("Project capabilities could not be read")).toBeInTheDocument());
    expect(screen.queryByText("No capability is declared on this Project")).toBeNull();
  });

  /**
   * AMENDMENT 11, THE HALF THAT WAS STILL OPEN — « Les deux panneaux `Modules`
   * et `Project capabilities on this Datastream` fusionnent en un seul ».
   *
   * Measured on the 2026-08-12 capture: both panels enumerated the same six
   * capabilities on one 3742px page. The merge is not a deletion — a row now
   * carries the Project decision AND what the last Change Set compiled for this
   * Datastream, so this test holds both halves on ONE row.
   */
  it("carries the Project switch and the compiled reading on one row", () => {
    stubApi();
    mount(
      {
        ...EMPTY_EVIDENCE,
        capabilities: {
          capabilities: [
            {
              capability_key: "country",
              applicability: "applicable",
              coverage_state: "partial",
              reason: "The market column is bound and two values reach no country.",
              impact: { fan_out: { downstream_consumers: 2 } },
              blockers: [],
              exceptions: [],
            },
          ],
        },
      },
      { ...(HEADER as object), capability_tabs: capabilityTabs({ country: { state: "ready", open: true } }) },
    );

    // ONE panel with the six rows, and no second enumeration anywhere.
    expect(screen.getAllByTestId(/^module-(?!switch|always)/)).toHaveLength(6);
    expect(screen.queryByText("Project capabilities on this Datastream")).toBeNull();

    const row = within(screen.getByTestId("module-country"));
    // The Project half: the state it is in, and the control that changes it.
    expect(row.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByTestId("module-switch-country")).toHaveAttribute("data-state", "checked");
    // The Datastream half, on the same row and named as such — two badges that
    // do not say which question they answer are the repetition this merge
    // removes, not a fix for it.
    expect(row.getByText("On this Datastream")).toBeInTheDocument();
    expect(row.getByText("Partial")).toBeInTheDocument();
    expect(
      row.getByText("The market column is bound and two values reach no country."),
    ).toBeInTheDocument();
    // And the compiled impact, which had no home outside the second panel.
    expect(row.getByText("Fan-out")).toBeInTheDocument();
    expect(row.getByText("2 downstream consumer(s)")).toBeInTheDocument();
  });

  /**
   * D-9 — `bound_fields: Unavailable` reached a user screen.
   *
   * A key is a machine word and `CLAUDE.md` forbids it here, and the value was
   * wrong too: `record()` answers `null` to a number, so a block carrying a
   * count reported "Unavailable" for a measurement the payload had given. The
   * repair is the fallback rather than two more dictionary entries — the next
   * block the compiler grows would leak in exactly the same way.
   */
  it("never prints a payload key, or an absence, where a block carries a count", () => {
    stubApi();
    mount(
      {
        ...EMPTY_EVIDENCE,
        capabilities: {
          capabilities: [
            {
              capability_key: "currency_fx",
              applicability: "applicable",
              coverage_state: "complete",
              impact: { bound_fields: 1, expected_fields: 1 },
              blockers: [],
              exceptions: [],
            },
          ],
        },
      },
      { ...(HEADER as object), capability_tabs: capabilityTabs() },
    );

    const row = screen.getByTestId("module-currency_fx");
    expect(row.textContent).not.toContain("bound_fields");
    expect(row.textContent).not.toContain("expected_fields");
    expect(within(row).getByText("Bound Fields")).toBeInTheDocument();
    expect(within(row).queryByText("Unavailable")).toBeNull();
  });

  it("reads the switch position from the state, never from the coverage", () => {
    stubApi();
    mount(EMPTY_EVIDENCE, {
      ...(HEADER as object),
      capability_tabs: capabilityTabs({
        country: { state: "ready", open: true },
        tax_fees: { state: "degraded", open: true },
        competitors: { state: "blocked", open: false },
      }),
    });

    expect(screen.getByTestId("module-switch-country")).toHaveAttribute("data-state", "checked");
    // `degraded` IS active — hiding evidence already collected is worse than
    // showing it diminished — and the badge says which of the two it is.
    expect(screen.getByTestId("module-switch-tax_fees")).toHaveAttribute("data-state", "checked");
    expect(within(screen.getByTestId("module-tax_fees")).getByText("Degraded")).toBeInTheDocument();
    expect(screen.getByTestId("module-switch-competitors")).toHaveAttribute("data-state", "unchecked");
    expect(within(screen.getByTestId("module-competitors")).getByText("Blocked")).toBeInTheDocument();
  });
});
