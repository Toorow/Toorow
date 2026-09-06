/**
 * THE REVIEW INSTRUMENT ITSELF, WALKED.
 *
 * `/debug/screen?name=DatastreamWorkbench` is the only address at which the
 * Datastream Workbench can be looked at, so every visual review this repository
 * has run rests on what this sandbox answers. On 2026-08-12 the tour-3 review
 * measured what it had been answering: `fixtureFor` had no `/progress` branch, so
 * every call fell to its `404`; `404` is in `STOP_STATUSES` (`lib/polledRead.ts`),
 * so the poll stopped on the first tick; and `DatastreamRunLive` — mounted above
 * the tab band, therefore visible from all eight tabs — rendered « The collection
 * state answered 404 » in EVERY capture ever taken. Nobody noticed for as long as
 * the instrument existed.
 *
 * That is the class of defect this file is for: an instrument nothing measures
 * measures nothing. Each test below walks one state of the sandbox to the DOM and
 * asserts the thing that would be wrong if the fixture drifted from the wire
 * again — not that a component renders, which its own suite already proves.
 *
 * `window.fetch` is left to the sandbox. It installs its own (`sandboxApi.ts`)
 * and that installation is part of what is under test: a stub here would prove a
 * fixture nobody reaches.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import DatastreamWorkbenchSandbox from "../shell/DatastreamWorkbenchSandbox";

const REAL_FETCH = window.fetch;

/** Open the sandbox at one address, as a browser would. */
function open(search: string) {
  window.history.replaceState({}, "", `/debug/screen?name=DatastreamWorkbench${search}`);
  return render(<DatastreamWorkbenchSandbox />);
}

beforeEach(() => {
  window.history.replaceState({}, "", "/debug/screen?name=DatastreamWorkbench");
});

afterEach(() => {
  cleanup();
  window.fetch = REAL_FETCH;
});

/** The band, once it has read something. */
async function band(): Promise<HTMLElement> {
  return await screen.findByTestId("datastream-run-live");
}

describe("the live collection band", () => {
  it("answers the poll instead of refusing it", async () => {
    open("");
    const live = await band();
    // THE ASSERTION THE MISSING BRANCH WOULD FAIL. `run-live-error` is the
    // `Status` block whose title reads « The collection state answered 404 ».
    await waitFor(() => expect(within(live).queryByTestId("run-live-error")).toBeNull());
    expect(within(live).getByTestId("run-live-measured")).toBeInTheDocument();
  });

  it("shows a run in flight with its days, its origin and its estimate", async () => {
    open("&run=running");
    const live = await band();
    expect(await within(live).findByTestId("run-live-days")).toBeInTheDocument();
    expect(within(live).getByText("4 of 7 days collected")).toBeInTheDocument();
    // Story 63.7 — WHY it is running, before what it has got to.
    expect(within(live).getByTestId("run-live-origin")).toHaveTextContent("Nightly collection");
    expect(within(live).getByTestId("run-live-estimate")).toHaveTextContent("About 4 minutes left.");
    // The one gesture this band writes is offered, because something is running.
    expect(within(live).getByTestId("run-live-stop")).toBeInTheDocument();
  });

  it("shows a run that has not started as ONE sentence and no measurement", async () => {
    open("&run=not_started");
    const live = await band();
    expect(await within(live).findByTestId("run-live-not-started")).toBeInTheDocument();
    // Amendment 8: no tile, no fraction, no estimate on a run with nothing to
    // measure. A fixture that filled those blanks would hide the repair.
    expect(within(live).queryByTestId("run-live-days")).toBeNull();
  });

  it("tells the four silences apart", async () => {
    open("&run=never_ran");
    expect(await within(await band()).findByTestId("run-live-idle")).toHaveTextContent(
      "This Datastream has never run.",
    );
    cleanup();

    open("&run=idle");
    expect(await within(await band()).findByTestId("run-live-idle")).toHaveTextContent(
      /Nothing is collecting\. The last run ended as Published/,
    );
    cleanup();

    open("&run=failed");
    expect(await within(await band()).findByTestId("run-live-idle")).toHaveTextContent(
      "provider_quota_exhausted",
    );
    cleanup();

    // Story 63.6: a run somebody STOPPED says what it KEPT, and never a `0` for
    // a count nobody measured.
    open("&run=stopped");
    expect(await within(await band()).findByTestId("run-live-kept")).toHaveTextContent(
      "3 of 7 days were collected and kept, and Not measured rows landed.",
    );
  });
});

describe("the Data tab", () => {
  it("draws the day grid on the real tab route", async () => {
    open("&tab=data");
    // The refusal the missing `/daily-breakdown` branch produced.
    expect(await screen.findByText("Read this Datastream by day")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("The daily breakdown could not be read")).toBeNull(),
    );
    // `findAllBy`, because a served day is on screen in several places at once —
    // the strip, the run link and the day selector — which is itself the proof
    // that the grid, its rows and `Read one day` all received the payload.
    expect((await screen.findAllByText("2026-08-02")).length).toBeGreaterThan(1);
    // The header of the grid comes from the MAPPING and names which store
    // answered — an empty header is otherwise a guess.
    expect((await screen.findAllByText(/search_appearance/)).length).toBeGreaterThan(0);
  });

  it("offers Collected and refuses Mapped with the resolver's own sentence", async () => {
    open("&tab=data");
    // `gsc`/`page_daily` declares a raw relation and no staging model, and four
    // staging models read that relation, so the resolver answers
    // `staging_relation_not_declared`. The greyed control carries that sentence,
    // and the sandbox may not compose one of its own.
    expect(
      (await screen.findAllByText(/declares no staging model, and several read its raw relation/))
        .length,
    ).toBeGreaterThan(0);
  });

  /**
   * Finding D-7 of issue #69, on the sandbox side. The server's `sample_reason`
   * was rewritten on 2026-08-12 to stop naming a story on a user's screen, and
   * both fixtures went on sending the retired sentence — so a capture kept
   * proving this tab against a payload the product no longer composes.
   *
   * `read_tab` keys the two fields on `module_name`: a Datastream that names a
   * connector earns `reachable` and NO sentence, a `managed_feed` earns the
   * refusal written for its own mode.
   */
  it("draws the sample on a connector pull, and names no story anywhere", async () => {
    open("&tab=data");
    expect(await screen.findByText("Bounded masked sample")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/Story 47\.5/)).toBeNull());
    expect(screen.queryByText(/No governed sample endpoint is mounted/)).toBeNull();
  });

  it("refuses a sample on a file source in the words of that mode", async () => {
    open("&mode=managed_feed&tab=data");
    expect(
      await screen.findByText(
        "No sample and no export can be drawn here — this Datastream receives a file, and the"
        + " rows it received are read in the last file that arrived, above.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Story 47\.5/)).toBeNull();
  });
});

describe("the two source modes", () => {
  it("drops both conditional tabs on a file source and mounts the landed file", async () => {
    open("&mode=managed_feed&tab=data");
    // `NavTabs` is a `<nav>` of controls, not an ARIA tablist: without a
    // `tabHref` a tab is a button, which is the honest mount at `/debug/screen`.
    const tabs = await screen.findByRole("navigation", { name: "Datastream" });
    expect(within(tabs).queryByRole("button", { name: "Cost" })).toBeNull();
    expect(within(tabs).queryByRole("button", { name: "Placements" })).toBeNull();
    // Lot B1 — what the last file that ARRIVED contained.
    // The panel, not a loose text match: since the arrival-by-day reading was
    // added (2026-08-18) the file names itself twice — in the panel's own
    // heading and in the dated row it now carries. What this case guards is
    // that the landed-file panel is MOUNTED for a file source, so it names the
    // panel and reads the file inside it.
    const landed = await screen.findByTestId("landed-file");
    expect(within(landed).getAllByText(/media-plan-2026-08\.csv/).length).toBeGreaterThan(0);
    // Amendment 7: no pull axis on a pushed source.
    expect(screen.queryByText("Read this Datastream by day")).toBeNull();
  });

  it("keeps both conditional tabs on a connector pull", async () => {
    open("");
    const tabs = await screen.findByRole("navigation", { name: "Datastream" });
    expect(within(tabs).getByRole("button", { name: "Cost" })).toBeInTheDocument();
    expect(within(tabs).getByRole("button", { name: "Placements" })).toBeInTheDocument();
  });

  it("says why no file has arrived, and names the door one comes in by", async () => {
    open("&mode=managed_feed&tab=data&file=no_file_yet");
    // `findBy`, not `getBy` inside the panel: the panel carries its `data-testid`
    // while it is still reading, so a synchronous query would assert against the
    // loading state and pass for the wrong reason.
    expect(await screen.findByText("No file has arrived yet")).toBeInTheDocument();
    expect(await screen.findByText(/arrives by email/)).toBeInTheDocument();
  });

  it("keeps « could not be read » apart from « nothing has arrived »", async () => {
    open("&mode=managed_feed&tab=data&file=relation_absent");
    expect(await screen.findByText("The last file could not be read")).toBeInTheDocument();
    expect(screen.queryByText("No file has arrived yet")).toBeNull();
  });
});

describe("the Placements tab", () => {
  it("counts the matched lines and names the state of each", async () => {
    open("&tab=placements");
    // `line_counts.matched`, not `attached`: the stale key rendered
    // `Unavailable / 3` in the only preview tool built for this tab.
    expect(await screen.findByText("0 / 3")).toBeInTheDocument();
    // `matching_state_label`, not a badge composed from a boolean: the three
    // rows rendered empty badges without it. SCOPED TO THE TABLE since
    // 2026-08-18: the narrowing chips are drawn from the same server words, so
    // the label now legitimately appears a fourth time — on the control that
    // narrows by it.
    const table = within(
      await screen.findByRole("region", { name: /Plan lines and their matches/i }),
    );
    expect(await table.findAllByText("Nothing observed")).toHaveLength(3);
    expect(screen.getByRole("radio", { name: "Nothing observed" })).toBeInTheDocument();
  });

  it("re-reads a chosen plan and gets the placements payload, not the overview", async () => {
    // `…/workbench/placements?plan_id=…` ended in a query string, matched no tab
    // and answered the OVERVIEW payload: choosing a second plan showed the
    // first one's lines. The address is now split before it is matched.
    const response = await (async () => {
      open("&tab=placements");
      await screen.findByText("0 / 3");
      return window.fetch(
        "/api/projects/proj_EXAMPLE/datastreams/ds_EXAMPLE/workbench/placements?plan_id=mplan_EXAMPLE_alwayson",
      );
    })();
    const body = (await response.json()) as { tab?: string };
    expect(body.tab).toBe("placements");
  });
});
