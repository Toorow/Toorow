import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import ProjectMapping from "../shell/pages/ProjectMapping";

function response(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const FIELDS = [
  {
    name: "revenue",
    display_name: "Revenue",
    data_type: "numeric",
    field_kind: "metric",
    used_by_count: 2,
  },
  {
    name: "campaign",
    display_name: "Campaign",
    data_type: "text",
    field_kind: "dimension",
    used_by_count: 1,
  },
];

const CONFLICTS = [
  {
    field: { name: "revenue" },
    conflict: {
      code: "CURRENCY_CONFLICT",
      message: "Two currencies require a governed resolution.",
      affected_streams: ["ds-one", "ds-two"],
    },
    resolutions_by_module: {},
  },
];

function mappingFetch(fields: unknown = FIELDS, conflicts: unknown = CONFLICTS) {
  return vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    return Promise.resolve(
      url.includes("/api/datamodel/fields")
        ? response(200, fields)
        : response(200, conflicts),
    );
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("loads both authorized reads for the exact project and renders only proven evidence", async () => {
  localStorage.setItem("api_token", "test-token");
  const fetchMock = mappingFetch();
  vi.stubGlobal("fetch", fetchMock);

  render(<ProjectMapping projectId="project/acme" />);

  expect(screen.getByRole("status", { name: "Loading project mapping evidence" })).toBeInTheDocument();
  expect(await screen.findByText("None (verified)")).toBeInTheDocument();
  expect(screen.getByText("1 blocking")).toBeInTheDocument();
  expect(screen.getByText("2", { selector: "strong.font-numeric" })).toBeInTheDocument();

  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(fetchMock.mock.calls.map((call) => String(call[0]))).toEqual(
    expect.arrayContaining([
      "/api/datamodel/fields?project_id=project%2Facme",
      "/api/mdm/conflicts?project_id=project%2Facme",
    ]),
  );
  for (const [, init] of fetchMock.mock.calls) {
    expect(init).toEqual(
      expect.objectContaining({
        method: "GET",
        cache: "no-store",
        headers: expect.objectContaining({ Authorization: "Bearer test-token" }),
      }),
    );
  }

  expect(screen.queryByText(/Fully mapped/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/22 Jul/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/Google Ads/i)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "+ Add concept" })).toBeDisabled();
  expect(screen.getByRole("button", { name: /Open evidence for Revenue unavailable/i })).toBeDisabled();
  // The header no longer carries a disabled `Resolve conflict`: the gesture is
  // on the conflict itself, and both routes it calls have been served since
  // story 13.2 — the reason that button gave was never true.
  expect(screen.queryByRole("button", { name: "Resolve conflict" })).not.toBeInTheDocument();
});

test("a conflict carries the gesture that closes it, on the screen that reports it", async () => {
  localStorage.setItem("api_token", "test-token");
  vi.stubGlobal(
    "fetch",
    mappingFetch(FIELDS, [
      {
        field: { name: "revenue", display_name: "Revenue" },
        conflict: {
          code: "CURRENCY_CONFLICT",
          message: "Two currencies require a governed resolution.",
          affected_streams: ["ds-one"],
        },
        resolutions_by_module: { "meta-ads": null },
      },
    ]),
  );

  render(<ProjectMapping projectId="project/acme" />);

  fireEvent.click(await screen.findByText("1 blocking"));
  fireEvent.click(screen.getByRole("button", { name: "Resolve" }));

  // The dialog opens on the conflict itself, with the source the server named.
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /Resolve Revenue/ })).toBeInTheDocument();
  expect(screen.getByDisplayValue("meta-ads")).toBeInTheDocument();
});

test("requires a project and never calls the API without one", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);

  render(<ProjectMapping />);

  expect(screen.getByTestId("mapping-unconfigured")).toHaveTextContent(
    "Project mapping is not configured",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

test("distinguishes empty, denied, degraded, and schema-error responses", async () => {
  vi.stubGlobal("fetch", mappingFetch([], []));
  const empty = render(<ProjectMapping projectId="empty" />);
  expect(await screen.findByTestId("mapping-empty")).toHaveTextContent("No mapped concepts yet");
  empty.unmount();

  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(404, { code: "not_found" })));
  const denied = render(<ProjectMapping projectId="foreign" />);
  expect(await screen.findByTestId("mapping-denied")).toHaveTextContent("Project mapping unavailable");
  denied.unmount();

  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(503, { code: "db_error" })));
  const degraded = render(<ProjectMapping projectId="degraded" />);
  expect(await screen.findByTestId("mapping-error")).toHaveTextContent("could not be loaded");
  expect(screen.queryByText("None (verified)")).not.toBeInTheDocument();
  degraded.unmount();

  vi.stubGlobal(
    "fetch",
    mappingFetch([{ ...FIELDS[0], used_by_count: "2" }], []),
  );
  render(<ProjectMapping projectId="malformed" />);
  expect(await screen.findByTestId("mapping-schema_error")).toHaveTextContent(
    "could not be verified",
  );
});

test("maps an explicit capability response to unconfigured", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(response(503, { code: "not_configured" })),
  );

  render(<ProjectMapping projectId="project-one" />);

  expect(await screen.findByTestId("mapping-unconfigured")).toHaveTextContent(
    "Project mapping is not configured",
  );
});

test("does not claim no conflicts when the conflict read fails", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      Promise.resolve(
        String(input).includes("/api/datamodel/fields")
          ? response(200, FIELDS)
          : response(500, { code: "db_error" }),
      ),
    ),
  );

  render(<ProjectMapping projectId="project-one" />);

  expect(await screen.findByTestId("mapping-error")).toBeInTheDocument();
  expect(screen.queryByText("None (verified)")).not.toBeInTheDocument();
  expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
});

test("fails closed when a conflict cannot join to a returned concept", async () => {
  vi.stubGlobal(
    "fetch",
    mappingFetch(FIELDS, [
      {
        field: { name: "other-project-field" },
        conflict: { code: "MEASURE_NULL", message: "Missing measure", affected_streams: [] },
      },
    ]),
  );

  render(<ProjectMapping projectId="project-one" />);

  expect(await screen.findByTestId("mapping-schema_error")).toBeInTheDocument();
  expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
});

test("never renders prior-project concepts under a new project key", async () => {
  const pendingResolvers: Array<(value: Response) => void> = [];
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("project-one")) {
      return Promise.resolve(
        url.includes("/api/datamodel/fields") ? response(200, FIELDS) : response(200, []),
      );
    }
    return new Promise<Response>((resolve) => pendingResolvers.push(resolve));
  });
  vi.stubGlobal("fetch", fetchMock);

  const view = render(<ProjectMapping projectId="project-one" />);
  expect(await screen.findByText("Revenue")).toBeInTheDocument();

  await act(async () => {
    view.rerender(<ProjectMapping projectId="project-two" />);
    await Promise.resolve();
  });

  expect(screen.getByRole("status", { name: "Loading project mapping evidence" })).toBeInTheDocument();
  expect(screen.queryByText("Revenue")).not.toBeInTheDocument();

  await act(async () => {
    pendingResolvers[0]?.(response(200, [{ ...FIELDS[0], name: "spend", display_name: "Spend" }]));
    pendingResolvers[1]?.(response(200, []));
  });
  await waitFor(() => expect(screen.getByText("Spend")).toBeInTheDocument());
});

test("renders advisory and blocking conflict evidence in accessible details", async () => {
  vi.stubGlobal(
    "fetch",
    mappingFetch(FIELDS, [
      {
        field: { name: "revenue" },
        conflict: {
          code: "TIMEZONE_DAY_OFFSET",
          message: "Daily boundaries differ across source timezones.",
          affected_streams: ["ds-paris", "ds-new-york"],
          severity: "advisory",
        },
      },
      {
        field: { name: "revenue" },
        conflict: {
          code: "CURRENCY_CONFLICT",
          message: "Currencies must be resolved before aggregation.",
          affected_streams: ["ds-eur", "ds-usd"],
          severity: "refusal",
        },
      },
    ]),
  );

  render(<ProjectMapping projectId="project-one" />);

  const details = await screen.findByLabelText("Conflict evidence for Revenue");
  expect(details).toHaveTextContent("1 blocking, 1 advisory");
  fireEvent.click(details.querySelector("summary") as HTMLElement);
  expect(details).toHaveAttribute("open");
  expect(details).toHaveTextContent("TIMEZONE_DAY_OFFSET");
  expect(details).toHaveTextContent("Daily boundaries differ across source timezones.");
  expect(details).toHaveTextContent("ds-paris");
  expect(details).toHaveTextContent("CURRENCY_CONFLICT");
  expect(details).toHaveTextContent("Currencies must be resolved before aggregation.");
  expect(details).toHaveTextContent("ds-usd");
});

test("gives denied deterministic precedence over concurrent failures and unconfigured codes", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes("/api/datamodel/fields")) {
        return Promise.resolve(response(503, { code: "db_error" }));
      }
      return Promise.resolve(response(403, { code: "not_configured" }));
    }),
  );

  render(<ProjectMapping projectId="project-one" />);

  expect(await screen.findByTestId("mapping-denied")).toHaveTextContent(
    "Project mapping unavailable",
  );
  expect(screen.queryByTestId("mapping-unconfigured")).not.toBeInTheDocument();
});

test("aborts bounded mapping reads and retry starts fresh signals", async () => {
  vi.useFakeTimers();
  try {
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal as AbortSignal;
        signals.push(signal);
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("Timed out", "AbortError")),
          { once: true },
        );
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ProjectMapping projectId="project-one" />);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(signals).toHaveLength(2);
    expect(signals.every((signal) => !signal.aborted)).toBe(true);

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByTestId("mapping-error")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(signals.slice(2).every((signal) => !signal.aborted)).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});