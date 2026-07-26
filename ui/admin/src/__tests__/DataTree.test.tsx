/**
 * DataTree — the sidebar Data navigation must never render a datastream it did
 * not read.
 *
 * Until 2026-07-25 this component made no network call at all: it rendered seven
 * invented streams with the real connector logos, one carrying a rose "Needs
 * attention" light, plus hard-coded counts (7 / 4 / 2 / 1). These tests pin the
 * three states — loading, error, empty — and assert that none of that fiction can
 * come back.
 */
import { render, screen, waitFor } from "@testing-library/react";
import DataTree from "../shell/DataTree";
import { RouterProvider } from "../shell/router";

/** Every fabricated label the old component shipped to production. */
const FICTION = [
  "Campaign performance",
  "Search performance",
  "Reach & frequency",
  "Website acquisition",
  "Conversion paths",
  "Media plan 2026",
  "Sales targets",
];

function renderTree() {
  return render(
    <RouterProvider defaultProject="proj_test">
      <DataTree />
    </RouterProvider>,
  );
}

function expectNoFiction() {
  for (const label of FICTION) {
    expect(screen.queryByText(label)).not.toBeInTheDocument();
  }
}

/** Route requests by URL; anything unmatched fails loudly rather than silently. */
function routeFetch(handlers: Record<string, () => Response | Promise<Response>>) {
  return vi.fn().mockImplementation((url: string) => {
    for (const [key, make] of Object.entries(handlers)) {
      if (String(url).includes(key)) return Promise.resolve(make());
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
}

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}
function fail(status: number, code: string, message: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message }),
  } as unknown as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DataTree — error state", () => {
  it("says the load failed, offers a retry, and shows no invented stream", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/api/datastreams": () => fail(500, "unavailable", "Lecture indisponible"),
        "/api/connections": () => fail(500, "unavailable", "nope"),
        "/api/modules/available": () => fail(500, "unavailable", "nope"),
      }),
    );

    renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("tree-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("tree-error")).toHaveTextContent(/Couldn't load datastreams/i);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();

    expectNoFiction();
    // No count may be asserted when nothing was read.
    expect(screen.queryByTestId("count-datastreams")).not.toBeInTheDocument();
  });

  it("omits the Sources and Modules counts when those reads fail", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/api/datastreams": () => ok([]),
        "/api/connections": () => fail(500, "unavailable", "nope"),
        "/api/modules/available": () => fail(500, "unavailable", "nope"),
      }),
    );

    renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("tree-empty")).toBeInTheDocument();
    });
    // The old hard-coded badges were "4", "2" and "1 active".
    expect(screen.getByTestId("sec-data-sources")).toHaveTextContent(/^Sources$/);
    expect(screen.getByTestId("sec-data-modules")).toHaveTextContent(/^Modules$/);
  });
});

describe("DataTree — empty state", () => {
  it("states there is no datastream instead of listing any", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/api/datastreams": () => ok([]),
        "/api/connections": () => ok({ connections: [] }),
        "/api/modules/available": () => ok([]),
      }),
    );

    renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("tree-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("tree-empty")).toHaveTextContent(/No datastream yet/i);
    expectNoFiction();
    expect(screen.getByTestId("count-datastreams")).toHaveTextContent("0");
    // Search over an empty fleet would be theatre.
    expect(screen.queryByLabelText("Find Datastreams")).not.toBeInTheDocument();
  });

  it("never shows an attention light when nothing was read", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/api/datastreams": () => ok([]),
        "/api/connections": () => ok({ connections: [] }),
        "/api/modules/available": () => ok([]),
      }),
    );

    const { container } = renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("tree-empty")).toBeInTheDocument();
    });
    expect(container.querySelector(".attention-light")).toBeNull();
  });
});

describe("DataTree — ready state", () => {
  const ROWS = [
    { id: "ds_001", name: "Meta paid", module_name: "meta-ads", published_state: "published" },
    { id: "ds_002", name: "GA4 web", module_name: "google_analytics", published_state: "failed" },
  ];

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/api/datastreams": () => ok(ROWS),
        "/api/connections": () => ok({ connections: [{ id: "c1" }, { id: "c2" }] }),
        "/api/modules/available": () =>
          ok([
            { module_name: "country-split", enabled: true },
            { module_name: "currency-fx", enabled: false },
          ]),
      }),
    );
  });

  it("renders only the datastreams the API returned, with real counts", async () => {
    renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("stream-ds_001")).toBeInTheDocument();
    });
    expect(screen.getByText("Meta paid")).toBeInTheDocument();
    expect(screen.getByText("GA4 web")).toBeInTheDocument();
    expectNoFiction();

    expect(screen.getByTestId("count-datastreams")).toHaveTextContent("2");
    expect(screen.getByTestId("sec-data-sources")).toHaveTextContent("2");
    expect(screen.getByTestId("sec-data-modules")).toHaveTextContent("1 active");
  });

  it("marks attention only for a stream whose published state is not healthy", async () => {
    renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("stream-ds_002")).toBeInTheDocument();
    });
    expect(screen.getByTestId("stream-ds_002").querySelector(".attention-light")).not.toBeNull();
    expect(screen.getByTestId("stream-ds_001").querySelector(".attention-light")).toBeNull();
  });

  it("carries no count for Imports — no endpoint answers that question", async () => {
    renderTree();

    await waitFor(() => {
      expect(screen.getByTestId("stream-ds_001")).toBeInTheDocument();
    });
    expect(screen.getByTestId("sec-data-imports")).toHaveTextContent(/^Imports$/);
  });
});
