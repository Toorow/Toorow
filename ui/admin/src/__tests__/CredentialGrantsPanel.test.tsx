/**
 * Vitest tests pour CredentialGrantsPanel (Story 21.8, AC5).
 *
 * Coverage :
 *   - Chargement des comptes (GET /api/credentials/{id}/accounts)
 *   - Chargement des grants (GET /api/credentials/{id}/grants)
 *   - Exposer un compte (POST grants) → succès
 *   - 409 already granted → Alert severity="info" honnête (AD-9)
 *   - 403 → Alert severity="error" honnête (AD-9)
 *   - Révoquer un grant (DELETE)
 *
 * AI-54 : fixtures calquées sur la réponse réelle de l'API.
 * AD-9 : 409 / 403 affichés honnêtement, jamais swallowed.
 * UX-DR10 : copie française accentuée.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import CredentialGrantsPanel from "../orgs/CredentialGrantsPanel";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54 : réponse réelle des endpoints)
// GET /api/credentials/{cred}/accounts → {"accounts": [{...}]}
// GET /api/credentials/{cred}/grants   → {"grants": [{...}]}
// ---------------------------------------------------------------------------

const ACCOUNT_GA4 = {
  credential_id: "cred_ga4_001",
  external_account_id: "ga4-account-123",
  label: "Compte GA4 principal",
  discovered_at: "2026-01-01T00:00:00Z",
};

const ACCOUNT_GSC = {
  credential_id: "cred_ga4_001",
  external_account_id: "gsc-site-456",
  label: null,
  discovered_at: null,
};

const GRANT_EXISTING = {
  id: "grant_001",
  credential_id: "cred_ga4_001",
  external_account_id: "ga4-account-123",
  grantee_org_id: "org_acme01",
  granted_by: "alice@example.com",
  created_at: "2026-01-15T00:00:00Z",
};

function mockFetchAccountsAndGrants(
  accounts = [ACCOUNT_GA4, ACCOUNT_GSC],
  grants = [GRANT_EXISTING]
) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url.includes("/grants")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ grants }),
        });
      }
      if (url.includes("/accounts")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ accounts }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({}),
      });
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : chargement (AC5)
// ---------------------------------------------------------------------------

describe("CredentialGrantsPanel — chargement", () => {
  it("affiche le titre 'Autorisations d'auth'", () => {
    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );
    expect(
      screen.getByText(/auth grants/i)
    ).toBeInTheDocument();
  });

  it("charge et affiche les comptes dans un tableau", async () => {
    mockFetchAccountsAndGrants();
    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() => {
      expect(screen.getByTestId("accounts-table")).toBeInTheDocument();
      expect(screen.getByText("ga4-account-123")).toBeInTheDocument();
      expect(screen.getByText("gsc-site-456")).toBeInTheDocument();
    });
  });

  it("affiche le libellé des comptes (ou — si absent)", async () => {
    mockFetchAccountsAndGrants();
    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() => {
      expect(screen.getByText("Compte GA4 principal")).toBeInTheDocument();
      // Label null → "—" (le compte GSC rend "—" pour le libellé ET la date)
      expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
    });
  });

  it("affiche un Alert si le GET accounts retourne 403 (AD-9)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: async () => ({
          code: "forbidden",
          message: "Forbidden",
        }),
      })
    );
    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() => {
      expect(screen.getByTestId("grants-load-error")).toBeInTheDocument();
      expect(screen.getByText(/insufficient permissions/i)).toBeInTheDocument();
    });
  });

  it("affiche 'Aucun compte trouvé' quand accounts est vide", async () => {
    mockFetchAccountsAndGrants([], []);
    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() => {
      expect(screen.getByText(/no accounts found/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : grants existants — bouton Révoquer (AC5)
// ---------------------------------------------------------------------------

describe("CredentialGrantsPanel — grants existants", () => {
  it("affiche le bouton 'Révoquer' si un grant existe pour l'org", async () => {
    mockFetchAccountsAndGrants([ACCOUNT_GA4, ACCOUNT_GSC], [GRANT_EXISTING]);
    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() => {
      // ACCOUNT_GA4 a un grant pour org_acme01 → bouton Révoquer
      expect(
        screen.getByTestId(`revoke-grant-${ACCOUNT_GA4.external_account_id}`)
      ).toBeInTheDocument();
      // ACCOUNT_GSC n'a pas de grant → bouton Exposer
      expect(
        screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
      ).toBeInTheDocument();
    });
  });

  it("envoie DELETE sur clic 'Révoquer'", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "DELETE") {
          return Promise.resolve({
            ok: true,
            status: 204,
            json: async () => ({}),
          });
        }
        if (url.includes("/grants")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ grants: [GRANT_EXISTING] }),
          });
        }
        if (url.includes("/accounts")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ accounts: [ACCOUNT_GA4, ACCOUNT_GSC] }),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() =>
      screen.getByTestId(`revoke-grant-${ACCOUNT_GA4.external_account_id}`)
    );

    await user.click(
      screen.getByTestId(`revoke-grant-${ACCOUNT_GA4.external_account_id}`)
    );

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" && opts?.method === "DELETE"
      );
      expect(deleteCall).toBeTruthy();
      expect(deleteCall![0]).toContain(ACCOUNT_GA4.external_account_id);
      expect(deleteCall![0]).toContain("org_acme01");
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : exposer un compte (AC5)
// ---------------------------------------------------------------------------

describe("CredentialGrantsPanel — exposer un compte", () => {
  it("envoie POST sur clic 'Exposer'", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") {
          return Promise.resolve({
            ok: true,
            status: 201,
            json: async () => ({
              ...GRANT_EXISTING,
              external_account_id: ACCOUNT_GSC.external_account_id,
            }),
          });
        }
        if (url.includes("/grants")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ grants: [GRANT_EXISTING] }),
          });
        }
        if (url.includes("/accounts")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ accounts: [ACCOUNT_GA4, ACCOUNT_GSC] }),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() =>
      screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
    );

    await user.click(
      screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
    );

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" && opts?.method === "POST"
      );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(postCall![1].body as string);
      expect(body.grantee_org_id).toBe("org_acme01");
    });
  });

  it("affiche Alert severity='info' si 409 already granted (AD-9)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") {
          return Promise.resolve({
            ok: false,
            status: 409,
            json: async () => ({
              code: "conflict",
              message: "already granted",
            }),
          });
        }
        if (url.includes("/grants")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ grants: [] }),
          });
        }
        if (url.includes("/accounts")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ accounts: [ACCOUNT_GSC] }),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() =>
      screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
    );

    await user.click(
      screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("grants-op-info")).toBeInTheDocument();
      expect(
        screen.getByText(/this account is already exposed to this organization/i)
      ).toBeInTheDocument();
    });
  });

  it("affiche Alert severity='error' si 403 (AD-9)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") {
          return Promise.resolve({
            ok: false,
            status: 403,
            json: async () => ({
              code: "forbidden",
              message: "Access denied",
            }),
          });
        }
        if (url.includes("/grants")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ grants: [] }),
          });
        }
        if (url.includes("/accounts")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ accounts: [ACCOUNT_GSC] }),
          });
        }
        return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <CredentialGrantsPanel orgId="org_acme01" credentialId="cred_ga4_001" />
    );

    await waitFor(() =>
      screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
    );

    await user.click(
      screen.getByTestId(`expose-grant-${ACCOUNT_GSC.external_account_id}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("grants-op-error")).toBeInTheDocument();
      expect(screen.getByText(/insufficient permissions/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : saisie manuelle du credential ID
// ---------------------------------------------------------------------------

describe("CredentialGrantsPanel — saisie manuelle credential ID", () => {
  it("affiche le champ 'Identifiant de la connexion'", () => {
    renderWithTheme(<CredentialGrantsPanel orgId="org_acme01" />);
    expect(
      screen.getByLabelText(/connection identifier/i)
    ).toBeInTheDocument();
  });

  it("le bouton 'Charger' déclenche le chargement quand un ID est saisi", async () => {
    const user = userEvent.setup();
    mockFetchAccountsAndGrants([ACCOUNT_GA4], []);

    renderWithTheme(<CredentialGrantsPanel orgId="org_acme01" />);

    const credInput = screen.getByLabelText(/connection identifier/i);
    await user.type(credInput, "cred_ga4_001");
    await user.click(screen.getByTestId("credential-load-button"));

    await waitFor(() => {
      expect(screen.getByTestId("accounts-table")).toBeInTheDocument();
    });
  });
});
