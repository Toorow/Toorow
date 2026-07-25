/**
 * Vitest tests pour DataAccessGrantsPanel (Story 24.5, AC8).
 *
 * Coverage :
 *   - Rendu initial : spinner puis liste vide (fetch mock 200 {grants: []})
 *   - Ajout grant : mock POST 201 → ligne apparaît dans la table
 *   - Conflit 409 : Alert severity="warning" visible (AD-9)
 *   - Revoke : mock DELETE 200 → ligne disparaît (rechargement)
 *
 * AI-54 : fixtures calquées sur la réponse réelle de l'API.
 * AD-9 : 409 / 403 / 404 affichés honnêtement, jamais swallowed.
 * UX-DR10 : copie française accentuée.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import DataAccessGrantsPanel from "./DataAccessGrantsPanel";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const GRANT_1 = {
  id: "dagrant_01HXYZ",
  org_id: "org_acme01",
  principal: "serviceAccount:sa@project.iam.gserviceaccount.com",
  granted_by: "alice@example.com",
  created_at: "2026-07-01T00:00:00Z",
};

const GRANT_2 = {
  id: "dagrant_02HXYZ",
  org_id: "org_acme01",
  principal: "user:bob@example.com",
  granted_by: "alice@example.com",
  created_at: "2026-07-02T00:00:00Z",
};

function mockFetchList(grants: typeof GRANT_1[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ grants }),
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : rendu initial
// ---------------------------------------------------------------------------

describe("DataAccessGrantsPanel — rendu initial", () => {
  it("affiche le titre 'Accès données'", () => {
    mockFetchList([]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);
    expect(screen.getByText(/data access/i)).toBeInTheDocument();
  });

  it("affiche 'Aucun accès accordé' quand la liste est vide", async () => {
    mockFetchList([]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() => {
      expect(screen.getByTestId("data-access-empty")).toBeInTheDocument();
    });
  });

  it("affiche les grants dans la table quand la liste est non vide", async () => {
    mockFetchList([GRANT_1, GRANT_2]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() => {
      expect(screen.getByTestId("data-access-grants-table")).toBeInTheDocument();
      // Le principal apparaît dans la table (une occurrence dans le tbody) et dans le
      // texte descriptif du composant (dans un <code>) -- on cherche dans la table.
      expect(screen.getByTestId(`data-access-row-${GRANT_1.id}`)).toBeInTheDocument();
      expect(screen.getByTestId(`data-access-row-${GRANT_2.id}`)).toBeInTheDocument();
      expect(screen.getByText("user:bob@example.com")).toBeInTheDocument();
    });
  });

  it("affiche le formulaire d'ajout de principal", () => {
    mockFetchList([]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);
    expect(screen.getByTestId("data-access-principal-input")).toBeInTheDocument();
    expect(screen.getByTestId("data-access-grant-button")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests : ajout d'un grant (POST 201)
// ---------------------------------------------------------------------------

describe("DataAccessGrantsPanel — ajout grant", () => {
  it("envoie POST puis recharge la liste sur succès 201", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        // Chargement initial : liste vide
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [] }),
        })
      )
      .mockImplementationOnce(() =>
        // POST grant → 201
        Promise.resolve({
          ok: true,
          status: 201,
          json: async () => GRANT_1,
        })
      )
      .mockImplementationOnce(() =>
        // Rechargement après POST → liste avec GRANT_1
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [GRANT_1] }),
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      expect(screen.getByTestId("data-access-empty")).toBeInTheDocument()
    );

    const input = screen.getByLabelText("Principal IAM");
    await user.type(
      input,
      "serviceAccount:sa@project.iam.gserviceaccount.com"
    );
    await user.click(screen.getByTestId("data-access-grant-button"));

    await waitFor(() => {
      expect(screen.getByTestId("data-access-grants-table")).toBeInTheDocument();
      expect(
        screen.getByTestId(`data-access-row-${GRANT_1.id}`)
      ).toBeInTheDocument();
    });

    // Verify POST was sent with the correct body.
    const postCall = fetchMock.mock.calls.find(
      ([, opts]) => opts?.method === "POST"
    );
    expect(postCall).toBeTruthy();
    const body = JSON.parse(postCall![1].body as string);
    expect(body.principal).toBe(
      "serviceAccount:sa@project.iam.gserviceaccount.com"
    );
  });
});

// ---------------------------------------------------------------------------
// Tests : conflit 409 (AD-9)
// ---------------------------------------------------------------------------

describe("DataAccessGrantsPanel — conflit 409", () => {
  it("affiche Alert severity='warning' sur 409 (AD-9)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [] }),
        })
      )
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({
            code: "conflict",
            message: "principal already has an active grant on this org",
          }),
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      expect(screen.getByTestId("data-access-empty")).toBeInTheDocument()
    );

    const input = screen.getByLabelText("Principal IAM");
    await user.type(input, "user:dup@example.com");
    await user.click(screen.getByTestId("data-access-grant-button"));

    await waitFor(() => {
      expect(
        screen.getByTestId("data-access-op-warning")
      ).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : révocation (DELETE 200)
// ---------------------------------------------------------------------------

describe("DataAccessGrantsPanel — révocation", () => {
  it("envoie DELETE puis la ligne disparaît", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        // Chargement initial avec un grant
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [GRANT_1] }),
        })
      )
      .mockImplementationOnce(() =>
        // DELETE → 200
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ revoked: true }),
        })
      )
      .mockImplementationOnce(() =>
        // Rechargement après DELETE → liste vide
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [] }),
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      expect(screen.getByTestId("data-access-grants-table")).toBeInTheDocument()
    );

    await user.click(
      screen.getByTestId(`data-access-revoke-${GRANT_1.id}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("data-access-empty")).toBeInTheDocument();
      expect(
        screen.queryByTestId("data-access-grants-table")
      ).not.toBeInTheDocument();
    });

    const deleteCall = fetchMock.mock.calls.find(
      ([, opts]) => opts?.method === "DELETE"
    );
    expect(deleteCall).toBeTruthy();
    expect(deleteCall![0]).toContain(GRANT_1.id);
  });

  it("affiche Alert severity='error' si 403 sur DELETE (AD-9)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [GRANT_1] }),
        })
      )
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: false,
          status: 403,
          json: async () => ({ code: "forbidden", message: "Access denied" }),
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      screen.getByTestId(`data-access-revoke-${GRANT_1.id}`)
    );

    await user.click(
      screen.getByTestId(`data-access-revoke-${GRANT_1.id}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("data-access-op-error")).toBeInTheDocument();
    });
  });

  it("affiche Alert severity='info' si 404 sur DELETE (AD-9)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants: [GRANT_1] }),
        })
      )
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: false,
          status: 404,
          json: async () => ({ code: "not_found", message: "grant not found" }),
        })
      );
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      screen.getByTestId(`data-access-revoke-${GRANT_1.id}`)
    );

    await user.click(
      screen.getByTestId(`data-access-revoke-${GRANT_1.id}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("data-access-op-info")).toBeInTheDocument();
    });
  });
});
