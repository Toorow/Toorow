/**
 * Vitest tests for ConnectButton (Story 2.4, AC8, T4.4 — Epic 8 polish: provider menu).
 *
 * Tests:
 *   - "Connecter une source" button is present and opens the provider menu
 *   - POST /api/connections called with correct payload after picking a provider
 *   - onSuccess callback triggered after successful connection
 *   - projectId prop forwarded (G-08); Snackbar instead of window.alert (G-08)
 *
 * Mocking strategy:
 *   - window.open: fake popup whose .closed toggles to true, simulating OAuth done.
 *   - fetch: /api/modules/available rejects (fallback provider list used);
 *     POST /api/connections returns 200.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ConnectButton from "../ConnectButton";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

/** Click the main button, then pick Google Analytics in the provider menu. */
async function openMenuAndPickGa() {
  fireEvent.click(screen.getByTestId("connect-source-button"));
  await waitFor(() => {
    expect(screen.getByTestId("connect-provider-google-analytics")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByTestId("connect-provider-google-analytics"));
}

/** fetch mock: modules list fails (fallback used); connections POST succeeds. */
function stubFetchOk() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes("/api/modules/available")) {
      return Promise.reject(new Error("offline"));
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

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Button + menu presence
// ---------------------------------------------------------------------------

describe("ConnectButton — button and provider menu", () => {
  it("renders the Connect Source button", () => {
    stubFetchOk();
    renderWithTheme(<ConnectButton onSuccess={vi.fn()} />);
    expect(
      screen.getByRole("button", { name: /Connect Source/i })
    ).toBeInTheDocument();
  });

  it("opens a provider menu listing the four fallback modules", async () => {
    stubFetchOk();
    renderWithTheme(<ConnectButton onSuccess={vi.fn()} />);
    fireEvent.click(screen.getByTestId("connect-source-button"));
    await waitFor(() => {
      expect(screen.getByTestId("connect-provider-google-analytics")).toBeInTheDocument();
      expect(screen.getByTestId("connect-provider-meta-ads")).toBeInTheDocument();
      expect(screen.getByTestId("connect-provider-gsc")).toBeInTheDocument();
      expect(screen.getByTestId("connect-provider-github")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// OAuth flow + POST /api/connections
// ---------------------------------------------------------------------------

describe("ConnectButton — OAuth flow", () => {
  it("calls POST /api/connections with correct payload on popup close", async () => {
    let closed = false;
    const fakePopup = {
      get closed() { return closed; },
    };
    vi.stubGlobal("open", vi.fn().mockReturnValue(fakePopup));
    setTimeout(() => { closed = true; }, 100);

    const fetchMock = stubFetchOk();
    renderWithTheme(<ConnectButton onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    await waitFor(
      () => {
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/connections",
          expect.objectContaining({
            method: "POST",
            headers: expect.objectContaining({ "Content-Type": "application/json" }),
          })
        );
      },
      { timeout: 3000 }
    );

    const callArgs = fetchMock.mock.calls.find(
      (call: unknown[]) => (call as [string])[0] === "/api/connections"
    );
    expect(callArgs).toBeDefined();
    const body = JSON.parse((callArgs as [string, RequestInit])[1].body as string);
    expect(body.provider).toBe("google-analytics");
    expect(body.project_id).toBe("default");
    expect(typeof body.nango_connection_id).toBe("string");
    // Connection ids are prefixed with the provider slug (8 chars).
    expect(body.nango_connection_id).toMatch(/^google-a-/);
  });

  it("calls onSuccess after successful POST /api/connections", async () => {
    let closed = false;
    const fakePopup = { get closed() { return closed; } };
    vi.stubGlobal("open", vi.fn().mockReturnValue(fakePopup));
    setTimeout(() => { closed = true; }, 50);
    stubFetchOk();

    const onSuccess = vi.fn();
    renderWithTheme(<ConnectButton onSuccess={onSuccess} />);
    await openMenuAndPickGa();

    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1), { timeout: 3000 });
  });

  it("shows loading state while connecting", async () => {
    const fakePopup = { closed: false };
    vi.stubGlobal("open", vi.fn().mockReturnValue(fakePopup));
    stubFetchOk();

    renderWithTheme(<ConnectButton onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Connecting\.\.\./i })
      ).toBeInTheDocument();
    });
  });

  it("passes projectId prop to POST /api/connections body (G-08)", async () => {
    let closed = false;
    const fakePopup = { get closed() { return closed; } };
    vi.stubGlobal("open", vi.fn().mockReturnValue(fakePopup));
    setTimeout(() => { closed = true; }, 100);

    const fetchMock = stubFetchOk();
    const onSuccess = vi.fn();
    renderWithTheme(<ConnectButton onSuccess={onSuccess} projectId="my-project" />);
    await openMenuAndPickGa();

    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1), { timeout: 3000 });

    const callArgs = fetchMock.mock.calls.find(
      (call: unknown[]) => (call as [string])[0] === "/api/connections"
    );
    expect(callArgs).toBeDefined();
    const body = JSON.parse((callArgs as [string, RequestInit])[1].body as string);
    expect(body.project_id).toBe("my-project");
  });

  it("shows MUI Snackbar (not window.alert) when popup is blocked (G-08)", async () => {
    vi.stubGlobal("open", vi.fn().mockReturnValue(null));
    stubFetchOk();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});

    renderWithTheme(<ConnectButton onSuccess={vi.fn()} />);
    await openMenuAndPickGa();

    await waitFor(() => {
      expect(alertSpy).not.toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(
        screen.getByText(/Popup window was blocked/i)
      ).toBeInTheDocument();
    });
  });
});
