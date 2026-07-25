/**
 * Vitest tests — MediaplansPage (Story 22.7, AC1 : liste + création).
 *
 * Coverage :
 *   - Chargement et affichage de la liste des plans (GET /api/projects/{id}/mediaplans)
 *   - Plusieurs plans actifs simultanément (décision 4 epic 22)
 *   - Badge « Archivé » affiché (texte + aria, WCAG — pas couleur seule)
 *   - État vide propre
 *   - Erreur réseau → Alert
 *   - Création d'un plan (POST) → rechargement
 *   - Erreur de création → Alert dans le dialogue
 *   - Clic sur un plan → onSelectPlan appelé
 *
 * AI-54 : fixtures calquées sur la vraie réponse de
 *   GET /api/projects/{project_id}/mediaplans → {"plans": [...]}
 * AD-15 : tout passe par fetch (zéro accès DB).
 * UX-DR10 : copie française accentuée.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import MediaplansPage from "../mediaplans/MediaplansPage";
import type { MediaPlan } from "../mediaplans/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54)
// ---------------------------------------------------------------------------

const PLAN_ACTIVE: MediaPlan = {
  id: "plan-aaa",
  project_id: "proj-1",
  name: "Campagne été 2026",
  currency: "EUR",
  created_by: "alice@example.com",
  created_at: "2026-04-01T00:00:00Z",
  updated_at: "2026-04-01T00:00:00Z",
  archived_at: null,
  active_version: {
    id: "ver-001",
    plan_id: "plan-aaa",
    version_number: 2,
    status: "published",
    source_note: "Import Q2",
    is_active: true,
    created_by: "alice@example.com",
    created_at: "2026-04-10T00:00:00Z",
    published_at: "2026-04-11T00:00:00Z",
  },
};

const PLAN_ARCHIVED: MediaPlan = {
  id: "plan-bbb",
  project_id: "proj-1",
  name: "Campagne hiver 2025",
  currency: "EUR",
  created_by: "bob@example.com",
  created_at: "2025-11-01T00:00:00Z",
  updated_at: "2026-01-15T00:00:00Z",
  archived_at: "2026-01-15T00:00:00Z",
  active_version: null,
};

const PLAN_NO_VERSION: MediaPlan = {
  id: "plan-ccc",
  project_id: "proj-1",
  name: "Nouveau plan sans version",
  currency: "USD",
  created_by: "alice@example.com",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-01T00:00:00Z",
  archived_at: null,
};

function mockFetchPlans(plans: MediaPlan[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ plans }),
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : affichage de la liste
// ---------------------------------------------------------------------------

describe("MediaplansPage — liste des plans", () => {
  it("charge et affiche les plans du projet", async () => {
    mockFetchPlans([PLAN_ACTIVE, PLAN_ARCHIVED]);
    const onSelectPlan = vi.fn();
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={onSelectPlan} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("plans-table")).toBeInTheDocument();
      expect(screen.getByText("Campagne été 2026")).toBeInTheDocument();
      expect(screen.getByText("Campagne hiver 2025")).toBeInTheDocument();
    });
  });

  it("affiche plusieurs plans actifs simultanément (décision 4 epic 22)", async () => {
    const plan2: MediaPlan = { ...PLAN_ACTIVE, id: "plan-ddd", name: "Plan parallèle" };
    mockFetchPlans([PLAN_ACTIVE, plan2]);
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByText("Campagne été 2026")).toBeInTheDocument();
      expect(screen.getByText("Plan parallèle")).toBeInTheDocument();
    });
  });

  it("affiche le badge 'Archived' avec texte + aria (WCAG)", async () => {
    mockFetchPlans([PLAN_ARCHIVED]);
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );

    await waitFor(() => {
      // Le texte "Archived" doit être visible (WCAG — pas couleur seule)
      expect(screen.getByText("Archived")).toBeInTheDocument();
      expect(screen.getByLabelText(/status: archived/i)).toBeInTheDocument();
    });
  });

  it("affiche 'No active version' si le plan n'a pas de version", async () => {
    mockFetchPlans([PLAN_NO_VERSION]);
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByText(/no active version/i)).toBeInTheDocument();
    });
  });

  it("affiche l'état vide si aucun plan", async () => {
    mockFetchPlans([]);
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("plans-empty")).toBeInTheDocument();
      expect(screen.getByText(/no media plan yet/i)).toBeInTheDocument();
    });
  });

  it("affiche une Alert d'erreur si le fetch échoue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ code: "db_error", message: "Erreur serveur" }),
      })
    );
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("plans-error")).toBeInTheDocument();
    });
  });

  it("appelle onSelectPlan avec l'id du plan au clic sur 'Détail →'", async () => {
    const user = userEvent.setup();
    mockFetchPlans([PLAN_ACTIVE]);
    const onSelectPlan = vi.fn();
    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={onSelectPlan} />
    );

    await waitFor(() =>
      screen.getByTestId(`plan-detail-btn-${PLAN_ACTIVE.id}`)
    );
    await user.click(screen.getByTestId(`plan-detail-btn-${PLAN_ACTIVE.id}`));

    expect(onSelectPlan).toHaveBeenCalledWith(PLAN_ACTIVE.id);
  });
});

// ---------------------------------------------------------------------------
// Tests : création d'un plan
// ---------------------------------------------------------------------------

describe("MediaplansPage — création d'un plan", () => {
  it("ouvre le dialogue et envoie POST /api/projects/{id}/mediaplans", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      // GET liste initiale
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ plans: [] }),
      })
      // POST création
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => PLAN_ACTIVE,
      })
      // GET rechargement
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ plans: [PLAN_ACTIVE] }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );
    await waitFor(() => screen.getByTestId("plans-empty"));

    await user.click(screen.getByTestId("create-plan-btn"));
    await waitFor(() => screen.getByTestId("create-plan-dialog"));

    const nameInput = screen.getByLabelText(/plan name/i);
    await user.type(nameInput, "Mon nouveau plan");

    await user.click(screen.getByTestId("create-plan-submit"));

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes("/mediaplans") &&
          opts?.method === "POST"
      );
      expect(postCall).toBeTruthy();
      const body = JSON.parse(postCall![1].body as string) as Record<string, unknown>;
      expect(body.name).toBe("Mon nouveau plan");
      expect(body.currency).toBe("EUR");
    });
  });

  it("affiche l'erreur de création dans le dialogue", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ plans: [] }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 422,
        json: async () => ({ code: "validation_error", message: "Nom requis." }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <MediaplansPage projectId="proj-1" onSelectPlan={vi.fn()} />
    );
    await waitFor(() => screen.getByTestId("plans-empty"));

    await user.click(screen.getByTestId("create-plan-btn"));
    await waitFor(() => screen.getByTestId("create-plan-dialog"));

    const nameInput = screen.getByLabelText(/plan name/i);
    await user.type(nameInput, "Test");
    await user.click(screen.getByTestId("create-plan-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("create-plan-error")).toBeInTheDocument();
      expect(screen.getByText("Nom requis.")).toBeInTheDocument();
    });
  });
});
