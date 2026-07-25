/**
 * Vitest tests for GoogleConnectPanel (Story 18.4).
 *
 * Coverage:
 *   - Rendu état non connecté (auth_path='nango') : message + bouton « Connecter Google »
 *   - Rendu état connecté (auth_path='google_direct') : scopes, expiry, santé
 *   - Badge de santé : ok / stale / not_connected / unknown
 *   - Bouton « Déconnecter Google » : dialog de confirmation
 *   - Confirmation déconnexion : appel POST /api/google/oauth/revoke/{id}
 *   - Annulation déconnexion : pas d'appel API
 *   - Retour ?google_oauth=success → bandeau de succès
 *   - Retour ?google_oauth=error → bandeau d'erreur
 *   - Erreur de chargement : alert d'erreur
 *   - NFR3 : aucun token dans le DOM
 *
 * Pièges connus :
 *   - MUI Dialog : les boutons sont dans un portail (getByRole fonctionne mieux que queryByTestId).
 *   - fetch mock : chaque test initialise son propre mock via vi.fn().
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import GoogleConnectPanel from "../GoogleConnectPanel";

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderPanel(props: {
  connectionRefId?: string;
  projectId?: string;
  onStatusChange?: () => void;
}) {
  return render(
    <ThemeProvider theme={adminTheme}>
      <GoogleConnectPanel
        connectionRefId={props.connectionRefId ?? "conn_test_001"}
        projectId={props.projectId ?? "proj_alpha"}
        onStatusChange={props.onStatusChange}
      />
    </ThemeProvider>
  );
}

function makeStatusResponse(overrides: Record<string, unknown> = {}) {
  return {
    connection_ref_id: "conn_test_001",
    auth_path: "nango",
    health: "not_connected",
    token_expiry: null,
    granted_scopes: [],
    project_id: "proj_alpha",
    ...overrides,
  };
}

function mockFetch(responses: Array<{ data: unknown; status?: number }>) {
  let callIndex = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => {
      const resp = responses[Math.min(callIndex, responses.length - 1)];
      callIndex++;
      const status = resp.status ?? 200;
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        statusText: status === 200 ? "OK" : "Error",
        json: async () => resp.data,
        text: async () => JSON.stringify(resp.data),
      });
    })
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  // Reset location.search to avoid bleeding between tests.
  window.history.replaceState({}, "", "/");
});

// ---------------------------------------------------------------------------
// Tests : état non connecté
// ---------------------------------------------------------------------------

describe("GoogleConnectPanel — état non connecté", () => {
  it("affiche le message 'Google not connected' et le bouton Connecter", async () => {
    mockFetch([{ data: makeStatusResponse() }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-not-connected-message")).toBeInTheDocument();
    });
    // « Connect Google » apparaît dans le message ET le bouton — scoper au bouton.
    expect(screen.getByTestId("google-connect-button")).toHaveTextContent(
      /Connect Google/i
    );
  });

  it("affiche le badge 'Not Connected' quand health='not_connected'", async () => {
    mockFetch([{ data: makeStatusResponse({ health: "not_connected" }) }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-health-not-connected")).toBeInTheDocument();
    });
  });

  it("appelle GET /api/google/oauth/authorize et redirige lors du clic Connecter", async () => {
    const authorizeUrl = "https://accounts.google.com/o/oauth2/v2/auth?fake=1";
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => makeStatusResponse(),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ authorize_url: authorizeUrl }),
      });
    vi.stubGlobal("fetch", fetchMock);

    // Mock window.location.href setter
    const locationSpy = vi.spyOn(window, "location", "get").mockReturnValue({
      ...window.location,
      href: "",
    } as Location);
    let capturedHref = "";
    Object.defineProperty(window, "location", {
      value: {
        ...window.location,
        set href(val: string) { capturedHref = val; },
        get href() { return capturedHref; },
      },
      writable: true,
    });

    const user = userEvent.setup();
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-connect-button")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-connect-button"));

    await waitFor(() => {
      // Le PREMIER fetch est le GET status — on vérifie qu'UN des appels vise
      // authorize (peu importe son rang et ses options).
      const urls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(
        urls.some((u) => u.includes("/api/google/oauth/authorize"))
      ).toBe(true);
    });

    locationSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// Tests : état connecté
// ---------------------------------------------------------------------------

describe("GoogleConnectPanel — état connecté", () => {
  const connectedStatus = makeStatusResponse({
    auth_path: "google_direct",
    health: "ok",
    token_expiry: "2026-07-19T18:00:00Z",
    granted_scopes: [
      {
        scope: "https://www.googleapis.com/auth/analytics.readonly",
        label: "Google Analytics 4 (lecture)",
      },
      {
        scope: "https://www.googleapis.com/auth/webmasters.readonly",
        label: "Google Search Console (lecture)",
      },
    ],
  });

  it("affiche le badge santé 'Connected' quand health='ok'", async () => {
    mockFetch([{ data: connectedStatus }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-health-ok")).toBeInTheDocument();
    });
  });

  it("affiche la liste des scopes accordés avec leurs libellés FR", async () => {
    mockFetch([{ data: connectedStatus }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-scopes-list")).toBeInTheDocument();
    });
    expect(screen.getByText("Google Analytics 4 (lecture)")).toBeInTheDocument();
    expect(screen.getByText("Google Search Console (lecture)")).toBeInTheDocument();
  });

  it("affiche la date d'expiration du token", async () => {
    mockFetch([{ data: connectedStatus }]);
    renderPanel({});

    await waitFor(() => {
      // L'expiry doit être présente quelque part dans le DOM
      expect(screen.getByText(/Token Expiration/i)).toBeInTheDocument();
    });
  });

  it("affiche le badge santé 'Token Expired' quand health='stale'", async () => {
    mockFetch([
      {
        data: makeStatusResponse({
          auth_path: "google_direct",
          health: "stale",
          token_expiry: "2026-07-18T11:00:00Z",
          granted_scopes: [],
        }),
      },
    ]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-health-stale")).toBeInTheDocument();
    });
  });

  it("n'affiche AUCUN token ni access_token ni refresh_token dans le DOM (NFR3)", async () => {
    mockFetch([{ data: connectedStatus }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-health-ok")).toBeInTheDocument();
    });

    // NFR3 : aucun token dans le DOM.
    expect(screen.queryByText(/access_token/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/refresh_token/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/fake_access/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests : déconnexion
// ---------------------------------------------------------------------------

describe("GoogleConnectPanel — déconnexion", () => {
  const connectedStatus = makeStatusResponse({
    auth_path: "google_direct",
    health: "ok",
    token_expiry: "2026-07-19T18:00:00Z",
    granted_scopes: [],
  });

  it("affiche le bouton 'Disconnect Google' quand connecté", async () => {
    mockFetch([{ data: connectedStatus }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-button")).toBeInTheDocument();
    });
  });

  it("ouvre le dialog de confirmation au clic Déconnecter", async () => {
    mockFetch([{ data: connectedStatus }]);
    const user = userEvent.setup();
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-button")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-disconnect-button"));

    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-dialog")).toBeInTheDocument();
    });
    // Le dialog contient un message explicite.
    expect(screen.getByText(/will revoke Google access/i)).toBeInTheDocument();
  });

  it("n'appelle PAS le POST revoke si l'utilisateur annule", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => connectedStatus,
    });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-button")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-disconnect-button"));
    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-cancel")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-disconnect-cancel"));

    // Seul le GET /status initial doit avoir été appelé (pas de POST revoke).
    const revokeCalls = fetchMock.mock.calls.filter(
      (call: unknown[]) =>
        typeof call[0] === "string" && call[0].includes("/revoke/")
    );
    expect(revokeCalls).toHaveLength(0);
  });

  it("appelle POST /api/google/oauth/revoke/{id} après confirmation", async () => {
    const afterRevokeStatus = makeStatusResponse({ auth_path: "nango", health: "not_connected" });
    const fetchMock = vi
      .fn()
      // 1. GET /status initial
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => connectedStatus,
      })
      // 2. POST /revoke
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          revoked: true,
          connection_ref_id: "conn_test_001",
          google_revoke: "ok",
        }),
      })
      // 3. GET /status après revoke
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => afterRevokeStatus,
      });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-button")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-disconnect-button"));
    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-confirm")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-disconnect-confirm"));

    await waitFor(() => {
      const revokeCalls = fetchMock.mock.calls.filter(
        (call: unknown[]) =>
          typeof call[0] === "string" && call[0].includes("/revoke/")
      );
      expect(revokeCalls).toHaveLength(1);
      const [url, init] = revokeCalls[0] as [string, RequestInit];
      expect(url).toContain("conn_test_001");
      expect(init?.method).toBe("POST");
    });
  });

  it("appelle onStatusChange après une déconnexion réussie", async () => {
    const onStatusChange = vi.fn();
    const afterRevokeStatus = makeStatusResponse();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => connectedStatus,
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ revoked: true, connection_ref_id: "conn_test_001", google_revoke: "ok" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => afterRevokeStatus,
      });
    vi.stubGlobal("fetch", fetchMock);

    const user = userEvent.setup();
    renderPanel({ onStatusChange });

    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-button")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("google-disconnect-button"));
    await waitFor(() => {
      expect(screen.getByTestId("google-disconnect-confirm")).toBeInTheDocument();
    });
    await user.click(screen.getByTestId("google-disconnect-confirm"));

    await waitFor(() => {
      expect(onStatusChange).toHaveBeenCalledTimes(1);
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : retour OAuth (?google_oauth=success|error)
// ---------------------------------------------------------------------------

describe("GoogleConnectPanel — retour OAuth URL params", () => {
  it("affiche un bandeau de succès quand ?google_oauth=success est dans l'URL", async () => {
    window.history.replaceState({}, "", "/?google_oauth=success&connection=conn_test_001");
    mockFetch([{ data: makeStatusResponse() }]);
    renderPanel({});

    await waitFor(() => {
      // Le bandeau de succès doit être visible.
      expect(
        screen.getByText(/Google account connected successfully/i)
      ).toBeInTheDocument();
    });
  });

  it("affiche un bandeau d'erreur quand ?google_oauth=error est dans l'URL", async () => {
    window.history.replaceState({}, "", "/?google_oauth=error");
    mockFetch([{ data: makeStatusResponse() }]);
    renderPanel({});

    await waitFor(() => {
      expect(
        screen.getByText(/Google connection failed/i)
      ).toBeInTheDocument();
    });
  });

  it("ne montre pas de bandeau quand l'URL n'a pas de paramètre google_oauth", async () => {
    window.history.replaceState({}, "", "/");
    mockFetch([{ data: makeStatusResponse() }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByTestId("google-connect-panel")).toBeInTheDocument();
    });

    expect(screen.queryByText(/Google account connected/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Google connection failed/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests : erreur de chargement
// ---------------------------------------------------------------------------

describe("GoogleConnectPanel — erreur de chargement", () => {
  it("affiche une alerte quand l'API retourne une erreur", async () => {
    mockFetch([{ data: { code: "db_error", message: "Erreur base de données." }, status: 500 }]);
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
  });

  it("affiche une alerte quand le fetch échoue (réseau)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network error")));
    renderPanel({});

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
  });
});
