/**
 * The control screen for WHEN a Datastream runs — AI-106.
 *
 * Until this panel there was no screen at all: `PATCH /api/datastreams/{id}`
 * accepted the cadence and the window, and the only component referencing either
 * field was `FlowSummary.tsx` — mounted nowhere, already recorded as orphaned
 * debt (deleted 2026-08-17, chantier 67-14). A setting reachable only by curl is
 * not a setting.
 *
 * `fetch` is stubbed rather than `apiFetch`, matching the workbench tests: the
 * seam guard is what proves the bearer is attached, and stubbing one level lower
 * would hide it.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SchedulePanel from "../datastreams/workbench/SchedulePanel";

const SCHEDULE = {
  datastream_id: "ds_1",
  cadence: "nightly",
  enabled: true,
  lifecycle_state: "active",
  // Lot D1: derived by the server from the pair above plus `archived_at`, so
  // the console never recombines two columns the dispatcher reads as one.
  run_state: "running",
  archived: false,
  window_days: 7,
  window_offset_days: 1,
  window_source: "date_window_days",
  arrival_hour_local: 6,
  arrival_hour_source: "datastream",
  timezone: "Europe/Paris",
  timezone_source: "project_preference",
  on_failure: "retry_at_next_hour",
  retry_count: 0,
  next_run_at: "2026-08-02T00:00:00Z",
  last_run_at: null,
  never_ran: true,
  runs_when: "when next_run_at is reached, then every day",
};

function stubFetch(handler: (url: string, init?: RequestInit) => unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo, init?: RequestInit) => {
      const result = handler(String(input), init);
      return result as Response;
    }),
  );
}

function ok(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("SchedulePanel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the frequency and the window a person can change", async () => {
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-cadence")).toBeTruthy());
    expect((screen.getByTestId("schedule-cadence") as HTMLSelectElement).value).toBe("nightly");
    expect((screen.getByTestId("schedule-window") as HTMLInputElement).value).toBe("7");
    expect((screen.getByTestId("schedule-offset") as HTMLInputElement).value).toBe("1");
  });

  it("says a Datastream has never run instead of showing an empty date", async () => {
    // `never_ran` exists because the server distinguishes "no run yet" from a
    // missing value. Rendering a blank cell would read as a display bug.
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-last-run")).toBeTruthy());
    expect(screen.getByTestId("schedule-last-run").textContent).toContain("Never run");
  });

  it("offers nightly, weekly, hourly and manual frequencies", async () => {
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-cadence")).toBeTruthy());
    const options = Array.from(
      (screen.getByTestId("schedule-cadence") as HTMLSelectElement).options,
    ).map((option) => option.value);
    expect(options).toEqual(["nightly", "weekly", "hourly", "manual"]);
  });

  it("reports a read failure as unknown, never as unset", async () => {
    stubFetch(() => ok({ message: "boom" }, 503));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByText(/Could not read the schedule/)).toBeTruthy());
    expect(screen.queryByTestId("schedule-cadence")).toBeNull();
  });

  it("sends the chosen frequency, window and instant", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    stubFetch((url, init) => {
      calls.push({ url, init });
      return ok(SCHEDULE);
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-window")).toBeTruthy());

    fireEvent.change(screen.getByTestId("schedule-cadence"), { target: { value: "hourly" } });
    fireEvent.change(screen.getByTestId("schedule-window"), { target: { value: "30" } });
    fireEvent.change(screen.getByTestId("schedule-offset"), { target: { value: "3" } });
    fireEvent.click(screen.getByTestId("schedule-save"));
    fireEvent.click(await screen.findByTestId("schedule-confirm-accept"));

    await waitFor(() => expect(calls.length).toBe(2));
    const body = JSON.parse(String(calls[1].init?.body));
    expect(calls[1].init?.method).toBe("PUT");
    expect(body.cadence).toBe("hourly");
    expect(body.window_days).toBe(30);
    expect(body.window_offset_days).toBe(3);
    expect(body.project_id).toBe("proj_1");
  });

  it("shows the server's refusal rather than a generic failure", async () => {
    // Each refusal names what to do -- an unknown cadence, an absurd window, a
    // Datastream that was never activated. Replacing it with "save failed" would
    // throw away the only actionable part.
    let first = true;
    stubFetch(() => {
      if (first) {
        first = false;
        return ok(SCHEDULE);
      }
      return ok({ message: "this Datastream has no schedule state for its current plan version" }, 422);
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-save")).toBeTruthy());

    fireEvent.click(screen.getByTestId("schedule-save"));
    fireEvent.click(await screen.findByTestId("schedule-confirm-accept"));
    await waitFor(() =>
      expect(screen.getByText(/no schedule state for its current plan version/)).toBeTruthy(),
    );
  });

  // -------------------------------------------------------------------------
  // Story 57.8 -- the arrival hour, the catch-up, and a confirmed write.
  // -------------------------------------------------------------------------

  it("shows the arrival hour with the zone it is read in, never the reader's", async () => {
    // The hour is local to the PROJECT. Resolving it against the browser would
    // show two different schedules to two people looking at one row.
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-arrival-hour")).toBeTruthy());
    expect((screen.getByTestId("schedule-arrival-hour") as HTMLSelectElement).value).toBe("6");
    expect(screen.getByTestId("schedule-arrival-zone").textContent).toContain("Europe/Paris");
  });

  it("says no arrival hour is set rather than showing midnight as a choice", async () => {
    // `0` is a legal arrival hour. Rendering it where nobody chose one would
    // report a decision that was never made.
    stubFetch(() => ok({ ...SCHEDULE, arrival_hour_local: null, arrival_hour_source: "unset" }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-arrival-hour")).toBeTruthy());
    expect((screen.getByTestId("schedule-arrival-hour") as HTMLSelectElement).value).toBe("");
    expect(screen.getByText(/No arrival hour set/)).toBeTruthy();
  });

  it("does not offer an arrival hour to a cadence that has no single arrival", async () => {
    // A3. Absent AND explained: a control greyed out without a word reads as a
    // bug, and a person cannot tell a refusal from a broken field.
    stubFetch(() => ok({ ...SCHEDULE, cadence: "hourly", on_failure: "next_hourly_run" }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-cadence")).toBeTruthy());
    expect(screen.queryByTestId("schedule-arrival-hour")).toBeNull();
    expect(screen.getByTestId("schedule-arrival-not-applicable").textContent).toMatch(
      /runs every hour/,
    );
  });

  it("says what happens when a pull fails, and counts a catch-up only once armed", async () => {
    stubFetch(() => ok(SCHEDULE));
    const view = render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await waitFor(() => expect(screen.getByTestId("schedule-on-failure")).toBeTruthy());
    expect(screen.getByTestId("schedule-on-failure").textContent).toContain(
      "Retries at the next hour",
    );
    expect(screen.queryByTestId("schedule-retry-count")).toBeNull();
    view.unmount();

    stubFetch(() => ok({ ...SCHEDULE, retry_count: 1 }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-retry-count")).toBeTruthy());
    expect(screen.getByTestId("schedule-retry-count").textContent).toContain("1");
  });

  it("names the scope before writing, and cancelling sends nothing", async () => {
    // The MCP door onto this same row is `confirmation_mode="human"`. One
    // write reached by two doors cannot have two regimes of consent.
    const calls: { url: string; init?: RequestInit }[] = [];
    stubFetch((url, init) => {
      calls.push({ url, init });
      return ok(SCHEDULE);
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arrival-hour")).toBeTruthy());

    fireEvent.change(screen.getByTestId("schedule-arrival-hour"), { target: { value: "9" } });
    fireEvent.click(screen.getByTestId("schedule-save"));

    const dialog = await screen.findByTestId("schedule-confirm");
    expect(dialog.textContent).toContain("ds_1");
    expect(dialog.textContent).toContain("06:00");
    expect(dialog.textContent).toContain("09:00");
    expect(dialog.textContent).toMatch(/provider quota/);

    fireEvent.click(screen.getByTestId("schedule-confirm-cancel"));
    await waitFor(() => expect(screen.queryByTestId("schedule-confirm")).toBeNull());
    expect(calls.filter((call) => call.init?.method === "PUT")).toEqual([]);
  });

  it("erases the arrival hour it says it is erasing", async () => {
    // The dialog announced "from 06:00 to —" and the write then omitted
    // `arrival_hour` entirely, so the server kept 06:00 and the panel reported
    // "Schedule saved." Clearing an hour from the console was impossible, and
    // the screen said the opposite. `null` is the erasure, and it is sent.
    const calls: { url: string; init?: RequestInit }[] = [];
    stubFetch((url, init) => {
      calls.push({ url, init });
      return ok({ ...SCHEDULE, arrival_hour_local: null, arrival_hour_source: "unset" });
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arrival-hour")).toBeTruthy());

    fireEvent.change(screen.getByTestId("schedule-arrival-hour"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("schedule-save"));

    const dialog = await screen.findByTestId("schedule-confirm");
    expect(dialog.textContent).toContain("Not set");
    fireEvent.click(screen.getByTestId("schedule-confirm-accept"));

    await waitFor(() => expect(calls.length).toBe(2));
    const body = JSON.parse(String(calls[1].init?.body));
    expect("arrival_hour" in body).toBe(true);
    expect(body.arrival_hour).toBeNull();
    // And the panel reflects the erasure rather than redisplaying the old hour.
    await waitFor(() =>
      expect((screen.getByTestId("schedule-arrival-hour") as HTMLSelectElement).value).toBe(""),
    );
  });

  it("sends the arrival hour once the write is confirmed", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    stubFetch((url, init) => {
      calls.push({ url, init });
      return ok({ ...SCHEDULE, arrival_hour_local: 9 });
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arrival-hour")).toBeTruthy());

    fireEvent.change(screen.getByTestId("schedule-arrival-hour"), { target: { value: "9" } });
    fireEvent.click(screen.getByTestId("schedule-save"));
    fireEvent.click(await screen.findByTestId("schedule-confirm-accept"));

    await waitFor(() => expect(calls.length).toBe(2));
    expect(JSON.parse(String(calls[1].init?.body)).arrival_hour).toBe(9);
  });

  // -------------------------------------------------------------------------
  // Lot D1 (issue #68) -- whether this Datastream is armed at all.
  //
  // `enabled` and `lifecycle_state` were typed on the payload and rendered
  // nowhere, and the PUT door refused `enabled`. A person could fill in five
  // settings on a stopped Datastream and read "Schedule saved."
  // -------------------------------------------------------------------------

  const PAUSED = { ...SCHEDULE, enabled: false, run_state: "paused" };

  it("says a configured Datastream is not collecting, instead of looking like one that is", async () => {
    stubFetch(() => ok(PAUSED));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const state = await screen.findByTestId("schedule-run-state");
    expect(state.textContent).toContain("Configured, not collecting");
    expect(state.textContent).toMatch(/no day is collected while it is stopped/);
    expect(screen.getByTestId("schedule-arm").textContent).toContain("Start collecting");
  });

  it("shows a collecting Datastream as collecting, and offers to stop it", async () => {
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const state = await screen.findByTestId("schedule-run-state");
    expect(state.textContent).toContain("Collecting on this schedule");
    expect(screen.getByTestId("schedule-arm").textContent).toContain("Stop collecting");
  });

  it("does not offer to start a draft, and names the gesture that does", async () => {
    // `publish_activate_mutation` is the single writer of `lifecycle_state` and
    // `enabled` together. A control here would be a second activation authority
    // -- and a dishonest one, since the dispatcher would still skip the row.
    stubFetch(() => ok({ ...SCHEDULE, enabled: false, lifecycle_state: "draft", run_state: "not_activated" }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const state = await screen.findByTestId("schedule-run-state");
    expect(state.textContent).toContain("Not activated yet");
    expect(state.textContent).toMatch(/setup wizard/);
    expect(screen.queryByTestId("schedule-arm")).toBeNull();
  });

  it("does not offer to start an archived Datastream", async () => {
    stubFetch(() => ok({ ...SCHEDULE, enabled: false, archived: true, run_state: "archived" }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const state = await screen.findByTestId("schedule-run-state");
    expect(state.textContent).toContain("Archived");
    expect(screen.queryByTestId("schedule-arm")).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Finding D-8 (issue #69) -- two acts of one panel, and no hierarchy between
  // them. `Stop collecting` was the QUIETER of the two while `Save schedule`
  // carried the rose, and on a stopped Datastream both carried it, which by the
  // primitive's own rule is no recommendation at all. `Button` writes its intent
  // to `data-variant`, so the weight is assertable rather than eyeballed.
  // -------------------------------------------------------------------------

  it("draws stopping a collection as the destructive act, and saving as the quieter one", async () => {
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const arm = await screen.findByTestId("schedule-arm");
    expect(arm.textContent).toContain("Stop collecting");
    expect(arm.getAttribute("data-variant")).toBe("destructive");
    // The consent this trigger opens already declares itself destructive; the
    // trigger now says the same thing before the dialog does.
    expect(screen.getByTestId("schedule-save").getAttribute("data-variant")).toBe("default");
  });

  it("keeps one recommendation on a stopped Datastream, and it is starting it", async () => {
    stubFetch(() => ok(PAUSED));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const arm = await screen.findByTestId("schedule-arm");
    expect(arm.textContent).toContain("Start collecting");
    expect(arm.getAttribute("data-variant")).toBe("default");
    // Saving settings onto something that never runs is not the next step, and
    // two rose buttons in one panel are two recommendations, which is none.
    expect(screen.getByTestId("schedule-save").getAttribute("data-variant")).toBe("secondary");
  });

  it("gives the save button the recommendation when no arming control exists", async () => {
    stubFetch(() => ok({ ...SCHEDULE, enabled: false, lifecycle_state: "draft", run_state: "not_activated" }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    await screen.findByTestId("schedule-run-state");
    expect(screen.queryByTestId("schedule-arm")).toBeNull();
    expect(screen.getByTestId("schedule-save").getAttribute("data-variant")).toBe("default");
  });

  it("reads an absent run state as unknown, never as stopped", async () => {
    // The two look identical on screen and take opposite gestures. A payload
    // that did not answer must not be drawn as an answer.
    const { run_state: _dropped, ...withoutRunState } = SCHEDULE;
    stubFetch(() => ok(withoutRunState));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);

    const state = await screen.findByTestId("schedule-run-state");
    expect(state.textContent).toMatch(/could not be read/);
    expect(state.textContent).toMatch(/unknown, not/);
    expect(screen.queryByTestId("schedule-arm")).toBeNull();
  });

  it("names what starting spends before it starts, and cancelling starts nothing", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    stubFetch((url, init) => {
      calls.push({ url, init });
      return ok(PAUSED);
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arm")).toBeTruthy());

    fireEvent.click(screen.getByTestId("schedule-arm"));
    const dialog = await screen.findByTestId("schedule-arm-confirm");
    expect(dialog.textContent).toContain("ds_1");
    expect(dialog.textContent).toMatch(/source account/);

    fireEvent.click(screen.getByTestId("schedule-arm-confirm-cancel"));
    await waitFor(() => expect(screen.queryByTestId("schedule-arm-confirm")).toBeNull());
    expect(calls.filter((call) => call.init?.method === "PUT")).toEqual([]);
  });

  it("names the days a stop loses, and how they come back", async () => {
    stubFetch(() => ok(SCHEDULE));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arm")).toBeTruthy());

    fireEvent.click(screen.getByTestId("schedule-arm"));
    const dialog = await screen.findByTestId("schedule-arm-confirm");
    expect(dialog.textContent).toMatch(/re-collection/);
    expect(dialog.textContent).toMatch(/day by day/);
  });

  it("sends only `enabled`, so a start does not commit settings nobody confirmed", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    stubFetch((url, init) => {
      calls.push({ url, init });
      return ok(SCHEDULE);
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-window")).toBeTruthy());

    // A value typed and NOT confirmed through `Save schedule`.
    fireEvent.change(screen.getByTestId("schedule-window"), { target: { value: "90" } });
    fireEvent.click(screen.getByTestId("schedule-arm"));
    fireEvent.click(await screen.findByTestId("schedule-arm-confirm-accept"));

    await waitFor(() => expect(calls.length).toBe(2));
    const body = JSON.parse(String(calls[1].init?.body));
    expect(body).toEqual({ project_id: "proj_1", enabled: false });
  });

  it("starts it, and reports the state the server answered", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    let first = true;
    stubFetch((url, init) => {
      calls.push({ url, init });
      if (first) {
        first = false;
        return ok(PAUSED);
      }
      return ok(SCHEDULE);
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arm")).toBeTruthy());

    fireEvent.click(screen.getByTestId("schedule-arm"));
    fireEvent.click(await screen.findByTestId("schedule-arm-confirm-accept"));

    await waitFor(() => expect(calls.length).toBe(2));
    expect(JSON.parse(String(calls[1].init?.body)).enabled).toBe(true);
    await waitFor(() =>
      expect(screen.getByTestId("schedule-run-state").textContent).toContain(
        "Collecting on this schedule",
      ),
    );
  });

  it("shows the server's refusal to start rather than a generic failure", async () => {
    let first = true;
    stubFetch(() => {
      if (first) {
        first = false;
        return ok({ ...SCHEDULE, enabled: false, run_state: "paused" });
      }
      return ok(
        { message: "this Datastream is archived. Restoring it is what makes it runnable again" },
        422,
      );
    });
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-arm")).toBeTruthy());

    fireEvent.click(screen.getByTestId("schedule-arm"));
    fireEvent.click(await screen.findByTestId("schedule-arm-confirm-accept"));
    await waitFor(() => expect(screen.getByText(/Restoring it/)).toBeTruthy());
  });

  it("does not report a saved schedule as an armed one", async () => {
    // The exact sentence that made the defect invisible: "Schedule saved." is
    // true on a stopped Datastream, and lets a person walk away believing a run
    // was armed.
    stubFetch(() => ok(PAUSED));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" />);
    await waitFor(() => expect(screen.getByTestId("schedule-save")).toBeTruthy());

    fireEvent.click(screen.getByTestId("schedule-save"));
    expect((await screen.findByTestId("schedule-confirm")).textContent).toMatch(
      /stopped: these settings are saved/,
    );
    fireEvent.click(screen.getByTestId("schedule-confirm-accept"));

    await waitFor(() => expect(screen.getByText(/none of it runs until it is started/)).toBeTruthy());
  });

  it("answers the question for a pushed source instead of hiding it", async () => {
    // Amendment 1 of the 2026-08-11 review: a `managed_feed` has no cadence to
    // arm. Measured: nothing in the import path reads `enabled`, so a start /
    // stop control here would stop nothing while claiming to.
    stubFetch(() => ok({ ...PAUSED, never_ran: true }));
    render(<SchedulePanel projectId="proj_1" datastreamId="ds_1" mode="managed_feed" />);

    const state = await screen.findByTestId("schedule-run-state");
    expect(state.textContent).toMatch(/still imported when it arrives/);
    expect(screen.queryByTestId("schedule-arm")).toBeNull();
    expect(screen.queryByTestId("schedule-cadence")).toBeNull();
  });
});
