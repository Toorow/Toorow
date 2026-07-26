/**
 * CountrySplit — the endpoint fix and the removal of invented coverage.
 *
 * The defects this pins:
 *   - the page called /api/reference/countries, which does not exist on the
 *     server (only /api/vocabularies/countries does), so country names never
 *     resolved and nothing said why;
 *   - "Active" was hard-coded, France was invented as a tracked market for
 *     projects that track none, and "6 compatible Datastreams" (twice) plus a
 *     Meta exception row were rendered as measurements with no endpoint at all.
 */
import { render, screen, waitFor } from "@testing-library/react";
import CountrySplit from "../shell/pages/CountrySplit";

function stubApi(opts: {
  project?: { ok: boolean; body?: unknown };
  countries?: { ok: boolean; body?: unknown };
}) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    const u = String(url);
    const spec = u.includes("/countries")
      ? (opts.countries ?? { ok: true, body: { countries: [] } })
      : (opts.project ?? { ok: true, body: { id: "p1", name: "P1" } });
    return Promise.resolve({
      ok: spec.ok,
      status: spec.ok ? 200 : 500,
      json: async () => spec.body ?? {},
      text: async () => "{}",
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CountrySplit — endpoint", () => {
  it("reads the country vocabulary from /api/vocabularies/countries", async () => {
    const fetchMock = stubApi({});
    render(<CountrySplit projectId="p1" />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    const urls = fetchMock.mock.calls.map((c: unknown[]) => String((c as [string])[0]));
    expect(urls).toContain("/api/vocabularies/countries");
    expect(urls.some((u) => u.includes("/api/reference/"))).toBe(false);
  });
});

describe("CountrySplit — failure and empty", () => {
  it("says the module state could not be loaded", async () => {
    stubApi({ project: { ok: false } });
    render(<CountrySplit projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load the module state/i)).toBeInTheDocument();
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/nothing below is being reported/i);
    // No module state, no tracked markets, no coverage claim.
    expect(screen.queryByRole("heading", { name: "Module" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Priority countries" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Country split is active/i)).not.toBeInTheDocument();
  });

  it("shows no tracked market when the project tracks none", async () => {
    stubApi({ project: { ok: true, body: { id: "p1", name: "P1", geographic_mode: "global" } } });
    render(<CountrySplit projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/No country is tracked for this project/i)).toBeInTheDocument();
    });
    // France was the invented default.
    expect(screen.queryByText(/France/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/1 selected/i)).not.toBeInTheDocument();
  });

  it("derives the module state from the project's reporting mode", async () => {
    stubApi({
      project: {
        ok: true,
        body: {
          id: "p1",
          name: "P1",
          geographic_mode: "local_markets",
          local_market_country_codes: ["DE"],
        },
      },
      countries: { ok: true, body: { countries: [{ code: "DE", display_name: "Germany" }] } },
    });
    render(<CountrySplit projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText("Germany")).toBeInTheDocument();
    });
    expect(screen.getByText(/1 selected/i)).toBeInTheDocument();
    expect(
      screen.getByText(/Country split is active \(reporting mode: local markets\)/i),
    ).toBeInTheDocument();
  });

  it("carries no invented coverage count and no exception row", async () => {
    stubApi({});
    render(<CountrySplit projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Coverage/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/6 compatible Datastreams/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Datastreams will update automatically/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Account insights/i)).not.toBeInTheDocument();
    // Inert controls are gone.
    expect(screen.queryByRole("button", { name: /Save changes/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add country/i })).not.toBeInTheDocument();
  });

  it("says when country names could not be resolved", async () => {
    stubApi({
      project: {
        ok: true,
        body: {
          id: "p1",
          name: "P1",
          geographic_mode: "local_markets",
          local_market_country_codes: ["DE"],
        },
      },
      countries: { ok: false },
    });
    render(<CountrySplit projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Country names could not be loaded/i)).toBeInTheDocument();
    });
    expect(screen.getByText("DE")).toBeInTheDocument();
  });
});
