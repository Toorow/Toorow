/**
 * ProjectSettings — the market picker must talk to an endpoint that exists.
 *
 * The defect this pins: the page called /api/reference/countries. The server
 * exposes only /api/vocabularies/countries (server/core/admin_api.py); there is
 * no /api/reference/* route at all. Every call 404'd, the picker was
 * structurally empty, and the empty picker read as "there are no countries".
 */
import { render, screen, waitFor } from "@testing-library/react";
import ProjectSettings from "../shell/pages/ProjectSettings";

const LOCAL_MARKETS_PROJECT = {
  id: "p1",
  name: "P1",
  slug: "p1",
  geographic_mode: "local_markets",
  local_market_country_codes: ["DE"],
};

function stubApi(opts: {
  project?: { ok: boolean; body?: unknown };
  countries?: { ok: boolean; body?: unknown };
}) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    const u = String(url);
    const spec = u.includes("countries")
      ? (opts.countries ?? { ok: true, body: { countries: [] } })
      : (opts.project ?? { ok: true, body: LOCAL_MARKETS_PROJECT });
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

describe("ProjectSettings — country vocabulary endpoint", () => {
  it("calls /api/vocabularies/countries and never /api/reference/countries", async () => {
    const fetchMock = stubApi({
      countries: { ok: true, body: { countries: [{ code: "DE", display_name: "Germany" }] } },
    });
    render(<ProjectSettings projectId="p1" />);

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((c: unknown[]) => String((c as [string])[0]));
      expect(urls).toContain("/api/vocabularies/countries");
    });
    const urls = fetchMock.mock.calls.map((c: unknown[]) => String((c as [string])[0]));
    expect(urls.some((u) => u.includes("/api/reference/"))).toBe(false);
  });
});

describe("ProjectSettings — failure and empty", () => {
  it("says the project settings could not be loaded", async () => {
    stubApi({ project: { ok: false } });
    render(<ProjectSettings projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load project settings/i)).toBeInTheDocument();
    });
    expect(screen.queryByLabelText(/Display name/i)).not.toBeInTheDocument();
  });

  it("says the country list failed to load instead of showing an empty picker", async () => {
    stubApi({ countries: { ok: false } });
    render(<ProjectSettings projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByText(/Could not load the country list/i)).toBeInTheDocument();
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/not an empty reference list/i);
    expect(screen.getByPlaceholderText(/Country list unavailable/i)).toBeDisabled();
  });

  it("renders an empty vocabulary as empty", async () => {
    stubApi({ countries: { ok: true, body: { countries: [] } } });
    render(<ProjectSettings projectId="p1" />);

    await waitFor(() => {
      expect(
        screen.getByText(/The reference country list came back empty/i),
      ).toBeInTheDocument();
    });
    expect(screen.getByPlaceholderText(/No country available/i)).toBeDisabled();
  });

  it("offers the countries the vocabulary returned", async () => {
    stubApi({
      countries: {
        ok: true,
        body: {
          countries: [
            { code: "DE", display_name: "Germany" },
            { code: "FR", display_name: "France" },
          ],
        },
      },
    });
    render(<ProjectSettings projectId="p1" />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText(/Add a country/i)).toBeInTheDocument();
    });
    // DE is already selected; it renders as a chip resolved to its display name.
    expect(screen.getByText(/Germany \(DE\)/)).toBeInTheDocument();
  });
});
