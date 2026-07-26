/**
 * Provenance — the surface that proves where a number came from must itself be
 * provable.
 *
 * Until 2026-07-25 it made no network call and invented pull ids, mapping
 * versions, exchange rates with an as-of date, and client names. These tests pin
 * the three states and assert that no fabricated lineage survives.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import Provenance from "../shell/pages/Provenance";

/** The exact fabrications the old page shipped. */
const FICTION = [
  "pull_9f2ac1",
  "pull_7c10de",
  "mapping_v17",
  "mapping_v16",
  "1 USD = 0.9184 EUR",
  "Meta Ads · Acme",
  "Google Ads · Northwind",
  "GA4 · acme.com",
  "Media plan · Planning team",
];

function expectNoFiction() {
  for (const label of FICTION) {
    expect(screen.queryByText(label)).not.toBeInTheDocument();
  }
}

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
  return { ok: false, status, json: async () => ({ code, message }) } as unknown as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Provenance — error state", () => {
  it("says the load failed, offers a retry, and invents no lineage", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/api/datastreams": () => fail(503, "unavailable", "Lecture indisponible"),
      }),
    );

    render(<Provenance projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("provenance-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("provenance-error")).toHaveTextContent(
      /Couldn't load the publication log/i,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expectNoFiction();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("reports an error rather than an empty page when there is no project in scope", async () => {
    vi.stubGlobal("fetch", routeFetch({}));

    render(<Provenance />);

    await waitFor(() => {
      expect(screen.getByTestId("provenance-error")).toBeInTheDocument();
    });
    expect(screen.getByTestId("provenance-error")).toHaveTextContent(/No project in scope/i);
  });
});

describe("Provenance — empty state", () => {
  it("says nothing has been published instead of showing a table", async () => {
    vi.stubGlobal("fetch", routeFetch({ "/api/datastreams": () => ok([]) }));

    render(<Provenance projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("provenance-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("provenance-empty")).toHaveTextContent(
      /Nothing has been published in this project yet/i,
    );
    expectNoFiction();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("stays empty when datastreams exist but none has ever been published", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/publications": () => ok({ publications: [] }),
        "/api/datastreams": () => ok([{ id: "ds_1", name: "Meta paid", module_name: "meta-ads" }]),
      }),
    );

    render(<Provenance projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("provenance-empty")).toBeInTheDocument();
    });
    expectNoFiction();
  });

  it("declares that pull ids and FX are not surfaced rather than faking them", async () => {
    vi.stubGlobal("fetch", routeFetch({ "/api/datastreams": () => ok([]) }));

    render(<Provenance projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("provenance-empty")).toBeInTheDocument();
    });
    expect(screen.getByTestId("provenance-gap")).toHaveTextContent(
      /No HTTP read exposes them yet/i,
    );
  });
});

describe("Provenance — ready state", () => {
  const PUBLICATION = {
    id: "pub_1",
    execution_id: "exec_01JABCDEF",
    mapping_version_id: "mv_01JXYZ",
    content_hash: "sha256:abc123",
    row_count: 1420,
    published_at: "2026-07-22T10:05:00+00:00",
    published_by: "svc:publisher",
  };

  it("renders only the publication-log fields the API returned", async () => {
    vi.stubGlobal(
      "fetch",
      routeFetch({
        "/publications": () => ok({ publications: [PUBLICATION] }),
        "/api/datastreams": () => ok([{ id: "ds_1", name: "Meta paid", module_name: "meta-ads" }]),
      }),
    );

    render(<Provenance projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByRole("table")).toBeInTheDocument();
    });
    // The datastream name also appears in the filter <option> list, hence within().
    const table = screen.getByRole("table");
    expect(within(table).getByText("Meta paid")).toBeInTheDocument();
    expect(screen.getByText("svc:publisher")).toBeInTheDocument();
    expect(screen.getByText("22 Jul 2026")).toBeInTheDocument();
    expectNoFiction();

    // No FX column exists any more: there is no HTTP field behind it.
    expect(screen.queryByText(/FX as-of/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Pull ids/i)).not.toBeInTheDocument();
  });

  it("names how many datastreams could not be read instead of hiding the gap", async () => {
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).includes("/publications")) {
          call += 1;
          return Promise.resolve(
            call === 1
              ? ok({ publications: [PUBLICATION] })
              : fail(503, "unavailable", "Journal indisponible"),
          );
        }
        return Promise.resolve(
          ok([
            { id: "ds_1", name: "Meta paid", module_name: "meta-ads" },
            { id: "ds_2", name: "GA4 web", module_name: "google_analytics" },
          ]),
        );
      }),
    );

    render(<Provenance projectId="proj_test" />);

    await waitFor(() => {
      expect(screen.getByTestId("provenance-partial")).toBeInTheDocument();
    });
    expect(screen.getByTestId("provenance-partial")).toHaveTextContent(
      /1 datastream could not be read/i,
    );
  });
});
