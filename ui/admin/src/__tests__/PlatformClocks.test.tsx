/**
 * Platform clocks — the screen that puts the declared cadence next to what
 * Cloud Scheduler actually holds (`shell/pages/PlatformClocks.tsx`).
 *
 * WHAT IS GUARDED HERE is not the happy path, it is the honesty of the screen:
 *
 *   - the five verdicts render DISTINCTLY. A screen that collapses two of them
 *     is a screen that hides one;
 *   - `unknown` never reads as agreement. The observed column says it could not
 *     be read and names the reason, and the words "In sync" appear nowhere in
 *     that card. An unavailable indistinguishable from a healthy state is
 *     finding F-010, and here it would mean reporting health about a platform
 *     whose clocks nobody could see;
 *   - `unmanaged_in_gcp` — a job running in Cloud Scheduler that this platform
 *     never declared — is shown as loudly as the rest, with no declaration
 *     invented for it;
 *   - "Run now" and "Apply" DEMAND a confirmation, and CANCELLING EMITS NOTHING.
 *     Both leave the console and change something outside it; neither may ride
 *     a single click;
 *   - a failed read is reported as a failure, never as an empty list;
 *   - LAST NIGHT'S STEPS, the same clause one level down: the three silences a
 *     nightly step can leave (never started, started-and-never-finished, not
 *     recorded) render as three DISTINCT states, none of them as "OK", and the
 *     gesture over each names the operator's own scheduler rather than a
 *     deployment.
 *
 * TWO ENDPOINTS, READ SEPARATELY. The screen calls `/api/platform/clocks` and
 * `/api/platform/nightly-steps` independently and stores the two results apart,
 * so the tests below drive them apart too: a ledger outage must not read as
 * "the clocks could not be read", and neither must the reverse.
 *
 * TRANSPORT. `fetch` is stubbed per test and un-stubbed in `afterEach`, because
 * `src/lib/apiFetch.ts` is the single seam every call goes through — stubbing it
 * also proves the exact paths the screen addresses. The stub is installed INSIDE
 * each test and never in a shared setup file: a `vi.stubGlobal` left standing
 * leaks into whatever suite runs next in the same worker, which has already been
 * repaired once in this tree.
 *
 * INTERACTION. `fireEvent`, not `userEvent`, exactly as the four neighbouring
 * suites that drive a Radix dialog do (`DatastreamChangeDialog`,
 * `DatastreamOperationsDialog`, `WorkbenchDeliveryPanel`): a modal layer sets
 * `pointer-events: none` on the body, and userEvent's pointer-events check is
 * unreliable against jsdom's partial `getComputedStyle` inheritance.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PlatformClocks from "../shell/pages/PlatformClocks";

// ---------------------------------------------------------------------------
// Transport double
// ---------------------------------------------------------------------------

interface Call {
  url: string;
  method: string;
  body: string | null;
  headers: HeadersInit | undefined;
}

function resp(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** Stub fetch with a router over (url, init) -> Response. Records every call. */
function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    const urlStr = String(url);
    if (!urlStr.includes("/cache/status")) {
      calls.push({
        url: urlStr,
        method: String(init.method ?? "GET"),
        body: typeof init.body === "string" ? init.body : null,
        headers: init.headers,
      });
    }
    return Promise.resolve(handler(urlStr, init));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures — the five real clocks, one per verdict.
//
// Written in the FLAT column vocabulary of
// `infra/nango/migrations/195_platform_clock_registry.sql`, which is the
// contract `server/core/platform_clocks_api.py` serves from.
// ---------------------------------------------------------------------------

const IN_SYNC = {
  clock_name: "dispatch-nightly",
  drift_verdict: "in_sync",
  declared_schedule: "0 2 * * *",
  declared_timezone: "Europe/Paris",
  declared_target_path: "/internal/scheduler/dispatch-nightly",
  declared_http_method: "POST",
  declared_attempt_deadline_seconds: 600,
  desired_state: "enabled",
  purpose: "Dispatch the nightly pulls for every Datastream on a nightly cadence.",
  observed_at: "2026-08-02T05:00:00.000Z",
  observed_state: "enabled",
  observed_schedule: "0 2 * * *",
  observed_timezone: "Europe/Paris",
  observed_target_uri: "https://example.com/internal/scheduler/dispatch-nightly",
  observed_http_method: "POST",
  observed_attempt_deadline_seconds: 600,
  observed_last_attempt_at: "2026-08-02T00:00:00.000Z",
  observed_last_attempt_status: "OK",
  drift_detail: {},
  observation_error: null,
};

const DRIFTED = {
  clock_name: "dispatch-hourly",
  drift_verdict: "drifted",
  declared_schedule: "0 * * * *",
  declared_timezone: "Europe/Paris",
  declared_target_path: "/internal/scheduler/dispatch-hourly",
  declared_http_method: "POST",
  declared_attempt_deadline_seconds: 600,
  desired_state: "enabled",
  purpose: "Dispatch the hourly pulls.",
  observed_at: "2026-08-02T05:00:00.000Z",
  observed_state: "paused",
  observed_schedule: "0 */6 * * *",
  observed_timezone: "Europe/Paris",
  observed_target_uri: "https://example.com/internal/scheduler/dispatch-hourly",
  observed_http_method: "POST",
  observed_attempt_deadline_seconds: 600,
  observed_last_attempt_at: "2026-07-20T00:00:00.000Z",
  observed_last_attempt_status: "OK",
  drift_detail: {
    schedule: { declared: "0 * * * *", observed: "0 */6 * * *" },
    state: { declared: "enabled", observed: "paused" },
  },
  observation_error: null,
};

const MISSING = {
  clock_name: "drain-outbox",
  drift_verdict: "missing_in_gcp",
  declared_schedule: "*/5 * * * *",
  declared_timezone: "Europe/Paris",
  declared_target_path: "/internal/scheduler/drain-outbox",
  declared_http_method: "POST",
  declared_attempt_deadline_seconds: 600,
  desired_state: "enabled",
  purpose: "Publish the facts sitting in the outbox.",
  observed_at: "2026-08-02T05:00:00.000Z",
  observed_state: "absent",
  observed_schedule: null,
  observed_timezone: null,
  observed_target_uri: null,
  observed_http_method: null,
  observed_attempt_deadline_seconds: null,
  observed_last_attempt_at: null,
  observed_last_attempt_status: null,
  drift_detail: { presence: { declared: "enabled", observed: "absent" } },
  observation_error: null,
};

/** No declaration at all: this row exists only because a job was FOUND in GCP. */
const UNMANAGED = {
  clock_name: "legacy-sweeper",
  drift_verdict: "unmanaged_in_gcp",
  observed_at: "2026-08-02T05:00:00.000Z",
  observed_state: "enabled",
  observed_schedule: "*/2 * * * *",
  observed_timezone: "Etc/UTC",
  observed_target_uri: "https://example.com/internal/legacy/sweep",
  observed_http_method: "POST",
  observed_attempt_deadline_seconds: 300,
  observed_last_attempt_at: "2026-08-02T04:58:00.000Z",
  observed_last_attempt_status: "OK",
  drift_detail: {},
  observation_error: null,
};

const UNKNOWN = {
  clock_name: "poll-health",
  drift_verdict: "unknown",
  declared_schedule: "0 6 * * *",
  declared_timezone: "Europe/Paris",
  declared_target_path: "/internal/scheduler/poll-health",
  declared_http_method: "POST",
  declared_attempt_deadline_seconds: 600,
  desired_state: "enabled",
  purpose: "Refresh connection health once a day.",
  observed_at: "2026-08-02T05:00:00.000Z",
  observed_state: null,
  observed_schedule: null,
  observed_timezone: null,
  observed_target_uri: null,
  observed_http_method: null,
  observed_attempt_deadline_seconds: null,
  observed_last_attempt_at: null,
  observed_last_attempt_status: null,
  drift_detail: {},
  observation_error: "PERMISSION_DENIED reading cloudscheduler.jobs.get",
};

const ALL_FIVE = {
  clocks: [IN_SYNC, DRIFTED, MISSING, UNMANAGED, UNKNOWN],
  reconciled_at: "2026-08-02T05:00:00.000Z",
  reconciliation_error: null,
};

// Kept in step with clockRegistryContract.PLATFORM_CLOCK_ROOT, which is itself
// measured against server/core/platform_clocks_api.py rather than assumed.
const LIST = "/api/platform/clocks";
// `clockRegistryContract.NIGHTLY_STEPS_ROOT`. Outside the `/clocks/` prefix on
// purpose: under it, `nightly-steps` is a valid clock NAME.
const NIGHTLY = "/api/platform/nightly-steps";

/** One step row of the ledger response, in the shape the server derives. */
function step(
  name: string,
  state: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    step_name: name,
    step_ordinal: 0,
    state,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    error_class: null,
    declared: true,
    ...extra,
  };
}

/** A ledger payload with one night. `unresolved` is the server's own count. */
function ledger(steps: Record<string, unknown>[]): Record<string, unknown> {
  return {
    runs: [
      {
        run_id: "nrun_0123456789ABCDEFGHJKMNPQRS",
        as_of_date: "2026-08-30",
        steps,
        unresolved: steps
          .filter((entry) => entry.state !== "succeeded")
          .map((entry) => entry.step_name),
      },
    ],
    declared_steps: steps.map((entry) => String(entry.step_name)),
    has_run: true,
  };
}

const NO_NIGHT = { runs: [], declared_steps: [], has_run: false };

/** Route the two endpoints apart. Anything else falls through to the clocks. */
function stubBoth(clocks: unknown, nightly: unknown, status = 200) {
  return stubFetch((url) =>
    url.includes(NIGHTLY) ? resp(status, nightly) : resp(200, clocks),
  );
}

function card(name: string): HTMLElement {
  return screen.getByTestId(`clock-${name}`);
}

/** The screen READS two endpoints on mount (the registry and the step ledger).
 *  "Cancelling emits nothing" is therefore a statement about the WRITES, not
 *  about the total call count -- counting the total would make the assertion
 *  break every time the screen learns to read one more fact, which is exactly
 *  how a real guard gets loosened to make a suite green. */
const writes = (calls: Call[]) => calls.filter((call) => call.method !== "GET");
const reads = (calls: Call[]) => calls.filter((call) => call.method === "GET");

// ---------------------------------------------------------------------------

describe("Platform clocks — declared next to observed", () => {
  it("renders the five verdicts distinctly, each with the two columns unmerged", async () => {
    stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);

    await screen.findByTestId("clock-dispatch-nightly");

    expect(screen.getByTestId("verdict-dispatch-nightly")).toHaveTextContent("In sync");
    expect(screen.getByTestId("verdict-dispatch-hourly")).toHaveTextContent("Drifted");
    expect(screen.getByTestId("verdict-drain-outbox")).toHaveTextContent(
      "Missing in Cloud Scheduler",
    );
    expect(screen.getByTestId("verdict-legacy-sweeper")).toHaveTextContent(
      "Unmanaged in Cloud Scheduler",
    );
    expect(screen.getByTestId("verdict-poll-health")).toHaveTextContent("Not read");

    // The five labels are five DIFFERENT strings — a screen that renders two of
    // them the same has hidden one of the two.
    const labels = [
      "dispatch-nightly",
      "dispatch-hourly",
      "drain-outbox",
      "legacy-sweeper",
      "poll-health",
    ].map((name) => screen.getByTestId(`verdict-${name}`).textContent);
    expect(new Set(labels).size).toBe(5);

    // Both columns exist on a clock that has both, and they are separate regions.
    const inSync = within(card("dispatch-nightly"));
    expect(inSync.getByText("Declared")).toBeInTheDocument();
    expect(inSync.getByText("Observed")).toBeInTheDocument();
    expect(
      inSync.getByRole("region", { name: "Declared cadence for dispatch-nightly" }),
    ).toBeInTheDocument();
    expect(
      inSync.getByRole("region", { name: "Observed Cloud Scheduler job for dispatch-nightly" }),
    ).toBeInTheDocument();
  });

  it("names every field that drifted, declared beside observed", async () => {
    stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-hourly");

    const differences = within(card("dispatch-hourly")).getByRole("region", {
      name: "Differences for dispatch-hourly",
    });
    // Both halves of each difference are readable, and labelled as to which is which.
    expect(within(differences).getByText("Schedule · Declared")).toBeInTheDocument();
    expect(within(differences).getByText("Schedule · Observed")).toBeInTheDocument();
    expect(within(differences).getByText("0 */6 * * *")).toBeInTheDocument();
    expect(within(differences).getByText("State · Observed")).toBeInTheDocument();
    expect(within(differences).getByText("paused")).toBeInTheDocument();
  });

  it("does NOT read `unknown` as agreement: it says it could not be read, and why", async () => {
    stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-poll-health");

    const unread = within(card("poll-health"));
    expect(unread.getByText("Cloud Scheduler could not be read")).toBeInTheDocument();
    expect(unread.getByTestId("unknown-reason-poll-health")).toHaveTextContent(
      "PERMISSION_DENIED reading cloudscheduler.jobs.get",
    );

    // The three things it must never do: claim agreement, claim health, or open
    // an observed record as if Cloud Scheduler had answered.
    expect(unread.queryByText("In sync")).toBeNull();
    expect(unread.queryByText(/on time/i)).toBeNull();
    expect(unread.queryByRole("region", { name: "Observed Cloud Scheduler job for poll-health" })).toBeNull();

    // The declared half is still shown — it is known, and it is the half this
    // platform can vouch for. Exactly one of the two columns is unread.
    expect(
      unread.getByRole("region", { name: "Declared cadence for poll-health" }),
    ).toBeInTheDocument();
  });

  it("shows the undeclared job, and invents no declaration for it", async () => {
    stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-legacy-sweeper");

    const unmanaged = within(card("legacy-sweeper"));
    expect(unmanaged.getByTestId("declared-absent-legacy-sweeper")).toHaveTextContent(
      "Not declared on this platform",
    );
    // Its observed half IS shown — it is the only half that exists.
    expect(
      unmanaged.getByRole("region", { name: "Observed Cloud Scheduler job for legacy-sweeper" }),
    ).toBeInTheDocument();
    expect(
      unmanaged.queryByRole("region", { name: "Declared cadence for legacy-sweeper" }),
    ).toBeNull();
    // And no action is offered, because there is no declaration to edit, to
    // apply, or to say what running it would do.
    expect(unmanaged.queryByRole("button", { name: /legacy-sweeper/ })).toBeNull();
  });

  it("separates 'read, and absent' from 'never read'", async () => {
    stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-drain-outbox");

    expect(screen.getByTestId("observed-absent-drain-outbox")).toHaveTextContent(
      "holds no job with this name",
    );
    expect(screen.queryByTestId("observed-never-drain-outbox")).toBeNull();
  });

  it("says on screen that these are not Datastream schedules", async () => {
    stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-nightly");

    expect(screen.getByText("These are not Datastream schedules")).toBeInTheDocument();
    expect(screen.getByText(/Processing tab/)).toBeInTheDocument();
  });
});

describe("Platform clocks — actions with a real effect", () => {
  it("requires a confirmation before running a clock, and emits nothing on cancel", async () => {
    const calls = stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-nightly");
    expect(reads(calls)).toHaveLength(2); // the registry and the step ledger
    expect(writes(calls)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Run dispatch-nightly now" }));

    // Opening the gate is not passing through it.
    expect(await screen.findByText("Run dispatch-nightly now?")).toBeInTheDocument();
    expect(writes(calls)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByText("Run dispatch-nightly now?")).toBeNull());
    // The decisive assertion: cancelling issued NO write at all.
    expect(writes(calls)).toHaveLength(0);
    expect(calls.some((call) => call.url.includes("/run"))).toBe(false);
  });

  it("fires the clock only once the confirmation is given", async () => {
    const calls = stubFetch((_url, init) =>
      String(init.method ?? "GET") === "POST"
        ? resp(200, { status: "accepted" })
        : resp(200, ALL_FIVE),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-nightly");

    fireEvent.click(screen.getByRole("button", { name: "Run dispatch-nightly now" }));
    await screen.findByText("Run dispatch-nightly now?");
    fireEvent.click(screen.getByRole("button", { name: "Run it now" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === `${LIST}/dispatch-nightly/run`)).toBe(true),
    );
    expect(calls.find((call) => call.url.endsWith("/run"))?.method).toBe("POST");
    const run = calls.find((call) => call.url.endsWith("/run"))!;
    expect(JSON.parse(run.body ?? "{}")).toEqual({ confirm: "dispatch-nightly" });
    expect(new Headers(run.headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("requires a confirmation before applying the declaration, and emits nothing on cancel", async () => {
    const calls = stubFetch(() => resp(200, ALL_FIVE));
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-hourly");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Apply the declared cadence for dispatch-hourly to Cloud Scheduler",
      }),
    );

    expect(
      await screen.findByText("Apply the declared cadence to Cloud Scheduler?"),
    ).toBeInTheDocument();
    // What it would write is shown BEFORE it is written.
    expect(
      screen.getByRole("region", { name: "What applying dispatch-hourly would write" }),
    ).toBeInTheDocument();
    expect(writes(calls)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByText("Apply the declared cadence to Cloud Scheduler?")).toBeNull(),
    );
    expect(writes(calls)).toHaveLength(0);
    expect(calls.some((call) => call.url.includes("/apply"))).toBe(false);
  });

  it("applies the declaration once confirmed, and re-reads afterwards", async () => {
    const calls = stubFetch((_url, init) =>
      String(init.method ?? "GET") === "POST"
        ? resp(200, { status: "applied" })
        : resp(200, ALL_FIVE),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-hourly");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Apply the declared cadence for dispatch-hourly to Cloud Scheduler",
      }),
    );
    await screen.findByText("Apply the declared cadence to Cloud Scheduler?");
    fireEvent.click(screen.getByRole("button", { name: "Overwrite Cloud Scheduler" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === `${LIST}/dispatch-hourly/apply`)).toBe(true),
    );
    // A verdict computed before the write is never left standing after it.
    await waitFor(() => expect(calls.filter((call) => call.url === LIST).length).toBe(2));
  });

  it("edits the DECLARED cadence only, seeded from the declaration and not from the drift", async () => {
    const calls = stubFetch((_url, init) =>
      String(init.method ?? "GET") === "PATCH"
        ? resp(200, { status: "saved" })
        : resp(200, ALL_FIVE),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-hourly");

    fireEvent.click(
      screen.getByRole("button", { name: "Edit the declared cadence for dispatch-hourly" }),
    );
    expect(
      await screen.findByText("Edit the declared cadence for dispatch-hourly"),
    ).toBeInTheDocument();

    // Seeded from the DECLARED half ("0 * * * *"), never from what Cloud
    // Scheduler drifted to ("0 */6 * * *") — adopting the drift is the silent
    // repair this screen exists to refuse.
    const schedule = screen.getByLabelText("Schedule") as HTMLInputElement;
    expect(schedule.value).toBe("0 * * * *");

    fireEvent.change(schedule, { target: { value: "30 * * * *" } });
    fireEvent.click(screen.getByRole("button", { name: "Save declaration" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch?.url).toBe(`${LIST}/dispatch-hourly`);
    expect(JSON.parse(patch?.body ?? "{}")).toMatchObject({
      declared_schedule: "30 * * * *",
      declared_timezone: "Europe/Paris",
      desired_state: "enabled",
      confirm: "dispatch-hourly",
    });
    expect(new Headers(patch?.headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("surfaces the server's refusal instead of claiming the action succeeded", async () => {
    stubFetch((_url, init) =>
      String(init.method ?? "GET") === "POST"
        ? resp(409, { code: "clock_absent", message: "No such job in Cloud Scheduler." })
        : resp(200, ALL_FIVE),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("clock-drain-outbox");

    fireEvent.click(screen.getByRole("button", { name: "Run drain-outbox now" }));
    await screen.findByText("Run drain-outbox now?");
    fireEvent.click(screen.getByRole("button", { name: "Run it now" }));

    expect(await screen.findByText("No such job in Cloud Scheduler.")).toBeInTheDocument();
    // The dialog stays open on failure: the operator has not done the thing.
    expect(screen.getByText("Run drain-outbox now?")).toBeInTheDocument();
  });
});

describe("Platform clocks — an unread registry is not an empty one", () => {
  it("reports a failed read as a failure, never as 'no clocks'", async () => {
    stubFetch(() => resp(503, { message: "registry unavailable" }));
    render(<PlatformClocks />);

    expect(await screen.findByText("The platform clocks could not be read")).toBeInTheDocument();
    // Both panels are down in this fixture and both say "unread" -- the wording
    // is deliberately the same sentence, so the assertion names the CLOCKS one
    // rather than matching whichever came first.
    expect(
      screen.getByText(/no clock is in sync, drifted or missing/),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/they\s+are unread/).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("No platform clock is declared")).toBeNull();
  });

  it("distinguishes an EMPTY registry, and says what an empty one would mean", async () => {
    stubFetch(() => resp(200, { clocks: [], reconciled_at: null, reconciliation_error: null }));
    render(<PlatformClocks />);

    expect(await screen.findByText("No platform clock is declared")).toBeInTheDocument();
    expect(screen.getByText("Cloud Scheduler has not been read yet.")).toBeInTheDocument();
    expect(screen.queryByText("The platform clocks could not be read")).toBeNull();
  });

  it("explains a failed reconciliation once, rather than five silent unknowns", async () => {
    stubFetch(() =>
      resp(200, {
        clocks: [{ ...UNKNOWN, observation_error: "reconciliation did not run" }],
        reconciled_at: null,
        reconciliation_error: "Cloud Scheduler refused the listing: PERMISSION_DENIED.",
      }),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("clock-poll-health");

    expect(screen.getByText("The last reconciliation did not complete")).toBeInTheDocument();
    expect(
      screen.getByText(/Cloud Scheduler refused the listing: PERMISSION_DENIED\./),
    ).toBeInTheDocument();
  });
});


// ---------------------------------------------------------------------------
// Last night's steps — `execution-substrate.md` "Incomplete if" 2, locus 3
// ---------------------------------------------------------------------------

describe("Platform clocks — last night's steps", () => {
  it("lists every declared step with its outcome and its duration", async () => {
    stubBoth(
      ALL_FIVE,
      ledger([
        step("dispatch_nightly", "succeeded", { duration_ms: 4200 }),
        step("alert_check", "succeeded", { duration_ms: 320 }),
      ]),
    );
    render(<PlatformClocks />);

    await screen.findByTestId("nightly-steps");
    expect(screen.getByTestId("nightly-state-dispatch_nightly")).toHaveTextContent("Succeeded");
    expect(within(screen.getByTestId("nightly-step-dispatch_nightly")).getByText("4.2 s"))
      .toBeInTheDocument();
    expect(within(screen.getByTestId("nightly-step-alert_check")).getByText("320 ms"))
      .toBeInTheDocument();
    expect(screen.getByTestId("nightly-unresolved")).toHaveTextContent("2 steps, all succeeded");
  });

  it("renders the three silences DISTINCTLY, and none of them as an outcome", async () => {
    stubBoth(
      ALL_FIVE,
      ledger([
        step("dispatch_nightly", "succeeded", { duration_ms: 10 }),
        step("dq_monitors", "unfinished", { started_at: "2026-08-30T02:00:00.000Z" }),
        step("run_due_notebooks", "never_started"),
        step("run_due_briefings", "unrecorded"),
      ]),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("nightly-steps");

    expect(screen.getByTestId("nightly-state-dq_monitors")).toHaveTextContent(
      "Started, never finished",
    );
    expect(screen.getByTestId("nightly-state-run_due_notebooks")).toHaveTextContent(
      "Never started",
    );
    expect(screen.getByTestId("nightly-state-run_due_briefings")).toHaveTextContent(
      "Not recorded",
    );
    // No two of them share a label, and none of them borrows the success one.
    const labels = ["dq_monitors", "run_due_notebooks", "run_due_briefings"].map(
      (name) => screen.getByTestId(`nightly-state-${name}`).textContent,
    );
    expect(new Set(labels).size).toBe(3);
    expect(labels).not.toContain("Succeeded");
    expect(screen.getByTestId("nightly-unresolved")).toHaveTextContent("3 of 4 unresolved");
  });

  it("names the gesture in the operator's scheduler, never a deployment", async () => {
    stubBoth(ALL_FIVE, ledger([step("run_due_briefings", "never_started")]));
    render(<PlatformClocks />);
    await screen.findByTestId("nightly-steps");

    const row = screen.getByTestId("nightly-step-run_due_briefings");
    expect(within(row).getByText(/Run dispatch-nightly once from its card above/))
      .toBeInTheDocument();
    // The words this sentence must never contain.
    for (const forbidden of [/deploy/i, /environment variable/i, /container/i, /queue/i]) {
      expect(row.textContent ?? "").not.toMatch(forbidden);
    }
  });

  it("shows the exception CLASS of a failed step and no message", async () => {
    stubBoth(
      ALL_FIVE,
      ledger([
        step("dq_monitors", "failed", {
          error_class: "TimeoutError",
          started_at: "2026-08-30T02:00:00.000Z",
          ended_at: "2026-08-30T02:05:00.000Z",
          duration_ms: 300000,
        }),
      ]),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("nightly-steps");

    expect(screen.getByTestId("nightly-error-dq_monitors")).toHaveTextContent("TimeoutError");
    // `5 min 00 s`, the console's one duration ladder
    // (`console-presentation.md` §2). This column held its own formatter
    // until 76-1 and answered `5 min`.
    expect(within(screen.getByTestId("nightly-step-dq_monitors")).getByText("5 min 00 s"))
      .toBeInTheDocument();
  });

  it("marks a step the sequence no longer declares rather than dropping it", async () => {
    stubBoth(
      ALL_FIVE,
      ledger([step("a_retired_step", "succeeded", { declared: false, duration_ms: 5 })]),
    );
    render(<PlatformClocks />);
    await screen.findByTestId("nightly-steps");

    expect(within(screen.getByTestId("nightly-step-a_retired_step"))
      .getByText("No longer in the sequence")).toBeInTheDocument();
  });

  it("gives a night that never happened its OWN state, not a clean one", async () => {
    stubBoth(ALL_FIVE, NO_NIGHT);
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-nightly");

    expect(await screen.findByText("No night has been recorded")).toBeInTheDocument();
    expect(
      screen.getByText(/Run dispatch-nightly once from its card above/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("nightly-unresolved")).toBeNull();
  });

  it("reports an unreadable ledger as unread, never as a night with no problem", async () => {
    stubBoth(ALL_FIVE, { message: "ledger unavailable" }, 503);
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-nightly");

    expect(await screen.findByText("The nightly steps could not be read")).toBeInTheDocument();
    expect(screen.queryByText("No night has been recorded")).toBeNull();
    expect(screen.queryByTestId("nightly-unresolved")).toBeNull();
    // The clocks were readable and still read: two facts, never merged.
    expect(screen.queryByText("The platform clocks could not be read")).toBeNull();
  });

  it("still reads the ledger when the clock registry itself is down", async () => {
    stubFetch((url) =>
      url.includes(NIGHTLY)
        ? resp(200, ledger([step("dispatch_nightly", "never_started")]))
        : resp(503, { message: "registry unavailable" }),
    );
    render(<PlatformClocks />);

    expect(await screen.findByText("The platform clocks could not be read")).toBeInTheDocument();
    expect(await screen.findByTestId("nightly-state-dispatch_nightly")).toHaveTextContent(
      "Never started",
    );
  });

  it("asks the ledger at its own path, with no store", async () => {
    const calls = stubBoth(ALL_FIVE, NO_NIGHT);
    render(<PlatformClocks />);
    await screen.findByTestId("clock-dispatch-nightly");

    const asked = calls.filter((call) => call.url.includes(NIGHTLY));
    expect(asked).toHaveLength(1);
    expect(asked[0].method).toBe("GET");
    // Never under the clocks prefix, where it would collide with a clock name.
    expect(asked[0].url.includes(`${LIST}/`)).toBe(false);
  });
});
