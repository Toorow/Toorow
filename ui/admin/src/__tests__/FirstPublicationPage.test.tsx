import { act, fireEvent, render, screen } from "@testing-library/react";
import type { FirstReportReadiness, ReadinessPhase } from "../datastreams/RapportPretCard";
import FirstPublication from "../shell/pages/FirstPublication";

const VERSION = "a".repeat(32);

function makeReadiness(
  overrides: Partial<FirstReportReadiness> = {},
): FirstReportReadiness {
  return {
    schema_version: "1",
    readiness_version: VERSION,
    datastream_id: "ds1",
    project_id: "p1",
    overall: "blocked",
    host_cta: "disabled",
    headline: "No current publication",
    current_publication: null,
    selected_objects: {
      report_id: null,
      metrics: [],
      dimensions: [],
      grain: [],
      timezone: null,
      currency: null,
    },
    recent_coverage: null,
    historical_coverage: null,
    freshness: null,
    verification: null,
    dq: null,
    mapping: null,
    provenance: null,
    exclusions: [],
    last_successful_pull: null,
    phases: [],
    ...overrides,
  };
}

function currentPublication(executionId = "exec_1") {
  return { execution_id: executionId, row_count: 42, published_at: "2026-07-22T00:00:00Z" };
}

function readyReadiness(overrides: Partial<FirstReportReadiness> = {}) {
  return makeReadiness({
    overall: "ready",
    host_cta: "enabled",
    headline: "Ready",
    current_publication: currentPublication(),
    ...overrides,
  });
}

function phase(overrides: Partial<ReadinessPhase> = {}): ReadinessPhase {
  return {
    phase: "recent_pull",
    state: "succeeded",
    interval: null,
    rows: 42,
    attempts: 1,
    live_publication_execution_id: "exec_1",
    next_action: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 500,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function stubReadiness(body: unknown, ok = true) {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body, ok));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const FABRICATIONS = [
  "186,420",
  "22 minutes",
  "8 / 8 passed",
  "mapping_v17",
  "plan_v12",
  "pub_01J4A8F2",
  "Jan-Jun 2026 / 31% complete",
  "Acme Ads / account access confirmed",
  "09:02",
];

function expectNoFabrication() {
  for (const literal of FABRICATIONS) {
    expect(screen.queryByText(literal)).not.toBeInTheDocument();
  }
}

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("FirstPublication - honest publication evidence", () => {
  it("reports load failure without rendering publication evidence", async () => {
    stubReadiness({}, false);
    render(<FirstPublication projectId="p1" datastreamId="ds1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/not a published result/i);
    expect(screen.queryByRole("heading", { name: "Publication readiness" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Published/)).not.toBeInTheDocument();
    expectNoFabrication();
  });

  it("does not fetch when no Datastream is scoped", async () => {
    const fetchMock = stubReadiness({});
    render(<FirstPublication />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/no Datastream is scoped/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renders a valid blocked response as not published and exposes no data or LKG action", async () => {
    stubReadiness(makeReadiness());
    render(<FirstPublication projectId="p1" datastreamId="ds1" />);

    expect(await screen.findByText("Not published")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /View published data/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Last-known-good evidence/i)).not.toBeInTheDocument();
    expectNoFabrication();
  });

  it.each([
    ["wrong scope", { datastream_id: "ds-other" }],
    ["unknown overall enum", { overall: "green" }],
    ["overall/CTA disagreement", { overall: "ready", host_cta: "disabled", current_publication: currentPublication() }],
    ["degraded without publication", { overall: "degraded", host_cta: "degraded", current_publication: null }],
    ["invalid readiness id", { readiness_version: "v1" }],
  ])("rejects %s before rendering it", async (_label, overrides) => {
    stubReadiness({ ...makeReadiness(), ...overrides });
    render(<FirstPublication projectId="p1" datastreamId="ds1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/invalid readiness response/i);
    expect(screen.queryByText(/^Published/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /View published data/i })).not.toBeInTheDocument();
  });

  it("renders only validated server phases and current-publication evidence", async () => {
    stubReadiness(
      readyReadiness({
        dq: {
          total_unresolved: 0,
          monitors_unavailable: false,
          degraded: false,
          evaluated_days_30d: 30,
        },
        phases: [phase()],
      }),
    );
    render(<FirstPublication projectId="p1" datastreamId="ds1" />);

    expect(await screen.findByText("Published")).toBeInTheDocument();
    expect(screen.getByText("Recent extraction")).toBeInTheDocument();
    expect(screen.getByText("No unresolved findings")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /View published data/i })).toBeInTheDocument();
    expect(screen.getByText(/Last-known-good evidence/i)).toBeInTheDocument();
    expect(screen.queryByText(/retry time|route to Runs|If the next historical/i)).not.toBeInTheDocument();
    expectNoFabrication();
  });

  it("does not turn zero evaluated days into a quality all-clear", async () => {
    stubReadiness(
      readyReadiness({
        dq: {
          total_unresolved: 0,
          monitors_unavailable: false,
          degraded: false,
          evaluated_days_30d: 0,
        },
      }),
    );
    render(<FirstPublication projectId="p1" datastreamId="ds1" />);

    expect(await screen.findByText("Not evaluated")).toBeInTheDocument();
    expect(screen.queryByText("No unresolved findings")).not.toBeInTheDocument();
    expect(screen.getByText(/No quality-evaluation day/i)).toBeInTheDocument();
  });

  it("keeps a server-degraded host CTA enabled and shows its caveat", async () => {
    const onConnectHost = vi.fn();
    stubReadiness(
      makeReadiness({
        overall: "degraded",
        host_cta: "degraded",
        headline: "Recent publication is degraded",
        current_publication: currentPublication(),
      }),
    );
    render(
      <FirstPublication projectId="p1" datastreamId="ds1" onConnectHost={onConnectHost} />,
    );

    const button = await screen.findByRole("button", { name: /Connect MCP host/i });
    expect(button).toBeEnabled();
    expect(screen.getByText(/server reports degraded readiness/i)).toBeInTheDocument();
    fireEvent.click(button);
    expect(onConnectHost).toHaveBeenCalledOnce();
  });
});

describe("FirstPublication - polling isolation", () => {
  it("ignores an older scope response even when its fetch implementation ignores abort", async () => {
    let resolveFirst!: (response: Response) => void;
    const firstResponse = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    let firstSignal: AbortSignal | undefined;
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/ds1/")) {
        firstSignal = init?.signal ?? undefined;
        return firstResponse;
      }
      return Promise.resolve(
        jsonResponse(
          readyReadiness({ datastream_id: "ds2", headline: "Second scope evidence" }),
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const view = render(<FirstPublication projectId="p1" datastreamId="ds1" />);
    view.rerender(<FirstPublication projectId="p1" datastreamId="ds2" />);

    expect(await screen.findByText("Second scope evidence")).toBeInTheDocument();
    expect(firstSignal?.aborted).toBe(true);

    await act(async () => {
      resolveFirst(jsonResponse(readyReadiness({ headline: "First scope evidence" })));
      await Promise.resolve();
    });
    expect(screen.queryByText("First scope evidence")).not.toBeInTheDocument();
  });

  it("never overlaps polls and stops after a terminal response", async () => {
    vi.useFakeTimers();
    let resolveFirst!: (response: Response) => void;
    const firstResponse = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const running = makeReadiness({
      overall: "degraded",
      host_cta: "degraded",
      headline: "History running",
      current_publication: currentPublication(),
      phases: [phase({ phase: "history", state: "running" })],
    });
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => firstResponse)
      .mockResolvedValueOnce(jsonResponse(readyReadiness({ headline: "Terminal evidence" })));
    vi.stubGlobal("fetch", fetchMock);

    render(<FirstPublication projectId="p1" datastreamId="ds1" />);
    await act(async () => Promise.resolve());
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst(jsonResponse(running));
      await Promise.resolve();
    });
    expect(screen.getByText("History running")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Terminal evidence")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("caps automatic retries after twenty attempts", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);

    render(<FirstPublication projectId="p1" datastreamId="ds1" />);
    await act(async () => Promise.resolve());
    await act(async () => {
      await vi.runAllTimersAsync();
    });

    expect(fetchMock).toHaveBeenCalledTimes(20);
  });});
