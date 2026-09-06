/**
 * Story 63.3 -- the poll asks about ONE flux, and refuses an answer about
 * another.
 *
 * WHY THIS IS A FILE OF ITS OWN. Story 63.5 mounts this hook on three surfaces
 * at once, so three polls are in flight at the same moment. An answer that does
 * not name the flux it describes cannot be routed to the right one, and an
 * answer that names a DIFFERENT one, rendered, would put another project's
 * numbers under this Datastream's name. `workbenchApi.ts:17-19` already applies
 * exactly this check to the Workbench header; the poll is not allowed to be the
 * looser of the two.
 *
 * `401` is here rather than in the cadence file because it is a scope answer:
 * the identity is not (or no longer) allowed to read this flux, and asking
 * again every five seconds cannot turn that into a yes.
 */
import { act, render, screen } from "@testing-library/react";
import {
  QUIET_INTERVAL_MS,
  STOP_STATUSES,
  parseProgressEnvelope,
  progressPath,
  useDatastreamProgress,
} from "../datastreams/workbench/datastreamProgress";

const PROJECT = "proj_EXAMPLE";
const DATASTREAM = "ds_EXAMPLE";
const OTHER_PROJECT = "proj_EXAMPLE_OTHER";
const OTHER_DATASTREAM = "ds_EXAMPLE_OTHER";

function envelope(projectId: string, datastreamId: string) {
  return {
    schema: "datastream_progress.v1",
    project_id: projectId,
    datastream_id: datastreamId,
    progress: {
      execution_id: "dse_EXAMPLE",
      state: "loading",
      step: "Collect",
      day_in_progress: "2026-07-01",
      days_done: 31,
      days_total: 730,
      windows_done: 1,
      windows_total: 24,
      window_in_progress: { date_from: "2026-08-01", date_to: "2026-08-31", days: 31 },
      rows_written: 4200,
      started_at: "2026-08-06T00:00:00+00:00",
      progress_updated_at: "2026-08-06T00:04:00+00:00",
      plan_version_id: "dpv_EXAMPLE",
      mapping_version_id: "dmv_EXAMPLE",
    },
    idle: null,
  };
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response;
}

function refused(status: number, code: string): Response {
  return {
    ok: false,
    status,
    json: () => Promise.resolve({ code, message: `HTTP ${status}` }),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;
let answer: () => Response | Promise<Response>;

function Probe() {
  const poll = useDatastreamProgress(PROJECT, DATASTREAM);
  return (
    <div>
      <span data-testid="phase">{poll.phase}</span>
      <span data-testid="rows">{poll.progress?.rows_written ?? "none"}</span>
      <span data-testid="error">{poll.error ? `${poll.error.status}` : "none"}</span>
      <span data-testid="message">{poll.error?.message ?? "none"}</span>
    </div>
  );
}

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  answer = () => ok(envelope(PROJECT, DATASTREAM));
  fetchMock = vi.fn(() => Promise.resolve(answer()));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("asks at an address carrying BOTH the project and the datastream of the mount", async () => {
  render(<Probe />);
  await flush();

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const asked = String(fetchMock.mock.calls[0][0]);
  expect(asked).toBe(`/api/projects/${PROJECT}/datastreams/${DATASTREAM}/progress`);
  expect(asked).toBe(progressPath(PROJECT, DATASTREAM));
});

test("escapes both ids rather than pasting them into the path", () => {
  expect(progressPath("proj EXAMPLE/../x", "ds EXAMPLE?y")).toBe(
    "/api/projects/proj%20EXAMPLE%2F..%2Fx/datastreams/ds%20EXAMPLE%3Fy/progress",
  );
});

test("refuses an answer about another project instead of showing it", async () => {
  answer = () => ok(envelope(OTHER_PROJECT, DATASTREAM));
  render(<Probe />);
  await flush();

  expect(screen.getByTestId("phase")).toHaveTextContent("error");
  expect(screen.getByTestId("rows")).toHaveTextContent("none");
  expect(screen.getByTestId("message").textContent).toMatch(/different Project or Datastream/i);

  // And it does not keep asking: a server answering about another flux is not a
  // transient condition, it is a contract that is not being honoured.
  await advance(QUIET_INTERVAL_MS * 10);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("refuses an answer about another datastream of the same project", async () => {
  answer = () => ok(envelope(PROJECT, OTHER_DATASTREAM));
  render(<Probe />);
  await flush();

  expect(screen.getByTestId("phase")).toHaveTextContent("error");
  expect(screen.getByTestId("rows")).toHaveTextContent("none");
});

test("refuses a body that does not carry the named envelope", () => {
  expect(() => parseProgressEnvelope({ progress: null }, PROJECT, DATASTREAM)).toThrow(
    /expected envelope/i,
  );
  expect(() => parseProgressEnvelope(null, PROJECT, DATASTREAM)).toThrow(/expected envelope/i);
  expect(() =>
    parseProgressEnvelope(
      { schema: "datastream_progress.v1", project_id: PROJECT, datastream_id: DATASTREAM, progress: { state: "loading" } },
      PROJECT,
      DATASTREAM,
    ),
  ).toThrow(/which run/i);
});

test("a field the server has not measured reads null, never zero", () => {
  const parsed = parseProgressEnvelope(
    {
      schema: "datastream_progress.v1",
      project_id: PROJECT,
      datastream_id: DATASTREAM,
      progress: {
        execution_id: "dse_EXAMPLE",
        state: "created",
        rows_written: null,
        days_done: null,
        windows_total: null,
        window_in_progress: null,
      },
      idle: null,
    },
    PROJECT,
    DATASTREAM,
  );
  expect(parsed.progress?.rows_written).toBeNull();
  expect(parsed.progress?.days_done).toBeNull();
  expect(parsed.progress?.windows_total).toBeNull();
  expect(parsed.progress?.window_in_progress).toBeNull();
});

test("a refusal stops the poll at once -- 401, 403 and 404 alike", async () => {
  expect(STOP_STATUSES).toEqual([401, 403, 404]);

  for (const status of STOP_STATUSES) {
    answer = () => refused(status, status === 401 ? "unauthorized" : "not_found");
    fetchMock.mockClear();
    const view = render(<Probe />);
    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("phase")).toHaveTextContent("error");
    expect(screen.getByTestId("error")).toHaveTextContent(String(status));

    await advance(QUIET_INTERVAL_MS * 10);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    view.unmount();
  }
});
