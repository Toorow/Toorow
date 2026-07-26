/**
 * DataQualityPage — a failed load must never read as a clean bill of health.
 *
 * The defect this pins: `catch { setIssues([]) }` handed IssuesTable an empty
 * array, which it renders as "No issues detected — all of your monitors are
 * green". A /api/dq/issues outage looked exactly like perfect data quality.
 * Same for /api/dq/datastream-freshness.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import DataQualityPage from "../DataQualityPage";

const MONITORS = [
  {
    type: "dq_volume",
    label: "Volume",
    healthy_pct: 100,
    unresolved_count: 0,
    new_since_yesterday: 0,
  },
];

interface Routes {
  issues?: { ok: boolean; body?: unknown };
  freshness?: { ok: boolean; body?: unknown };
}

function stubApi(routes: Routes) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    const u = String(url);
    const reply = (ok: boolean, body: unknown, status = ok ? 200 : 503) =>
      Promise.resolve({ ok, status, json: async () => body, text: async () => "{}" });

    if (u.includes("/api/dq/summary")) {
      return reply(true, { monitors: MONITORS, total_issues_24h: 0, total_unresolved: 0 });
    }
    if (u.includes("/api/dq/issues")) {
      const r = routes.issues ?? { ok: true, body: { issues: [] } };
      return reply(r.ok, r.body ?? {});
    }
    if (u.includes("/api/dq/datastream-freshness")) {
      const r = routes.freshness ?? { ok: true, body: { datastreams: [] } };
      return reply(r.ok, r.body ?? {});
    }
    // Cache health card and anything else.
    return reply(true, {});
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPage() {
  return render(
    <ThemeProvider theme={adminTheme}>
      <DataQualityPage projectId="p1" />
    </ThemeProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DataQualityPage — failure is said, never shown as green", () => {
  it("reports a failed issues load and never claims 'No issues detected'", async () => {
    stubApi({ issues: { ok: false } });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Could not load the quality issues/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/No issues detected/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/All of your monitors are green/i)).not.toBeInTheDocument();
  });

  it("reports a failed freshness load instead of an empty datastream table", async () => {
    stubApi({ freshness: { ok: false } });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/Could not load datastream freshness/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/No datastreams registered yet/i)).not.toBeInTheDocument();
  });

  it("renders an empty API answer as an honest empty state", async () => {
    stubApi({ issues: { ok: true, body: { issues: [] } } });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/No issues detected/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/No datastreams registered yet/i)).toBeInTheDocument();
    // …and no DQ load-failure banner (the cache card owns its own messaging).
    expect(screen.queryByText(/Could not load the quality issues/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not load datastream freshness/i)).not.toBeInTheDocument();
  });
});

describe("DataQualityPage — configuration is not asserted", () => {
  it("does not claim a fixed number of monitors, nor project-level thresholds", async () => {
    stubApi({});
    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/1 universal monitor reporting/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/Five universal monitors/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no configuration required/i)).not.toBeInTheDocument();
    // The definition strip is derived from the monitors that actually reported.
    expect(
      screen.getByText(/platform definitions applied to every project/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Rejected rows/i)).not.toBeInTheDocument();
  });
});
