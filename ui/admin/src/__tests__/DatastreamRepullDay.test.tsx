/**
 * Re-collecting one day, from its row — story 58.4, epic 58.
 *
 * THE GESTURE SPENDS PROVIDER QUOTA, so the ways it can lie are the tests:
 *
 *   1. spending without saying on which connector and which account. The day
 *      payload carries the connector and NOT the account, so a screen that
 *      showed only what it had would let somebody re-collect on a connection
 *      they never checked;
 *   2. putting a figure beside "this will cost". No route of this product
 *      publishes one — `quota.py` answers `open|closed` from a per-process
 *      singleton — so the line is `Not measured` WITH its reason, or it is a
 *      number nobody computed;
 *   3. announcing "queued" for a window the queue refused. `enqueue_pull`
 *      answers a dict shaped like a job whose state is no registered job state,
 *      and the previous screen counted `jobs.length`;
 *   4. reading a broken write as an empty one. THE DOUBLE ANSWERS `ok: false`
 *      here, because a double that never refuses is what let story 58.3 ship a
 *      screen where an HTTP failure read « no day was opened »;
 *   5. drawing a run the grid never observed. Arbitrage 6: after the `202` the
 *      window is READ AGAIN and the row shows what came back.
 *
 * `fetch` is stubbed, never `apiFetch`: the seam guard is what proves the bearer
 * is attached, and stubbing one level lower would hide it.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchDataPage from "../datastreams/workbench/pages/WorkbenchDataPage";
import type { WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";
import { EXTRACT_STATUS_MARK, EXTRACT_STATUS_SHAPE, type CoverageStatus } from "../ui";

const PROJECT = "proj_EXAMPLE";
const STREAM = "ds_EXAMPLE";
const ACCOUNT = "sacct_EXAMPLE";

const TAB_PAYLOAD: WorkbenchTabPayload = {
  schema: "datastream_workbench.data.v1",
  tab: "data",
  project_id: PROJECT,
  datastream_id: STREAM,
  evidence: {
    state: "unavailable",
    sample_state: "unavailable",
    sample_reason: "No execution-scoped masked sample reference was persisted.",
    availability: {
      collected: "unavailable", mapped: "unavailable",
      processed: "unavailable", published: "unavailable",
    },
    availability_reason: {},
    stages: [],
  },
};

const WINDOW = { start: "2026-07-10", end: "2026-07-13", bounded_at: 92, bound_reached: false };

/** One day per state that matters here: collected, empty, never asked, running. */
function days(overrides: Record<string, unknown>[] = []) {
  return overrides.length ? overrides : [
    { date: "2026-07-10", extract_status: "ok", job_state: "done", extract_count: 1,
      row_count: 150, row_count_reason: null, rows: 4, rows_reason: null,
      execution_id: null },
    { date: "2026-07-11", extract_status: "empty", job_state: "done", extract_count: 1,
      row_count: 0, row_count_reason: null, rows: 0, rows_reason: null,
      execution_id: null },
    { date: "2026-07-12", extract_status: "never_fetched", job_state: null,
      extract_count: 0, row_count: null, row_count_reason: null, rows: null,
      rows_reason: null, execution_id: null },
    { date: "2026-07-13", extract_status: "running", job_state: "running",
      extract_count: 1, row_count: null, row_count_reason: "not_verified", rows: null,
      rows_reason: null, execution_id: null },
  ];
}

function breakdown(overrides: Record<string, unknown> = {}) {
  return {
    schema: "datastream_daily_breakdown.v1",
    project_id: PROJECT,
    datastream_id: STREAM,
    connector: "meta-ads",
    window: WINDOW,
    columns: [],
    columns_reason: "no_mapping",
    columns_source: null,
    column_row_join_available: false,
    column_row_join_reason: "mart_metric_is_a_dbt_literal",
    days: days(),
    reason: null,
    view_mode: null,
    stage_relations: null,
    ...overrides,
  };
}

/**
 * The two calls this screen makes, and only these two.
 *
 * `breakdowns` is a QUEUE: the first read answers the first entry, and the read
 * that follows the `202` answers the next. That is what makes "the window was
 * read again" measurable instead of assumed.
 */
function stub({
  breakdowns = [breakdown()],
  refetch = { ok: true, status: 202, body: { jobs: [{ job_id: "job_1", state: "queued", date_from: "2026-07-11", date_to: "2026-07-11" }] } },
}: {
  breakdowns?: unknown[];
  refetch?: { ok: boolean; status: number; body: unknown };
} = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  let read = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({
      url,
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    if (url.includes("/refetch")) {
      return {
        ok: refetch.ok, status: refetch.status, json: async () => refetch.body,
      } as Response;
    }
    if (url.includes("/daily-breakdown")) {
      const body = breakdowns[Math.min(read, breakdowns.length - 1)];
      if (!url.includes("day=")) read += 1;
      return { ok: true, status: 200, json: async () => body } as Response;
    }
    throw new Error(`unexpected ${url}`);
  }));
  return calls;
}

function mount(props: Record<string, unknown> = {}) {
  return render(
    <WorkbenchDataPage
      payload={TAB_PAYLOAD}
      projectId={PROJECT}
      datastreamId={STREAM}
      sourceAccountRef={ACCOUNT}
      {...props}
    />,
  );
}

async function gridRows() {
  const grid = within(await screen.findByRole("region", { name: "Day-by-day breakdown" }));
  return grid.getAllByRole("row").slice(1);
}

/** The action of a row, named by the sentence it carries. */
async function repullButton(name: RegExp) {
  return screen.findByRole("button", { name });
}

afterEach(() => vi.unstubAllGlobals());

describe("Re-collecting one day", () => {
  it("names what is missing, with the connector and the date, on the row itself", async () => {
    stub();
    mount();

    // The two absences of the fixture, each said as the fact it is. Not one
    // wording for both: « returned nothing » and « was never asked » are the
    // difference between an answer and an absence.
    expect(await repullButton(/^meta-ads returned nothing for 2026-07-11$/))
      .toBeInTheDocument();
    expect(await repullButton(/^meta-ads was never asked for 2026-07-12$/))
      .toBeInTheDocument();
  });

  it("offers the gesture on an empty day, and not on a collected or a running one", async () => {
    // JEAN, 2026-08-06, and it reverses what the code said: `empty` IS
    // re-collectable. The provider may have filled its own hole since, and the
    // person looking at the row is the one who knows.
    stub();
    mount();

    const rows = await gridRows();
    expect(within(rows[1]).getByRole("button", { name: /returned nothing/ })).toBeEnabled();
    // A collected day and a day being collected right now have nothing to
    // re-ask, and they say so instead of showing a dead control.
    expect(within(rows[0]).queryByRole("button", { name: /Re-collect/i })).toBeNull();
    expect(within(rows[0]).getByText("Nothing to re-ask")).toBeInTheDocument();
    expect(within(rows[3]).getByText("Nothing to re-ask")).toBeInTheDocument();
  });

  it("writes nothing until the confirmation is confirmed, and nothing at all on Cancel", async () => {
    const calls = stub();
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    expect(screen.getByTestId("repull-confirm")).toBeInTheDocument();
    expect(calls.some((call) => call.url.includes("/refetch"))).toBe(false);

    await userEvent.click(screen.getByTestId("repull-cancel"));
    await waitFor(() => expect(screen.queryByTestId("repull-confirm")).toBeNull());
    expect(calls.some((call) => call.url.includes("/refetch"))).toBe(false);
  });

  it("names the Datastream, the connector, the source account and the day", async () => {
    stub();
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    const dialog = screen.getByTestId("repull-confirm");

    expect(dialog).toHaveTextContent(STREAM);
    expect(dialog).toHaveTextContent("meta-ads");
    // The ACCOUNT, which the day payload does not carry: it comes from the
    // header through the route, and without it the dialog would name a
    // connection instead of the thing that gets billed.
    expect(dialog).toHaveTextContent(ACCOUNT);
    expect(dialog).toHaveTextContent("2026-07-11");
    // And the sentence of the row, again, in full — the button could only carry
    // it as a name.
    expect(dialog).toHaveTextContent("meta-ads returned nothing for 2026-07-11");
  });

  it("says the spend is not measured, and says WHY, and invents no figure", async () => {
    stub();
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    const dialog = screen.getByTestId("repull-confirm");

    expect(dialog).toHaveTextContent("Not measured");
    expect(dialog).toHaveTextContent(/open\/closed breaker and no balance/);
    // No number that would read as a cost. The day and the dates are the only
    // digits a person may see here.
    expect(dialog.textContent).not.toMatch(/\b\d+\s*(points?|credits?|requests?|€|\$)/i);
  });

  it("says the source account is not reported rather than leaving it blank", async () => {
    // A Datastream whose header names no account. The row must not disappear
    // from the dialog: a missing line reads as a line whose value is empty.
    stub();
    mount({ sourceAccountRef: null });

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    expect(screen.getByTestId("repull-confirm")).toHaveTextContent("Not reported");
  });

  it("asks for exactly the one day, at the project-scoped address", async () => {
    const calls = stub();
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/refetch"))).toBe(true));
    const write = calls.find((call) => call.url.includes("/refetch"))!;
    expect(write.method).toBe("POST");
    // ONE address, and the project is in the PATH — the un-scoped one is gone.
    expect(write.url).toContain(`/api/projects/${PROJECT}/datastreams/${STREAM}/refetch`);
    expect(write.body).toEqual({ dates: ["2026-07-11"] });
  });

  it("reads the window again after the 202 instead of drawing the id it was handed", async () => {
    // ARBITRAGE 6. The second payload is what the route WOULD answer once a
    // re-collection can own a run; today it cannot (the same route refuses a
    // stream that names an active plan version, proved on rows in
    // tests/integration/test_datastream_daily_breakdown_api.py). What this pins
    // is the console's rule: the row shows what the READ came back with.
    const after = breakdown({
      days: days([
        { ...days()[0] }, { ...days()[1], job_state: "queued", execution_id: "dse_2" },
        { ...days()[2] }, { ...days()[3] },
      ]),
    });
    const calls = stub({ breakdowns: [breakdown(), after] });
    mount({ onOpenRun: vi.fn() });

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    // The window was asked for a second time…
    await waitFor(() =>
      expect(calls.filter((call) =>
        call.url.includes("/daily-breakdown") && !call.url.includes("day=")).length,
      ).toBeGreaterThan(1));
    // …and the row now carries the run that read came back with.
    const rows = await gridRows();
    await waitFor(() =>
      expect(within(rows[1]).getByRole("button", { name: "2026-07-11" })).toBeInTheDocument());
  });

  it("says what was queued, for how many days, and closes the dialog", async () => {
    stub();
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    expect(await screen.findByText(/Queued 1 pull window for 1 day/)).toBeInTheDocument();
    expect(screen.queryByTestId("repull-confirm")).toBeNull();
  });

  it("never announces a queue for a window the queue refused", async () => {
    // `enqueue_pull` answers `202` with an entry whose state is no registered
    // pull-job state. Counting `jobs.length` reported a spend that never
    // happened, on the exact path a person uses when their account scope is not
    // authorized — which is the moment they most need the real answer.
    stub({
      refetch: {
        ok: true, status: 202,
        body: { jobs: [{ state: "refused", code: "access_denied",
          message: "connection/account scope is not authorized for this resource" }] },
      },
    });
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    const said = await screen.findByText(/refused by the queue/);
    expect(said).toHaveTextContent(/connection\/account scope is not authorized/);
    expect(screen.queryByText(/Queued 1 pull window/)).toBeNull();
  });

  it("says a collection already holds the Datastream, and does not call it a refusal", async () => {
    // Jean, 2026-08-06: the console SAYS it and lets go. The windows were
    // queued; what is missing is the run line, and the sentence is the server's.
    stub({
      refetch: {
        ok: true, status: 202,
        body: {
          jobs: [{ job_id: "job_1", state: "queued" }],
          active_run: {
            execution_id: "dse_EXAMPLE", state: "loading",
            message: "A collection is already running on this Datastream.",
          },
        },
      },
    });
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    const said = await screen.findByText(/A collection is already running on this Datastream/);
    // It is a fact beside a success, not an error: the day WAS queued.
    expect(said).toHaveTextContent(/Queued 1 pull window/);
    expect(said).toHaveTextContent(/no run line of its own/);
  });

  it("says a day is outside the organisation's recovery window, in its own words", async () => {
    // The ceiling used to CLAMP this into a window whose start was after its end
    // and queue it anyway. It is now a refusal with an actionable sentence, and
    // the console must render that sentence rather than count the entry — which
    // is the same rule as any other queue refusal, and the reason `wasQueued`
    // asks the registry instead of matching a word.
    stub({
      refetch: {
        ok: true, status: 202,
        body: { jobs: [{ state: "refused", code: "window_before_backfill_ceiling",
          date_from: "2026-07-11", date_to: "2026-07-11",
          message: "this organisation's backfill reaches back to 2026-07-07 (30 days); "
            + "the requested window ends on 2026-07-11, which is before it, so no day "
            + "of it can be collected" }] },
      },
    });
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    const said = await screen.findByText(/refused by the queue/);
    expect(said).toHaveTextContent(/backfill reaches back to 2026-07-07/);
    expect(screen.queryByText(/Queued 1 pull window/)).toBeNull();
  });

  it("says the window was moved when the backfill ceiling moved it", async () => {
    stub({
      refetch: {
        ok: true, status: 202,
        body: { jobs: [{ job_id: "job_1", state: "queued",
          backfill_clamp: { clamped: true, date_from: "2026-07-07", max_backfill_days: 30 } }] },
      },
    });
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    expect(await screen.findByText(/moved to start on 2026-07-07/)).toBeInTheDocument();
  });

  it("keeps every day and shows the server's sentence when the write is refused", async () => {
    // BROKEN IS NOT EMPTY. The double answers `ok: false` — the one thing the
    // 58.3 double never did, which is how a failed read shipped as « no day was
    // opened ».
    stub({
      refetch: {
        ok: false, status: 422,
        body: { code: "not_configured",
          message: "This Datastream is not linked to a connection yet." },
      },
    });
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    const dialog = await screen.findByTestId("repull-confirm");
    expect(dialog).toHaveTextContent("The re-collection could not be queued.");
    // The route's own words, not `HTTP 422`.
    expect(dialog).toHaveTextContent("This Datastream is not linked to a connection yet.");
    // The grid keeps its four days, and no run appeared anywhere. Queried
    // through `hidden: true`: an open dialog puts `aria-hidden` on everything
    // behind it, which is correct and is not the grid being unmounted.
    const rows = within(screen.getByTestId("daily-breakdown-grid"))
      .getAllByRole("row", { hidden: true })
      .slice(1);
    expect(rows).toHaveLength(4);
    expect(screen.queryByText(/Queued/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^dse_/, hidden: true })).toBeNull();
  });

  it("renders the mark fact and the pill class as ONE decision", async () => {
    // THREE SURFACES DRAW THIS DISTINCTION and each has its own guard, on its own
    // suite — measured 2026-08-07: mutating `EXTRACT_STATUS_SHAPE` reddens
    // `WorkbenchDataPage` alone, mutating `BAR` reddens `CoverageStrip` alone,
    // mutating `EXTRACT_STATUS_MARK` reddens `MiniStrip` alone. Three green
    // guards over one decision can still drift apart, so the two forms that CAN
    // be compared without a DOM are compared here: a state is open in the mark
    // exactly when its pill carries the dashed outline.
    for (const status of Object.keys(EXTRACT_STATUS_MARK) as CoverageStatus[]) {
      expect(EXTRACT_STATUS_SHAPE[status].includes("dashed")).toBe(
        EXTRACT_STATUS_MARK[status] === "open",
      );
    }
    // And the fact itself is not vacuous: exactly one state is open.
    expect(
      Object.values(EXTRACT_STATUS_MARK).filter((mark) => mark === "open"),
    ).toHaveLength(1);
  });

  it("cites the status when the refusal carried no sentence at all", async () => {
    stub({ refetch: { ok: false, status: 503, body: {} } });
    mount();

    await userEvent.click(await repullButton(/returned nothing for 2026-07-11/));
    await userEvent.click(screen.getByTestId("repull-go"));

    expect(await screen.findByTestId("repull-confirm")).toHaveTextContent("HTTP 503");
  });
});

/**
 * A DAY THE SOURCE REFUSED — AI-307, and the half of it a person actually sees.
 *
 * The state shipped on the server first: `prevented`, `row_count` NULL, and the
 * connector's own sentence written onto the window. Measured on that landing,
 * `grep -rn "prevented_message" ui/ web/` returned 0 — the sentence reached the
 * MCP diagnosis and stopped there, while the grid said « did not allow this
 * collection for 2026-07-12 » and named no gesture at all. The ways this half
 * can lie are these tests:
 *
 *   1. saying WHAT happened and not WHAT TO DO. « did not allow » is a state; the
 *      thing a person can act on is the connector's sentence, and it is the one
 *      thing `CLAUDE.md` asks of every message;
 *   2. telling two stories one click apart. The row's name and the confirmation
 *      it opens are compared CHARACTER FOR CHARACTER here, because that is the
 *      drift that shipped: `repullScopeSentence` dropped the window's state and
 *      the same day read « was never asked for » inside the dialog;
 *   3. counting a refusal as an absence. The ledger answers `never_fetched` for a
 *      prevented window by design, so a bulk confirmation that counts
 *      `extract_status` alone says « N never requested » about N refusals;
 *   4. dressing an absent sentence up as a complete one. A prevented day whose
 *      window carried no message says so, and still points at the source.
 */
describe("A day the source did not allow", () => {
  const GESTURE =
    "Request the reviews allowlist for this project, then re-ask these dates. "
    + "Your daily figures are unaffected.";

  /** `never_fetched` at the day grain — the ledger's own answer — and `prevented`
   *  on the window. That pair IS the defect: only the second word is true. */
  function preventedDay(overrides: Record<string, unknown> = {}) {
    return {
      date: "2026-07-12", extract_status: "never_fetched", job_state: "prevented",
      extract_count: 0, row_count: null, row_count_reason: null, rows: null,
      rows_reason: null, execution_id: null,
      prevented_reason: "reviews_access_pending",
      prevented_message: GESTURE,
      ...overrides,
    };
  }

  it("names the gesture the connector sent, not only that it was refused", async () => {
    stub({ breakdowns: [breakdown({ days: [preventedDay()] })] });
    mount();

    const button = await repullButton(/did not allow this collection for 2026-07-12/);
    // THE STATE IS NOT THE MESSAGE. Whatever else the row says, it says what to
    // go and do — and it is the connector's wording, not one composed here.
    expect(button).toHaveAccessibleName(new RegExp(GESTURE.replace(/[.]/g, "\\.")));
  });

  it("says the same sentence on the row and in the confirmation it opens", async () => {
    stub({ breakdowns: [breakdown({ days: [preventedDay()] })] });
    mount();

    const button = await repullButton(/did not allow this collection for 2026-07-12/);
    const onTheRow = button.getAttribute("title") ?? "";
    expect(onTheRow).not.toBe("");

    await userEvent.click(button);
    // Character for character. « ... did not allow this collection » on the row
    // and « ... was never asked for » in the dialog is what a person quotes back
    // at support, and only one of the two was true.
    expect(screen.getByTestId("repull-confirm")).toHaveTextContent(onTheRow);
    expect(screen.getByTestId("repull-confirm")).not.toHaveTextContent(
      "was never asked for 2026-07-12",
    );
  });

  it("counts refusals as refusals when several days are asked for at once", async () => {
    stub({
      breakdowns: [breakdown({ days: [
        preventedDay(),
        preventedDay({ date: "2026-07-13" }),
        { date: "2026-07-11", extract_status: "never_fetched", job_state: null,
          extract_count: 0, row_count: null, row_count_reason: null, rows: null,
          rows_reason: null, execution_id: null },
      ] })],
    });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Select all 3 re-collectable/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Re-collect 3$/ }));

    const dialog = screen.getByTestId("repull-confirm");
    // Two facts, counted apart: two windows the source refused, and one day
    // nobody ever asked for. Flattened on `extract_status` this read « 3 Never
    // requested », which is the number that decides whether re-asking is worth
    // quota — and it was wrong about two thirds of it.
    expect(dialog).toHaveTextContent(/2 Prevented/);
    expect(dialog).toHaveTextContent(/1 Never requested/);
    expect(dialog).not.toHaveTextContent(/3 Never requested/);
  });

  /**
   * THE SAME THREE READINGS OF ONE WINDOW, on one mount — the class this
   * describe closed on ONE of its readers.
   *
   * `repullScopeSentence` was handed the whole day and stopped counting a
   * refusal as an absence; two readers of the SAME grid still counted and spelt
   * `extract_status` alone. Measured before the repair, on this exact fixture:
   * the filter band read « Never requested · 3 », the prevented rows carried a
   * pill reading « Never requested », and the confirmation of the same page read
   * « 2 Prevented — the source did not allow it; 1 Never requested ». One page,
   * two answers to one question, and the page is where a person decides whether
   * to spend quota.
   */
  function twoRefusalsAndOneAbsence() {
    return [
      { date: "2026-07-10", extract_status: "ok", job_state: "done", extract_count: 1,
        row_count: 150, row_count_reason: null, rows: 4, rows_reason: null,
        execution_id: null },
      preventedDay(),
      preventedDay({ date: "2026-07-13" }),
      { date: "2026-07-11", extract_status: "never_fetched", job_state: null,
        extract_count: 0, row_count: null, row_count_reason: null, rows: null,
        rows_reason: null, execution_id: null },
    ];
  }

  it("counts the refusals apart in the band that filters the same days", async () => {
    stub({ breakdowns: [breakdown({ days: twoRefusalsAndOneAbsence() })] });
    mount();

    const band = within(await screen.findByTestId("day-state-filter"));
    expect(band.getByRole("button", { name: "Prevented — the source did not allow it · 2" }))
      .toBeInTheDocument();
    expect(band.getByRole("button", { name: "Never requested · 1" })).toBeInTheDocument();
    // The number that was there before: three days flattened onto the ledger
    // status two of them only carry because the source refused them.
    expect(band.queryByRole("button", { name: /Never requested · 3/ })).toBeNull();
  });

  it("narrows on the same key it counted, so the chip and the table agree", async () => {
    // A band counted on the day and a table filtered on `extract_status` would
    // show three rows behind a chip that says two.
    stub({ breakdowns: [breakdown({ days: twoRefusalsAndOneAbsence() })] });
    mount();

    const band = within(await screen.findByTestId("day-state-filter"));
    await userEvent.click(
      band.getByRole("button", { name: "Prevented — the source did not allow it · 2" }),
    );

    const rows = await gridRows();
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.textContent?.slice(0, 10))).toEqual([
      "2026-07-12", "2026-07-13",
    ]);
    expect(screen.getByText(/Showing 2 of 4 days/)).toBeInTheDocument();
  });

  it("spells the refusal on the row's own pill, beside the button that names it", async () => {
    stub({ breakdowns: [breakdown({ days: twoRefusalsAndOneAbsence() })] });
    mount();

    const rows = await gridRows();
    // Row 2 is the first refusal. Its pill said « Never requested » one cell away
    // from a control named « ... did not allow this collection for 2026-07-12 ».
    const pill = within(rows[1]).getByTestId("day-extract-status");
    expect(pill).toHaveTextContent("Prevented — the source did not allow it");
    expect(pill).not.toHaveTextContent("Never requested");
    // And the day nobody asked for keeps ITS word: the repair may not swallow
    // the absence into the refusal either.
    expect(within(rows[3]).getByTestId("day-extract-status"))
      .toHaveTextContent("Never requested");
  });

  it("ends the sentence once, even when the connector's message ends it too", async () => {
    // `${what}. ` over a connector sentence that already carries its full stop
    // put « ... then re-ask these dates.. The provider is asked ... » in the
    // first line a person reads before spending quota.
    stub({ breakdowns: [breakdown({ days: [preventedDay()] })] });
    mount();

    await userEvent.click(await repullButton(/did not allow this collection for 2026-07-12/));
    const text = screen.getByTestId("repull-confirm").textContent ?? "";
    expect(text).toContain("re-ask these dates. Your daily figures are unaffected. The provider is asked");
    expect(text).not.toContain("..");
  });

  it("says a refusal carried no sentence rather than pretending it was whole", async () => {
    stub({
      breakdowns: [breakdown({
        days: [preventedDay({ prevented_message: null, prevented_reason: null })],
      })],
    });
    mount();

    const button = await repullButton(/did not allow this collection for 2026-07-12/);
    // Absence said as absence — and it still points at the only place the access
    // can come from, because « did not allow » on its own leaves nowhere to go.
    expect(button).toHaveAccessibleName(/recorded no sentence naming what releases it/);
    expect(button).toHaveAccessibleName(/obtained at meta-ads before re-asking/);
  });
});

/**
 * A WINDOW STATE THIS BUILD HAS NO WORD FOR — criterion 18.
 *
 * « A payload publishes a raw `app.pull_jobs.state` value as a status a person
 * reads, i.e. any mapping that ends by returning the column ». The server closed
 * it at `dq_api._map_state_to_status`; `jobStateLabel` still ended by returning
 * the column, so the next name added to the CHECK constraint would have arrived
 * in the `Collection window` cell of this grid as a database word.
 */
describe("A collecting window whose state this build does not know", () => {
  it("says so in words, and never shows the database's own", async () => {
    stub({
      breakdowns: [breakdown({ days: [
        { date: "2026-07-12", extract_status: "never_fetched", job_state: "quarantined",
          extract_count: 0, row_count: null, row_count_reason: null, rows: null,
          rows_reason: null, execution_id: null },
      ] })],
    });
    mount();

    const rows = await gridRows();
    expect(rows[0]).toHaveTextContent(
      "Collection state this console has no word for — reload this page, and open this day's run if it stays",
    );
    // The word from `app.pull_jobs.state` reaches no cell, no title, no
    // accessible name.
    expect(screen.queryByText(/quarantined/)).toBeNull();
    expect(document.body.innerHTML).not.toContain("quarantined");
  });
});

/**
 * RE-COLLECTING SEVERAL DAYS AT ONCE — amendment of 2026-08-18.
 *
 * The row action was the only door, so a window with sixteen holes cost sixteen
 * dialogs and sixteen `POST`s, and nobody reads the sixteenth confirmation. The
 * route has always taken an array; what was missing was a way to say which days.
 * The ways THAT can lie are these tests:
 *
 *   1. sending one request per day anyway — a bulk gesture that is a loop is a
 *      bulk gesture only on screen;
 *   2. naming the selection as an INTERVAL. Four failed days picked out of sixty
 *      are not `first → last`, and a confirmation saying so asks for sixty;
 *   3. selecting a day worth nothing. `ok` and `running` spend quota to learn
 *      nothing, and `REPAIRABLE` is the one set both doors read;
 *   4. spending without naming the connector and the account, which is the whole
 *      reason the single-day confirmation exists.
 */
describe("Re-collecting several days at once", () => {
  /** A window with one of each state that matters, `failed` included. */
  function mixedDays() {
    return [
      { date: "2026-07-10", extract_status: "ok", job_state: "done", extract_count: 1,
        row_count: 150, row_count_reason: null, rows: 4, rows_reason: null, execution_id: null },
      { date: "2026-07-11", extract_status: "empty", job_state: "done", extract_count: 1,
        row_count: 0, row_count_reason: null, rows: 0, rows_reason: null, execution_id: null },
      { date: "2026-07-12", extract_status: "never_fetched", job_state: null, extract_count: 0,
        row_count: null, row_count_reason: null, rows: null, rows_reason: null, execution_id: null },
      { date: "2026-07-13", extract_status: "failed", job_state: "failed", extract_count: 1,
        row_count: null, row_count_reason: null, rows: null, rows_reason: null, execution_id: null },
    ];
  }

  it("sends ONE request naming every selected day, and no request per day", async () => {
    const calls = stub({ breakdowns: [breakdown({ days: mixedDays() })] });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Select all 3 re-collectable/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Re-collect 3$/ }));
    await userEvent.click(screen.getByTestId("repull-go"));

    await waitFor(() =>
      expect(calls.filter((call) => call.url.includes("/refetch"))).toHaveLength(1));
    const write = calls.find((call) => call.url.includes("/refetch"))!;
    expect(write.method).toBe("POST");
    // The three repairable days, in the grid's own order — and NOT the collected
    // one that sits between them.
    expect(write.body).toEqual({ dates: ["2026-07-11", "2026-07-12", "2026-07-13"] });
  });

  it("names the count and every day, never an interval, and keeps the account", async () => {
    stub({ breakdowns: [breakdown({ days: mixedDays() })] });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Select all 3 re-collectable/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Re-collect 3$/ }));

    const dialog = screen.getByTestId("repull-confirm");
    expect(dialog).toHaveTextContent("3 days — 2026-07-11, 2026-07-12, 2026-07-13");
    // An interval would have said this, and it is exactly what must not appear:
    // the collected day between them is not being asked for.
    expect(dialog).not.toHaveTextContent("2026-07-11 → 2026-07-13");
    // The scope is still named in full before anything is spent.
    expect(dialog).toHaveTextContent("meta-ads");
    expect(dialog).toHaveTextContent(ACCOUNT);
    expect(dialog).toHaveTextContent(STREAM);
    expect(dialog).toHaveTextContent("Not measured");
    // And WHAT is missing, counted by state rather than flattened into one word.
    expect(dialog).toHaveTextContent(/1 Empty/);
    expect(dialog).toHaveTextContent(/1 Never requested/);
    expect(dialog).toHaveTextContent(/1 Failed/);
  });

  it("takes the failed days alone when that is what was asked for", async () => {
    const calls = stub({ breakdowns: [breakdown({ days: mixedDays() })] });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Select the 1 failed/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Re-collect 1$/ }));
    await userEvent.click(screen.getByTestId("repull-go"));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/refetch"))).toBe(true));
    expect(calls.find((call) => call.url.includes("/refetch"))!.body)
      .toEqual({ dates: ["2026-07-13"] });
  });

  it("offers no selection on a day that is already collected or being collected", async () => {
    stub({ breakdowns: [breakdown({ days: mixedDays() })] });
    mount();

    // The honest count, before anything is picked: three of the four days.
    expect(await screen.findByText(/3 of the 4 days shown can be re-collected/))
      .toBeInTheDocument();
    const rows = await gridRows();
    // A collected day carries no checkbox at all — not a disabled one.
    expect(within(rows[0]).queryByRole("checkbox")).toBeNull();
    expect(within(rows[1]).getByRole("checkbox")).toBeInTheDocument();
  });

  it("writes nothing until the bulk confirmation is confirmed", async () => {
    const calls = stub({ breakdowns: [breakdown({ days: mixedDays() })] });
    mount();

    await userEvent.click(await screen.findByRole("button", { name: /Select all 3 re-collectable/ }));
    await userEvent.click(screen.getByRole("button", { name: /^Re-collect 3$/ }));
    expect(calls.some((call) => call.url.includes("/refetch"))).toBe(false);

    await userEvent.click(screen.getByTestId("repull-cancel"));
    await waitFor(() => expect(screen.queryByTestId("repull-confirm")).toBeNull());
    expect(calls.some((call) => call.url.includes("/refetch"))).toBe(false);
  });
});
