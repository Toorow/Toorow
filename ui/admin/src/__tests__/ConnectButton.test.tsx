/**
 * Vitest tests for ConnectButton (Story 2.4, AC8, T4.4).
 *
 * These tests pin the honesty contract of the OAuth flow:
 *   - success is determined ONLY by Nango's postMessage on its own origin;
 *   - a popup that merely CLOSES is a cancellation: no POST /api/connections,
 *     no onSuccess, and the user is told nothing was connected;
 *   - a failed /api/modules/available says so instead of offering an invented
 *     provider list, and an empty API answer renders as empty;
 *   - a build with no VITE_NANGO_BASE_URL refuses to open a popup at all
 *     (the old code shipped a localhost:3003 default to production).
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ConnectButton from "../ConnectButton";

const NANGO_BASE = "https://nango.example.test";
const NANGO_ORIGIN = "https://nango.example.test";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

/** The two modules the stubbed /api/modules/available returns. */
const MODULES = [
  { name: "google-analytics", display_name: "Google Analytics 4" },
  { name: "meta-ads", display_name: "Meta Ads" },
];

/** fetch mock: modules list resolves with `modules`; connections POST succeeds. */
function stubFetch(modules: unknown = { modules: MODULES }) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (String(url).includes("/api/modules/available")) {
      if (modules instanceof Error) return Promise.reject(modules);
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => modules,
        text: async () => JSON.stringify(modules),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({ id: "conn_ok" }),
      text: async () => "{}",
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Open the provider menu and pick Google Analytics. */
async function openMenuAndPickGa() {
  fireEvent.click(screen.getByTestId("connect-source-button"));
  await waitFor(() => {
    expect(screen.getByTestId("connect-provider-google-analytics")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByTestId("connect-provider-google-analytics"));
}

/** A fake popup whose `closed` flag the test drives. */
let activePopup: Window;
function fakePopup() {
  const popup = { closed: false } as Window;
  activePopup = popup;
  vi.stubGlobal("open", vi.fn().mockReturnValue(popup));
  return popup;
}

/** Simulate Nango reporting from the popup that owns the current flow. */
function postNango(
  eventType: string,
  data?: Record<string, unknown>,
  source: Window = activePopup
) {
  window.dispatchEvent(
    new MessageEvent("message", {
      data: { eventType, data },
      origin: NANGO_ORIGIN,
      source,
    })
  );
}
function connectionsCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(
    (call: unknown[]) => String((call as [string])[0]) === "/api/connections"
  );
}

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.stubEnv("VITE_NANGO_BASE_URL", NANGO_BASE);
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});


// ---------------------------------------------------------------------------
// Provider list — loaded, never invented
// ---------------------------------------------------------------------------

describe("ConnectButton — provider list", () => {
  it("renders the Connect Source button", () => {
    stubFetch();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Connect Source/i })).toBeInTheDocument();
  });

  it("lists exactly the modules the API returned", async () => {
    stubFetch();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    fireEvent.click(screen.getByTestId("connect-source-button"));
    await waitFor(() => {
      expect(screen.getByTestId("connect-provider-google-analytics")).toBeInTheDocument();
    });
    expect(screen.getByTestId("connect-provider-meta-ads")).toBeInTheDocument();
    // Nothing that was never installed.
    expect(screen.queryByTestId("connect-provider-gsc")).not.toBeInTheDocument();
    expect(screen.queryByTestId("connect-provider-github")).not.toBeInTheDocument();
  });

  it("says the list failed to load instead of offering invented providers", async () => {
    stubFetch(new Error("offline"));
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    fireEvent.click(screen.getByTestId("connect-source-button"));
    await waitFor(() => {
      expect(screen.getByTestId("connect-providers-error")).toBeInTheDocument();
    });
    expect(screen.getByText(/Could not load the available sources/i)).toBeInTheDocument();
    expect(screen.queryByTestId("connect-provider-google-analytics")).not.toBeInTheDocument();
  });

  it("renders an empty API answer as empty", async () => {
    stubFetch({ modules: [] });
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    fireEvent.click(screen.getByTestId("connect-source-button"));
    await waitFor(() => {
      expect(screen.getByTestId("connect-providers-empty")).toBeInTheDocument();
    });
    expect(screen.getByText(/No connector module is installed/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// OAuth outcome — reported, never inferred
// ---------------------------------------------------------------------------

describe("ConnectButton — OAuth outcome", () => {
  it("POSTs /api/connections only after Nango reports success", async () => {
    fakePopup();
    const fetchMock = stubFetch();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    // Nothing registered before the provider answered.
    expect(connectionsCalls(fetchMock)).toHaveLength(0);

    postNango("AUTHORIZATION_SUCEEDED");

    await waitFor(() => expect(connectionsCalls(fetchMock)).toHaveLength(1), {
      timeout: 3000,
    });
    const [, init] = connectionsCalls(fetchMock)[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.provider).toBe("google-analytics");
    expect(body.project_id).toBe("p1");
    expect(body.nango_connection_id).toMatch(/^google-a-/);
  });

  it("calls onSuccess after a reported success", async () => {
    fakePopup();
    stubFetch();
    const onSuccess = vi.fn();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={onSuccess} />);
    await openMenuAndPickGa();
    postNango("AUTHORIZATION_SUCEEDED");
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1), { timeout: 3000 });
  });

  it("treats a popup that merely closes as a cancellation — no connection registered", async () => {
    const popup = fakePopup();
    const fetchMock = stubFetch();
    const onSuccess = vi.fn();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={onSuccess} />);
    await openMenuAndPickGa();

    popup.closed = true;

    await waitFor(
      () => {
        expect(screen.getByText(/Authorization was not completed/i)).toBeInTheDocument();
      },
      { timeout: 4000 }
    );
    expect(screen.getByText(/No source was connected/i)).toBeInTheDocument();
    expect(connectionsCalls(fetchMock)).toHaveLength(0);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("reports an explicit authorization failure and registers nothing", async () => {
    fakePopup();
    const fetchMock = stubFetch();
    const onSuccess = vi.fn();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={onSuccess} />);
    await openMenuAndPickGa();

    postNango("AUTHORIZATION_FAILED", { message: "access_denied" });

    await waitFor(
      () => expect(screen.getByText(/Authorization failed: access_denied/i)).toBeInTheDocument(),
      { timeout: 3000 }
    );
    expect(connectionsCalls(fetchMock)).toHaveLength(0);
    expect(onSuccess).not.toHaveBeenCalled();
  });

  it("ignores a success message posted from an unrelated origin", async () => {
    const popup = fakePopup();
    const fetchMock = stubFetch();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { eventType: "AUTHORIZATION_SUCEEDED" },
        origin: "https://evil.example",
      })
    );
    popup.closed = true;

    await waitFor(
      () => expect(screen.getByText(/Authorization was not completed/i)).toBeInTheDocument(),
      { timeout: 4000 }
    );
    expect(connectionsCalls(fetchMock)).toHaveLength(0);
  });

  it("forwards projectId to POST /api/connections (G-08)", async () => {
    fakePopup();
    const fetchMock = stubFetch();
    renderWithTheme(<ConnectButton projectId="my-project" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();
    postNango("AUTHORIZATION_SUCEEDED");

    await waitFor(() => expect(connectionsCalls(fetchMock)).toHaveLength(1), {
      timeout: 3000,
    });
    const [, init] = connectionsCalls(fetchMock)[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).project_id).toBe("my-project");
  });

  it("shows loading state while waiting for the provider", async () => {
    const popup = fakePopup();
    stubFetch();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Connecting\.\.\./i })).toBeInTheDocument();
    });
    popup.closed = true;
    await waitFor(() => {
      expect(screen.getByText(/Authorization was not completed/i)).toBeInTheDocument();
    }, { timeout: 4000 });
  });

  it("shows a MUI Snackbar (not window.alert) when the popup is blocked (G-08)", async () => {
    vi.stubGlobal("open", vi.fn().mockReturnValue(null));
    stubFetch();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    await waitFor(() => {
      expect(screen.getByText(/Popup window was blocked/i)).toBeInTheDocument();
    });
    expect(alertSpy).not.toHaveBeenCalled();
  });
  it("ignores same-origin success from another popup or another connection id", async () => {
    const popup = fakePopup();
    const fetchMock = stubFetch();
    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    postNango("AUTHORIZATION_SUCEEDED", undefined, { closed: false } as Window);
    postNango("AUTHORIZATION_SUCEEDED", { connection_id: "another-connection" }, popup);
    popup.closed = true;

    await waitFor(
      () => expect(screen.getByText(/Authorization was not completed/i)).toBeInTheDocument(),
      { timeout: 4000 }
    );
    expect(connectionsCalls(fetchMock)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Configuration — no localhost default in production
// ---------------------------------------------------------------------------

describe("ConnectButton — OAuth configuration", () => {
  it("refuses to open a popup when no OAuth service address is configured", async () => {
    vi.stubEnv("VITE_NANGO_BASE_URL", "");
    const openSpy = vi.fn();
    vi.stubGlobal("open", openSpy);
    const fetchMock = stubFetch();

    renderWithTheme(<ConnectButton projectId="p1" onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    await waitFor(() => {
      expect(screen.getByText(/no OAuth service address configured/i)).toBeInTheDocument();
    });
    expect(openSpy).not.toHaveBeenCalled();
    expect(connectionsCalls(fetchMock)).toHaveLength(0);
  });
});
describe("ConnectButton — project and reconnect invariants", () => {
  it("refuses to authorize without a real project id", async () => {
    const openSpy = vi.fn();
    vi.stubGlobal("open", openSpy);
    const fetchMock = stubFetch();
    renderWithTheme(<ConnectButton onSuccess={vi.fn()} />);

    await openMenuAndPickGa();
    await waitFor(() => {
      expect(screen.getByText(/Select a real project before connecting/i)).toBeInTheDocument();
    });
    expect(openSpy).not.toHaveBeenCalled();
    expect(connectionsCalls(fetchMock)).toHaveLength(0);
  });

  it("reconnects the existing Nango id without creating a duplicate connection_ref", async () => {
    fakePopup();
    const fetchMock = stubFetch();
    const onSuccess = vi.fn();
    renderWithTheme(
      <ConnectButton
        projectId="p1"
        fixedProvider="meta-ads"
        nangoConnectionId="existing-nango-id"
        label="Reconnect"
        onSuccess={onSuccess}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    const openedUrl = String((window.open as ReturnType<typeof vi.fn>).mock.calls[0][0]);
    expect(openedUrl).toContain("/oauth/connect/meta-ads");
    expect(openedUrl).toContain("connection_id=existing-nango-id");
    postNango("AUTHORIZATION_SUCEEDED");

    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(connectionsCalls(fetchMock)).toEqual([]);
  });
});
