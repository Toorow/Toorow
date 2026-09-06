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
import DataAccessGrantsPanel from "./DataAccessGrantsPanel";

/**
 * The MUI `ThemeProvider` this used to wrap is gone with the MUI components it
 * carried: the panel now draws from the shadcn primitives, whose tokens come
 * from the stylesheet, not from a React context. The helper name is kept so the
 * nine call sites below stay a one-line diff — a rename would have buried the
 * only change that matters in noise.
 */
function renderWithTheme(ui: React.ReactElement) {
  return render(ui);
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
  lifecycle_state: "effective" as const,
  dataset_id: "org_acme_marts",
  role: "roles/bigquery.dataViewer" as const,
  effective_at: "2026-07-01T00:00:01Z",
  revoked_at: null,
  last_provider_error: null,
  provider_error_at: null,
  revocation_state: null,
  grant_operation_id: "op_grant_1",
  revoke_operation_id: null,
  provider_attempt_started_at: null,
  updated_at: "2026-07-01T00:00:01Z",
};

const GRANT_2 = {
  id: "dagrant_02HXYZ",
  org_id: "org_acme01",
  principal: "user:bob@example.com",
  granted_by: "alice@example.com",
  created_at: "2026-07-02T00:00:00Z",
  lifecycle_state: "failed" as const,
  dataset_id: "org_acme_marts",
  role: "roles/bigquery.dataViewer" as const,
  effective_at: null,
  revoked_at: null,
  last_provider_error: "Forbidden: grant BigQuery Data Owner to the runtime identity.",
  provider_error_at: "2026-07-02T00:00:01Z",
  revocation_state: null,
  grant_operation_id: "op_grant_2",
  revoke_operation_id: null,
  provider_attempt_started_at: null,
  updated_at: "2026-07-02T00:00:01Z",
};

const REQUESTED = {
  ...GRANT_2,
  id: "dagrant_03HXYZ",
  lifecycle_state: "requested" as const,
  dataset_id: null,
  last_provider_error: "Provider outcome is unknown; retry the request.",
  created_at: "2026-07-03T10:11:12Z",
  provider_error_at: "2026-07-03T10:11:13Z",
};

const REVOCATION_FAILED = {
  ...GRANT_1,
  id: "dagrant_04HXYZ",
  revocation_state: "failed" as const,
  revoke_operation_id: "op_revoke_4",
  last_provider_error: "Forbidden while revoking",
  provider_error_at: "2026-07-04T10:11:13Z",
};

function mockFetchList(
  grants: Array<typeof GRANT_1 | typeof GRANT_2 | typeof REQUESTED | typeof REVOCATION_FAILED> = []
) {
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
      expect(screen.getByText("Effective")).toBeInTheDocument();
      expect(screen.getByText("Failed")).toBeInTheDocument();
      expect(screen.getAllByText("org_acme_marts")).toHaveLength(2);
      expect(
        screen.getByText(/grant BigQuery Data Owner to the runtime identity/i)
      ).toBeInTheDocument();
    });
  });

  it("affiche le formulaire d'ajout de principal", () => {
    mockFetchList([]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);
    expect(screen.getByTestId("data-access-principal-kind")).toBeInTheDocument();
    expect(screen.getByTestId("data-access-principal-input")).toBeInTheDocument();
    expect(screen.getByTestId("data-access-grant-button")).toBeInTheDocument();
  });

  it("rend l'historique requested/revocation_failed et leurs vraies reprises", async () => {
    mockFetchList([REQUESTED, REVOCATION_FAILED]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    expect(await screen.findByText("Requested")).toBeInTheDocument();
    expect(screen.getByText("Revocation failed")).toBeInTheDocument();
    expect(screen.getByText("Requested at")).toBeInTheDocument();
    expect(screen.getByTestId(`data-access-retry-${REQUESTED.id}`)).toHaveTextContent("Retry");
    expect(
      screen.getByTestId(`data-access-retry-revoke-${REVOCATION_FAILED.id}`)
    ).toHaveTextContent("Retry revocation");
    expect(screen.getByTestId(`data-access-revoke-${REQUESTED.id}`)).toHaveTextContent(
      "Cancel request"
    );
  });

  it("n'offre que les trois natures que le serveur accepte", () => {
    mockFetchList([]);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);
    // `dataset_access_api.py:44` — user | serviceAccount | group, and no other.
    const options = Array.from(
      screen.getByTestId("data-access-principal-kind").querySelectorAll("option")
    ).map((o) => (o as HTMLOptionElement).value);
    expect(options).toEqual(["user", "serviceAccount", "group"]);
  });
});

// ---------------------------------------------------------------------------
// Tests : composition et refus côté client
// ---------------------------------------------------------------------------

describe("DataAccessGrantsPanel — le principal est composé", () => {
  it("compose type:identifiant à l'octet près pour les trois natures", async () => {
    const cases: Array<[string, string, string]> = [
      ["user", "person@example.com", "user:person@example.com"],
      [
        "serviceAccount",
        "sa@project.iam.gserviceaccount.com",
        "serviceAccount:sa@project.iam.gserviceaccount.com",
      ],
      ["group", "team@example.com", "group:team@example.com"],
    ];
    for (const [kind, identifier, wire] of cases) {
      const user = userEvent.setup();
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ grants: [] }),
      });
      vi.stubGlobal("fetch", fetchMock);
      const view = renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

      await waitFor(() =>
        expect(screen.getByTestId("data-access-empty")).toBeInTheDocument()
      );
      await user.selectOptions(screen.getByTestId("data-access-principal-kind"), kind);
      await user.type(screen.getByLabelText("Identifier"), identifier);
      await user.click(screen.getByTestId("data-access-grant-button"));

      await waitFor(() => {
        const post = fetchMock.mock.calls.find(([, opts]) => opts?.method === "POST");
        expect(post).toBeTruthy();
        expect(JSON.parse(post![1].body as string).principal).toBe(wire);
      });
      view.unmount();
      vi.unstubAllGlobals();
    }
  });

  it("refuse un identifiant sans domaine, nomme la réparation, et n'appelle pas le serveur", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ grants: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      expect(screen.getByTestId("data-access-empty")).toBeInTheDocument()
    );
    await user.selectOptions(
      screen.getByTestId("data-access-principal-kind"),
      "serviceAccount"
    );
    await user.type(screen.getByLabelText("Identifier"), "sa");
    await user.click(screen.getByTestId("data-access-grant-button"));

    // The repair, not the grammar.
    expect(
      screen.getByText(/Write the service account's full address/i)
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Identifier")).toHaveAttribute("aria-invalid", "true");
    expect(fetchMock.mock.calls.some(([, opts]) => opts?.method === "POST")).toBe(false);
  });

  it("laisse le serveur rester l'autorité : un 422 remplace le message du client", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() =>
        Promise.resolve({ ok: true, status: 200, json: async () => ({ grants: [] }) })
      )
      .mockImplementationOnce(() =>
        Promise.resolve({
          ok: false,
          status: 422,
          json: async () => ({
            code: "invalid_input",
            message: "principal must be type:identifier with type in {user, serviceAccount, group}",
          }),
        })
      );
    vi.stubGlobal("fetch", fetchMock);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await waitFor(() =>
      expect(screen.getByTestId("data-access-empty")).toBeInTheDocument()
    );
    // A shape the client accepts and the server may still refuse.
    await user.type(screen.getByLabelText("Identifier"), "person@example.com");
    await user.click(screen.getByTestId("data-access-grant-button"));

    await waitFor(() => {
      expect(
        screen.getByText(/principal must be type:identifier/i)
      ).toBeInTheDocument();
    });
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

    // The kind is CHOSEN and the identifier typed; the wire format is composed.
    await user.selectOptions(
      screen.getByTestId("data-access-principal-kind"),
      "serviceAccount"
    );
    const input = screen.getByLabelText("Identifier");
    await user.type(input, "sa@project.iam.gserviceaccount.com");
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

  it("utilise une nouvelle clé après un échec provider terminal", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ grants: [] }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 502,
        json: async () => ({ message: "provider refused" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ grants: [GRANT_2] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => GRANT_1,
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ grants: [GRANT_1] }),
      });
    vi.stubGlobal("fetch", fetchMock);
    renderWithTheme(<DataAccessGrantsPanel orgId="org_acme01" />);

    await screen.findByTestId("data-access-empty");
    await user.selectOptions(
      screen.getByTestId("data-access-principal-kind"),
      "serviceAccount"
    );
    await user.type(
      screen.getByLabelText("Identifier"),
      "sa@project.iam.gserviceaccount.com"
    );
    await user.click(screen.getByTestId("data-access-grant-button"));
    await user.click(await screen.findByTestId(`data-access-retry-${GRANT_2.id}`));

    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2);
    });
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts[0][1].headers["Idempotency-Key"]).not.toBe(
      posts[1][1].headers["Idempotency-Key"]
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

    const input = screen.getByLabelText("Identifier");
    await user.type(input, "dup@example.com");
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
    expect(screen.getByText("Revoke this marts access?")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, opts]) => opts?.method === "DELETE")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Revoke access" }));

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
    await user.click(screen.getByRole("button", { name: "Revoke access" }));

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
    await user.click(screen.getByRole("button", { name: "Revoke access" }));

    await waitFor(() => {
      expect(screen.getByTestId("data-access-op-info")).toBeInTheDocument();
    });
  });
});
