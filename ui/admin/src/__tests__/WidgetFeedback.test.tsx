import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import WidgetFeedback from "../shell/pages/WidgetFeedback";
import exactFixture from "./fixtures/feedbackReviewExact.json";

const PROJECT = exactFixture.collection.project_id;
const COLLECTION = exactFixture.collection;
const ANNOTATION = COLLECTION.items.find((item) => item.review.current_state === "triaged")!;
const NORMALIZED_FILTERS = exactFixture.aggregates.normalized_filters;
const AGGREGATES = exactFixture.aggregates;

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(
  implementation?: (url: string, init?: RequestInit) => Promise<Response>,
) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (implementation) return implementation(url, init);
    if (url.includes("/feedback/aggregates")) return response(AGGREGATES);
    if (url.includes("/feedback/critical-negatives")) return response(exactFixture.critical_negatives);
    if (url.includes("/feedback?")) {
      return response({
        ...COLLECTION,
      });
    }
    return response({ code: "not_found", message: "Not found" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("reads nothing without an exact Project scope", () => {
  const { fetchMock } = mockApi();
  render(<WidgetFeedback />);
  expect(screen.getByText(/Feedback is Project-scoped/i)).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("keeps the labelled native filters mounted and bounds the window to 90 days", async () => {
  const { calls } = mockApi();
  render(<WidgetFeedback projectId={PROJECT} />);
  const form = screen.getByRole("form", { name: "Feedback aggregate filters" });
  expect(form).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/Reading the feedback review/i);
  expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");

  const from = screen.getByLabelText(/^Observed from/) as HTMLInputElement;
  const to = screen.getByLabelText(/^Observed to/) as HTMLInputElement;
  expect(from).toBeRequired();
  expect(to).toBeRequired();
  expect(from.max).toBe(to.value);
  expect(to.min).toBe(from.value);
  expect(
    (new Date(`${to.max}T00:00:00Z`).getTime() - new Date(`${from.value}T00:00:00Z`).getTime()) /
      86_400_000,
  ).toBe(89);

  await screen.findByText(/Feedback results loaded/i);
  expect(calls).toHaveLength(3);
  expect(calls.filter((call) => !call.url.includes("critical-negatives")).every(
    (call) => call.url.includes("observed_from=") && call.url.includes("observed_to="),
  )).toBe(true);
});

it("applies and clears typed filters through keyboard-operable native controls", async () => {
  const { calls } = mockApi();
  render(<WidgetFeedback projectId={PROJECT} />);
  await screen.findByText(/Feedback results loaded/i);

  fireEvent.change(screen.getByLabelText("Source"), { target: { value: "anonymous_share" } });
  fireEvent.change(screen.getByLabelText("Result type"), { target: { value: "breakdown" } });
  fireEvent.submit(screen.getByRole("form", { name: "Feedback aggregate filters" }));
  await waitFor(() =>
    expect(calls.filter((call) => call.url.includes("source=anonymous_share"))).toHaveLength(2),
  );
  const filteredCalls = calls.filter((call) => call.url.includes("source=anonymous_share"));
  expect(filteredCalls.every((call) => call.url.includes("result_type=breakdown"))).toBe(true);

  fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
  await waitFor(() => expect(calls).toHaveLength(9));
  expect(screen.getByLabelText("Source")).toHaveValue("");
});

it("shows five named cohort tables without a thumbs-up percentage or grand total", async () => {
  mockApi();
  render(<WidgetFeedback projectId={PROJECT} />);
  for (const name of ["Business Domain", "Skill", "Semantic View", "Capability", "Result type"]) {
    expect(await screen.findByRole("region", { name: `Feedback by ${name}` })).toBeInTheDocument();
  }
  expect(screen.getAllByText("1 / 1").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/Buckets overlap and must not be summed/i)).toHaveLength(2);
  expect(screen.queryByText(/^\d+\s?%$/)).not.toBeInTheDocument();
  expect(screen.queryByText(/^grand total$/i)).not.toBeInTheDocument();
});

it("renders user polarity and human review separately from detail-only automated verdicts", async () => {
  mockApi();
  render(<WidgetFeedback projectId={PROJECT} />);
  const table = await screen.findByRole("region", { name: "Feedback annotations" });
  expect(within(table).getByTestId(`polarity-${ANNOTATION.id}`)).toHaveTextContent("negative");
  expect(within(table).getByTestId(`human-${ANNOTATION.id}`)).toHaveTextContent("Triaged");
  expect(within(table).getByTestId(`automated-${ANNOTATION.id}`)).toHaveTextContent(
    /Open the exact detail/i,
  );
});

it("keeps normalized filters visible beside the cohorts", async () => {
  mockApi();
  render(<WidgetFeedback projectId={PROJECT} />);
  const filters = await screen.findAllByText(
    new RegExp(`observed_from: ${NORMALIZED_FILTERS.observed_from}`),
  );
  expect(filters.length).toBeGreaterThan(1);
});

it("continues truncated aggregate cohorts with the server cursor", async () => {
  const first = {
    ...AGGREGATES,
    truncated: true,
    next_cursor: "agg-next",
  };
  const added = {
    ...AGGREGATES.axes.business_domain.buckets[0],
    key: { id: "bd_NEXT", version_number: 2, name: "Next domain" },
    compatibility_key: "next-compatible",
  };
  const { calls } = mockApi(async (url) => {
    if (url.includes("/feedback/aggregates")) {
      return response(url.includes("cursor=agg-next")
        ? {
            ...AGGREGATES,
            axes: {
              ...AGGREGATES.axes,
              business_domain: { ...AGGREGATES.axes.business_domain, buckets: [added] },
            },
            truncated: false,
            next_cursor: null,
          }
        : first);
    }
    if (url.includes("/feedback/critical-negatives")) return response(exactFixture.critical_negatives);
    return response(COLLECTION);
  });
  render(<WidgetFeedback projectId={PROJECT} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load next aggregate cohorts" }));
  await waitFor(() => expect(calls.some((call) => call.url.includes("cursor=agg-next"))).toBe(true));
  expect(await screen.findByText("next-compatible")).toBeInTheDocument();
  expect(calls.some((call) => call.url.includes("cursor=agg-next"))).toBe(true);
  expect(screen.queryByRole("button", { name: "Load next aggregate cohorts" })).not.toBeInTheDocument();
});

it("shows the bounded unresolved critical-negative queue and opens its exact feedback", async () => {
  const open = vi.fn();
  mockApi();
  render(<WidgetFeedback projectId={PROJECT} onOpenFeedback={open} />);
  const queue = await screen.findByRole("region", { name: "Unresolved critical negative feedback" });
  fireEvent.click(within(queue).getByRole("button", { name: exactFixture.critical_negatives.items[0].comment }));
  expect(open).toHaveBeenCalledWith(exactFixture.critical_negatives.items[0].id);
  expect(screen.getByText(/50 unresolved critical negative annotations per server page/i)).toBeInTheDocument();
});

it("continues the critical-negative queue with its server cursor", async () => {
  const first = {
    ...exactFixture.critical_negatives,
    truncated: true,
    next_cursor: "critical-next",
  };
  const added = {
    ...exactFixture.critical_negatives.items[0],
    id: "fba_CRITICAL_NEXT",
    comment: "Later critical feedback",
  };
  const { calls } = mockApi(async (url) => {
    if (url.includes("/feedback/aggregates")) return response(AGGREGATES);
    if (url.includes("/feedback/critical-negatives")) {
      return response(url.includes("cursor=critical-next")
        ? { ...first, items: [added], truncated: false, next_cursor: null }
        : first);
    }
    return response(COLLECTION);
  });

  render(<WidgetFeedback projectId={PROJECT} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load next critical negatives" }));

  expect(await screen.findByText("Later critical feedback")).toBeInTheDocument();
  expect(calls.some((call) => call.url.includes("cursor=critical-next"))).toBe(true);
  expect(screen.queryByRole("button", { name: "Load next critical negatives" })).not.toBeInTheDocument();
});

it("aborts and ignores a critical-negative continuation when the Project changes", async () => {
  const first = {
    ...exactFixture.critical_negatives,
    truncated: true,
    next_cursor: "critical-stale",
  };
  const otherProject = `${PROJECT}_OTHER`;
  const otherItem = {
    ...exactFixture.critical_negatives.items[0],
    id: "fba_OTHER_PROJECT",
    comment: "Other Project critical feedback",
  };
  const staleItem = {
    ...exactFixture.critical_negatives.items[0],
    id: "fba_STALE_PROJECT",
    comment: "Stale Project critical feedback",
  };
  let continuationSignal: AbortSignal | null = null;
  let resolveContinuation: ((value: Response) => void) | undefined;
  mockApi(async (url, init) => {
    if (url.includes("/feedback/aggregates")) return response(AGGREGATES);
    if (url.includes("/feedback/critical-negatives")) {
      if (url.includes("cursor=critical-stale")) {
        continuationSignal = init?.signal ?? null;
        return new Promise<Response>((resolve) => { resolveContinuation = resolve; });
      }
      return response(url.includes(otherProject)
        ? { ...first, items: [otherItem], truncated: false, next_cursor: null }
        : first);
    }
    return response(COLLECTION);
  });

  const view = render(<WidgetFeedback projectId={PROJECT} />);
  fireEvent.click(await screen.findByRole("button", { name: "Load next critical negatives" }));
  await waitFor(() => expect(continuationSignal).not.toBeNull());
  view.rerender(<WidgetFeedback projectId={otherProject} />);

  expect(await screen.findByText("Other Project critical feedback")).toBeInTheDocument();
  await waitFor(() => expect(continuationSignal?.aborted).toBe(true));
  resolveContinuation?.(response({ ...first, items: [staleItem], truncated: false, next_cursor: null }));
  await waitFor(() => expect(screen.queryByText("Stale Project critical feedback")).not.toBeInTheDocument());
});

it("states server failures and fabricates no annotations", async () => {
  mockApi(async () => response({ code: "unavailable", message: "feedback service unavailable" }, 503));
  render(<WidgetFeedback projectId={PROJECT} />);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/feedback service unavailable/i);
  expect(alert).toHaveTextContent(/No annotation and no figure has been fabricated/i);
  expect(screen.getByRole("form", { name: "Feedback aggregate filters" })).toBeInTheDocument();
});
