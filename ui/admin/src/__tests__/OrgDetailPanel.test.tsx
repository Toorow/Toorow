/**
 * Vitest tests pour OrgDetailPanel (Story 21.8, AC4).
 *
 * Coverage :
 *   - Rendu de la liste des membres (GET /api/organizations/{id}/members)
 *   - Ajout d'un membre (POST)
 *   - Changement de rôle (PATCH)
 *   - Retrait d'un membre (DELETE)
 *   - 409 last-owner guard → Alert severity="warning" honnête (AD-9)
 *   - Membres suspendus affichés avec leur statut
 *
 * AI-54 : fixtures calquées sur la vraie réponse API :
 *   GET /api/organizations/{org_id}/members
 *   → {"members": [{id, org_id, identity, role, status,
 *                   invited_by, invited_at, joined_at, created_at}]}
 *
 * AD-9 : 409 last-owner guard → visible en Alert warning, jamais swallowed.
 * UX-DR10 : copie française accentuée.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import OrgDetailPanel from "../orgs/OrgDetailPanel";
import type { OrgMember } from "../orgs/OrgDetailPanel";
import type { Org } from "../orgs/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54 : réponse réelle de GET /api/organizations/{id}/members)
// ---------------------------------------------------------------------------

const ORG: Org = {
  id: "org_acme01",
  name: "Acme Corp",
  slug: "acme-corp",
  status: "active",
  billing_ref: null,
  brand_primary: null,
  brand_secondary: null,
  brand_accent: null,
  logo_url: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  archived_at: null,
};

// Membres avec les champs exacts retournés par _list_org_members
const MEMBER_OWNER: OrgMember = {
  id: "mem_001",
  org_id: "org_acme01",
  identity: "alice@example.com",
  role: "owner",
  status: "active",
  invited_by: null,
  invited_at: null,
  joined_at: "2026-01-01T00:00:00Z",
  created_at: "2026-01-01T00:00:00Z",
};

const MEMBER_EDITOR: OrgMember = {
  id: "mem_002",
  org_id: "org_acme01",
  identity: "bob@example.com",
  role: "member",
  status: "active",
  invited_by: "alice@example.com",
  invited_at: "2026-01-10T00:00:00Z",
  joined_at: "2026-01-11T00:00:00Z",
  created_at: "2026-01-10T00:00:00Z",
};

const MEMBER_SUSPENDED: OrgMember = {
  id: "mem_003",
  org_id: "org_acme01",
  identity: "carol@example.com",
  role: "viewer",
  status: "suspended",
  invited_by: "alice@example.com",
  invited_at: "2026-02-01T00:00:00Z",
  joined_at: null,
  created_at: "2026-02-01T00:00:00Z",
};

function mockFetch(members: OrgMember[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ members }),
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : liste des membres
// ---------------------------------------------------------------------------

describe("OrgDetailPanel — liste des membres", () => {
  it("charge et affiche les membres de l'organisation", async () => {
    mockFetch([MEMBER_OWNER, MEMBER_EDITOR]);
    renderWithTheme(<OrgDetailPanel org={ORG} />);

    await waitFor(() => {
      expect(screen.getByTestId("members-table")).toBeInTheDocument();
      expect(screen.getByText("alice@example.com")).toBeInTheDocument();
      expect(screen.getByText("bob@example.com")).toBeInTheDocument();
    });
  });

  it("affiche 'Aucun membre' quand la liste est vide", async () => {
    mockFetch([]);
    renderWithTheme(<OrgDetailPanel org={ORG} />);

    await waitFor(() => {
      expect(
        screen.getByText(/No members in this organization/i)
      ).toBeInTheDocument();
    });
  });

  it("affiche les membres suspendus avec le statut 'suspended'", async () => {
    mockFetch([MEMBER_OWNER, MEMBER_SUSPENDED]);
    renderWithTheme(<OrgDetailPanel org={ORG} />);

    await waitFor(() => {
      // Le statut "suspended" doit être visible (texte accompagne la couleur — WCAG AA)
      const suspendedRow = screen.getByTestId(`member-row-${MEMBER_SUSPENDED.identity}`);
      expect(suspendedRow).toBeInTheDocument();
      // Le chip de statut contient le texte "suspended"
      expect(screen.getByLabelText(/status: suspended/i)).toBeInTheDocument();
    });
  });

  it("affiche un Alert severity='error' si le fetch échoue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ code: "db_error", message: "Database error" }),
      })
    );
    renderWithTheme(<OrgDetailPanel org={ORG} />);

    await waitFor(() => {
      expect(screen.getByTestId("members-error")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : ajout membre (AC4)
// ---------------------------------------------------------------------------

describe("OrgDetailPanel — ajout membre", () => {
  it("envoie POST /api/organizations/{id}/members avec identity et role", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      // GET membres initial
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ members: [MEMBER_OWNER] }),
      })
      // POST ajout
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => MEMBER_EDITOR,
      })
      // GET membres rechargement
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ members: [MEMBER_OWNER, MEMBER_EDITOR] }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<OrgDetailPanel org={ORG} />);
    await waitFor(() => screen.getByTestId("add-member-identity"));

    const identityInput = screen.getByLabelText(/new member identity/i);
    await user.type(identityInput, "bob@example.com");

    await user.click(screen.getByTestId("add-member-submit"));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes("/members") &&
          opts?.method === "POST"
      );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(postCall![1].body as string);
      expect(body.identity).toBe("bob@example.com");
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : changement de rôle (AC4)
// ---------------------------------------------------------------------------

describe("OrgDetailPanel — changement de rôle", () => {
  it("envoie PATCH avec le nouveau rôle quand le select change", async () => {
    const fetchMock = vi
      .fn()
      // GET membres initial
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ members: [MEMBER_OWNER, MEMBER_EDITOR] }),
      })
      // PATCH role
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...MEMBER_EDITOR, role: "admin" }),
      })
      // GET rechargement
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          members: [MEMBER_OWNER, { ...MEMBER_EDITOR, role: "admin" }],
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<OrgDetailPanel org={ORG} />);
    await waitFor(() => screen.getByTestId("members-table"));

    // Ouvrir le select de rôle pour MEMBER_EDITOR
    const roleSelect = screen.getByTestId(`role-select-${MEMBER_EDITOR.identity}`);
    const combobox = roleSelect.querySelector("[role='combobox']") ?? roleSelect;
    fireEvent.mouseDown(combobox);

    const adminOption = await screen.findByRole("option", { name: /admin/i });
    fireEvent.click(adminOption);

    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes("/members/") &&
          opts?.method === "PATCH"
      );
      expect(patchCall).toBeTruthy();
      const body = JSON.parse(patchCall![1].body as string);
      expect(body.role).toBe("admin");
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : retrait membre + 409 last-owner guard (AC4, AD-9)
// ---------------------------------------------------------------------------

describe("OrgDetailPanel — retrait membre (AD-9 last-owner guard)", () => {
  it("envoie DELETE sur clic 'Retirer'", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      // GET membres
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ members: [MEMBER_OWNER, MEMBER_EDITOR] }),
      })
      // DELETE
      .mockResolvedValueOnce({
        ok: true,
        status: 204,
        json: async () => ({}),
      })
      // GET rechargement
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ members: [MEMBER_OWNER] }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<OrgDetailPanel org={ORG} />);
    await waitFor(() => screen.getByTestId(`remove-member-${MEMBER_EDITOR.identity}`));

    await user.click(
      screen.getByTestId(`remove-member-${MEMBER_EDITOR.identity}`)
    );

    await waitFor(() => {
      const deleteCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes("/members/") &&
          opts?.method === "DELETE"
      );
      expect(deleteCall).toBeTruthy();
    });
  });

  it("affiche Alert severity='warning' si le serveur retourne 409 last-owner (AD-9)", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        // GET membres
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: async () => ({ members: [MEMBER_OWNER] }),
        })
        // DELETE → 409 last-owner
        .mockResolvedValueOnce({
          ok: false,
          status: 409,
          json: async () => ({
            code: "conflict",
            message: "cannot remove the last active owner",
          }),
        })
    );

    renderWithTheme(<OrgDetailPanel org={ORG} />);
    await waitFor(() =>
      screen.getByTestId(`remove-member-${MEMBER_OWNER.identity}`)
    );

    await user.click(
      screen.getByTestId(`remove-member-${MEMBER_OWNER.identity}`)
    );

    await waitFor(() => {
      const warning = screen.getByTestId("members-warning");
      expect(warning).toBeInTheDocument();
      expect(
        screen.getByText(/cannot remove the organization's last active owner/i)
      ).toBeInTheDocument();
    });
  });

  it("surfacé le 409 même avec le message 'cannot drop the last active owner' (variante)", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: async () => ({ members: [MEMBER_OWNER] }),
        })
        .mockResolvedValueOnce({
          ok: false,
          status: 409,
          json: async () => ({
            code: "conflict",
            message: "cannot drop the last active owner",
          }),
        })
    );

    renderWithTheme(<OrgDetailPanel org={ORG} />);
    await waitFor(() =>
      screen.getByTestId(`remove-member-${MEMBER_OWNER.identity}`)
    );
    await user.click(
      screen.getByTestId(`remove-member-${MEMBER_OWNER.identity}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("members-warning")).toBeInTheDocument();
    });
  });

  it("affiche Alert severity='error' si le serveur retourne 403", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          json: async () => ({ members: [MEMBER_OWNER, MEMBER_EDITOR] }),
        })
        .mockResolvedValueOnce({
          ok: false,
          status: 403,
          json: async () => ({
            code: "forbidden",
            message: "Acces refuse",
          }),
        })
    );

    renderWithTheme(<OrgDetailPanel org={ORG} />);
    await waitFor(() =>
      screen.getByTestId(`remove-member-${MEMBER_EDITOR.identity}`)
    );
    await user.click(
      screen.getByTestId(`remove-member-${MEMBER_EDITOR.identity}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("members-mutation-error")).toBeInTheDocument();
      expect(screen.getByText(/insufficient permissions/i)).toBeInTheDocument();
    });
  });
});
