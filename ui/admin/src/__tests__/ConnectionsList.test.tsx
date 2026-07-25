/**
 * Vitest tests for ConnectionsList (Story 2.4, AC8, T3.6;
 *                                   Story 2.5, AC3, AC4, AC10, T6.6).
 *
 * Tests:
 *   - Empty state renders correctly
 *   - Connections list renders from mocked API response
 *   - Error state renders correctly
 *   - "Connect Source" button is present
 *   - Story 2.5: traffic-light health chip (ok/stale/revoked/unknown)
 *   - Story 2.5: last-pull date display + "Never" for null
 *   - Story 2.5: "Refresh" button triggers POST refresh-health
 *
 * Copy is English (2026-07-24 Englishization).
 *
 * No Nango SDK calls are made in unit tests (all mocked via fetch mock).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ConnectionsList from "../ConnectionsList";

// Wrap with ThemeProvider to avoid MUI warnings
function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockFetch(data: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => data,
    text: async () => JSON.stringify(data),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Test data factories (Story 2.5)
// ---------------------------------------------------------------------------

function makeConnection(overrides: Record<string, unknown> = {}) {
  return {
    id: "conn_01",
    nango_connection_id: "nango-abc",
    provider: "google-analytics",
    project_id: "default",
    created_at: "2026-07-11T10:00:00Z",
    health: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

describe("ConnectionsList — empty state", () => {
  it("renders empty state message when no connections", async () => {
    mockFetch({ connections: [] });
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(
        screen.getByText(/No connections configured/i)
      ).toBeInTheDocument();
    });
  });

  it("shows the ConnectButton even when list is empty", async () => {
    mockFetch({ connections: [] });
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Connect Source/i })
      ).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// List render
// ---------------------------------------------------------------------------

describe("ConnectionsList — list render", () => {
  it("renders connection rows from mocked API response", async () => {
    mockFetch({
      connections: [
        makeConnection({ health: { status: "ok", last_checked_at: null, last_fetched_at: null } }),
      ],
    });

    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByText("google-analytics")).toBeInTheDocument();
    });
    expect(screen.getByText("nango-abc")).toBeInTheDocument();
    expect(screen.getByText("default")).toBeInTheDocument();
  });

  it("renders the Authorizations heading", async () => {
    mockFetch({ connections: [] });
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /Authorizations/i })).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Story 2.5 — traffic-light health chip (AC3, AC10)
// ---------------------------------------------------------------------------

describe("ConnectionsList — health chip (Story 2.5 AC3)", () => {
  it("shows 'Active' chip for ok status", async () => {
    mockFetch({
      connections: [
        makeConnection({
          health: { status: "ok", last_checked_at: null, last_fetched_at: "2026-07-10T12:00:00Z" },
        }),
      ],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      expect(screen.getByText("Active")).toBeInTheDocument();
    });
  });

  it("shows 'Late' chip for stale status", async () => {
    mockFetch({
      connections: [
        makeConnection({
          id: "conn_02",
          health: { status: "stale", last_checked_at: null, last_fetched_at: "2026-07-09T08:00:00Z" },
        }),
      ],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      expect(screen.getByText("Late")).toBeInTheDocument();
    });
  });

  it("shows 'Token Expired' chip for revoked status", async () => {
    mockFetch({
      connections: [
        makeConnection({
          id: "conn_03",
          health: { status: "revoked", last_checked_at: null, last_fetched_at: null },
        }),
      ],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      expect(screen.getByText("Token Expired")).toBeInTheDocument();
    });
  });

  it("shows 'Unknown' chip when health is null", async () => {
    mockFetch({
      connections: [makeConnection({ health: null })],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      expect(screen.getByText("Unknown")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Story 2.5 — last-pull date display (AC3, AC10)
// ---------------------------------------------------------------------------

describe("ConnectionsList — last-pull display (Story 2.5 AC3)", () => {
  it("shows 'Never' when last_fetched_at is null", async () => {
    mockFetch({
      connections: [
        makeConnection({
          health: { status: "ok", last_checked_at: null, last_fetched_at: null },
        }),
      ],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      expect(screen.getByText("Never")).toBeInTheDocument();
    });
  });

  it("shows formatted date when last_fetched_at is set", async () => {
    mockFetch({
      connections: [
        makeConnection({
          health: {
            status: "ok",
            last_checked_at: "2026-07-11T10:00:00Z",
            last_fetched_at: "2026-07-10T12:00:00Z",
          },
        }),
      ],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      // Exact format depends on locale, but should NOT be "Never"
      expect(screen.queryByText("Never")).not.toBeInTheDocument();
      // The date cell should contain some text (formatted date). en-US short
      // renders the last-pull date as e.g. "7/10/26" and Created At as a
      // localized string — both include the year "2026" or "26".
      expect(screen.getAllByText(/2026|\/26/).length).toBeGreaterThan(0);
    });
  });
});

// ---------------------------------------------------------------------------
// Story 2.5 — "Actualiser" refresh button (AC4, AC10)
// ---------------------------------------------------------------------------

describe("ConnectionsList — refresh button (Story 2.5 AC4)", () => {
  it("'Refresh' button is visible in each connection row", async () => {
    mockFetch({
      connections: [
        makeConnection({ health: { status: "ok", last_checked_at: null, last_fetched_at: null } }),
      ],
    });
    renderWithTheme(<ConnectionsList />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Refresh/i })).toBeInTheDocument();
    });
  });

  it("clicking 'Refresh' calls POST /api/connections/<id>/refresh-health", async () => {
    const user = userEvent.setup();

    // Initial GET
    const initialFetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({
        connections: [
          makeConnection({ health: { status: "ok", last_checked_at: null, last_fetched_at: null } }),
        ],
      }),
    });

    // POST refresh-health
    const refreshHealth = { status: "stale", last_checked_at: "2026-07-11T10:05:00Z", last_fetched_at: null };
    initialFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "conn_01", health: refreshHealth }),
    });

    vi.stubGlobal("fetch", initialFetch);

    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Refresh/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /Refresh/i }));

    await waitFor(() => {
      // GET connections + ConnectButton's GET modules/available + POST refresh.
      expect(initialFetch).toHaveBeenCalledTimes(3);
    });

    // Verify the POST call was made to the correct URL
    const calls = initialFetch.mock.calls;
    const postCall = calls.find(
      (c: unknown[]) => typeof c[0] === "string" && c[0].includes("refresh-health")
    );
    expect(postCall).toBeDefined();
    expect(postCall![0]).toBe("/api/connections/conn_01/refresh-health");
    expect(postCall![1]?.method).toBe("POST");
  });

  it("updates the health chip after successful refresh", async () => {
    const user = userEvent.setup();

    // URL-aware mock (sequential Once mocks would be consumed by the
    // ConnectButton's /api/modules/available fetch at mount).
    const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes("refresh-health") && init?.method === "POST") {
        return Promise.resolve({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => ({
            id: "conn_01",
            health: {
              status: "revoked",
              last_checked_at: "2026-07-11T10:05:00Z",
              last_fetched_at: null,
            },
          }),
        });
      }
      if (url.includes("/api/connections")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          statusText: "OK",
          json: async () => ({
            connections: [
              makeConnection({
                health: { status: "ok", last_checked_at: null, last_fetched_at: null },
              }),
            ],
          }),
        });
      }
      return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: async () => ({}) });
    });

    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByText("Active")).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /Refresh/i }));

    await waitFor(() => {
      expect(screen.getByText("Token Expired")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Story 18.4 review-18-5 fix-5 : GoogleConnectPanel absent sans connexion google_direct
// ---------------------------------------------------------------------------

describe("ConnectionsList — GoogleConnectPanel absent sans connexion google_direct (review-18-5)", () => {
  it("ne rend PAS le GoogleConnectPanel quand la liste est vide (projet vierge)", async () => {
    mockFetch({ connections: [] });
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByText(/No connections configured/i)).toBeInTheDocument();
    });
    // Le panneau Google direct ne doit PAS apparaître si aucune connexion google_direct
    // n'existe (projet vierge -- le flux Nango est la porte d'entrée initiale).
    expect(screen.queryByTestId("google-connect-panel")).not.toBeInTheDocument();
  });

  it("ne rend PAS le GoogleConnectPanel quand seules des connexions nango existent", async () => {
    mockFetch({
      connections: [
        makeConnection({ provider: "google-analytics", auth_path: "nango" }),
        makeConnection({ id: "conn_02", provider: "gsc", auth_path: "nango" }),
      ],
    });
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByText("google-analytics")).toBeInTheDocument();
    });
    // Aucune connexion google_direct => panneau absent.
    expect(screen.queryByTestId("google-connect-panel")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------

describe("ConnectionsList — error state", () => {
  it("renders error alert on fetch failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network error")));
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
    expect(screen.getByText(/Error loading connections/i)).toBeInTheDocument();
  });

  it("renders error alert on non-ok HTTP response", async () => {
    mockFetch({ detail: "Internal Server Error" }, 500);
    renderWithTheme(<ConnectionsList />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
  });
});
