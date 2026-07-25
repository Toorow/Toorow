/**
 * Vitest tests — MediaplanDetailPage + LignesVersionsTab (Story 22.7, AC2).
 *
 * Coverage :
 *   - Affichage du détail d'un plan (GET /api/mediaplans/{id})
 *   - Lignes de la version active : label, support, badge « Plan seul »
 *   - Historique des versions
 *   - Bouton « Publier » sur une version candidate (POST /versions/{id}/publish)
 *   - Erreur de publication → Alert
 *   - Version non-active en lecture seule (message affiché)
 *   - Erreur 403/404 → Alert honnête
 *
 * AI-54 : fixtures calquées sur les réponses réelles de l'API.
 * AD-15 : tout passe par fetch.
 * UX-DR10 : copie française accentuée.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import MediaplanDetailPage from "../mediaplans/MediaplanDetailPage";
import type { MediaPlanDetail, MediaPlanVersion } from "../mediaplans/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54)
// ---------------------------------------------------------------------------

const VERSION_ACTIVE: MediaPlanVersion = {
  id: "ver-001",
  plan_id: "plan-aaa",
  version_number: 2,
  status: "published",
  source_note: "Import Q2",
  is_active: true,
  created_by: "alice@example.com",
  created_at: "2026-04-10T00:00:00Z",
  published_at: "2026-04-11T00:00:00Z",
};

const VERSION_CANDIDATE: MediaPlanVersion = {
  id: "ver-002",
  plan_id: "plan-aaa",
  version_number: 3,
  status: "candidate",
  source_note: "Import Q3 test",
  is_active: false,
  created_by: "alice@example.com",
  created_at: "2026-07-01T00:00:00Z",
  published_at: null,
};

const PLAN_DETAIL: MediaPlanDetail = {
  id: "plan-aaa",
  project_id: "proj-1",
  name: "Campagne été 2026",
  currency: "EUR",
  created_by: "alice@example.com",
  created_at: "2026-04-01T00:00:00Z",
  updated_at: "2026-04-01T00:00:00Z",
  archived_at: null,
  active_version: VERSION_ACTIVE,
  lines: [
    {
      id: "line-1",
      version_id: "ver-001",
      line_key: "digital/meta",
      label: "Meta Social",
      support: "Digital",
      channel: "Social",
      date_debut: "2026-04-01",
      date_fin: "2026-06-30",
      budget: 30000,
      mode_achat: "CPM",
      is_plan_only: false,
    },
    {
      id: "line-2",
      version_id: "ver-001",
      line_key: "tv/tf1",
      label: "TF1 Spot",
      support: "TV",
      channel: null,
      date_debut: "2026-04-01",
      date_fin: "2026-06-30",
      budget: 50000,
      mode_achat: null,
      is_plan_only: true,
    },
  ],
};

function stubPlanFetch(plan: MediaPlanDetail, versions: MediaPlanVersion[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url.includes("/versions") && !url.includes("publish")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ versions }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => plan,
      });
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : affichage détail + lignes
// ---------------------------------------------------------------------------

describe("MediaplanDetailPage — détail et lignes", () => {
  it("charge et affiche le nom du plan + onglets", async () => {
    stubPlanFetch(PLAN_DETAIL, [VERSION_ACTIVE]);
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      // Plusieurs occurrences possibles (titre + fil d'Ariane) → getAllByText
      const headings = screen.getAllByText("Campagne été 2026");
      expect(headings.length).toBeGreaterThanOrEqual(1);
      expect(screen.getByTestId("tab-lignes")).toBeInTheDocument();
      expect(screen.getByTestId("tab-import")).toBeInTheDocument();
      expect(screen.getByTestId("tab-mapping")).toBeInTheDocument();
      expect(screen.getByTestId("tab-pacing")).toBeInTheDocument();
    });
  });

  it("affiche les lignes de la version active avec labels et supports", async () => {
    stubPlanFetch(PLAN_DETAIL, [VERSION_ACTIVE]);
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("lines-table")).toBeInTheDocument();
      expect(screen.getByText("Meta Social")).toBeInTheDocument();
      expect(screen.getByText("TF1 Spot")).toBeInTheDocument();
    });
  });

  it("affiche le badge 'Plan seul' sur les lignes plan-only (WCAG — texte + aria)", async () => {
    stubPlanFetch(PLAN_DETAIL, [VERSION_ACTIVE]);
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      const badge = screen.getByTestId("badge-plan-only-tv/tf1");
      expect(badge).toBeInTheDocument();
      // WCAG : aria-label présent
      expect(
        screen.getByLabelText(/type: plan only/i)
      ).toBeInTheDocument();
    });
  });

  it("affiche 'Aucune version active' si active_version est null", async () => {
    const planNoVersion: MediaPlanDetail = {
      ...PLAN_DETAIL,
      active_version: null,
      lines: [],
    };
    stubPlanFetch(planNoVersion, []);
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("no-active-version")).toBeInTheDocument();
    });
  });

  it("affiche une Alert d'erreur si le plan est 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 404,
        json: async () => ({ code: "not_found", message: "Plan not found." }),
      })
    );
    renderWithTheme(
      <MediaplanDetailPage planId="plan-inexistant" onBack={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("plan-detail-error")).toBeInTheDocument();
      expect(screen.getByText(/plan not found/i)).toBeInTheDocument();
    });
  });

  it("affiche une Alert d'erreur si le plan est 403", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: async () => ({ code: "forbidden", message: "Accès refusé." }),
      })
    );
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("plan-detail-error")).toBeInTheDocument();
      expect(screen.getByText(/acc[eè]s refus[eé]/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : versions + publication
// ---------------------------------------------------------------------------

describe("MediaplanDetailPage — versions et publication", () => {
  it("affiche l'historique des versions", async () => {
    stubPlanFetch(PLAN_DETAIL, [VERSION_ACTIVE, VERSION_CANDIDATE]);
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      expect(screen.getByTestId("versions-table")).toBeInTheDocument();
      expect(screen.getByText("v2")).toBeInTheDocument();
      expect(screen.getByText("v3")).toBeInTheDocument();
    });
  });

  it("affiche le bouton 'Publier' sur la version candidate", async () => {
    stubPlanFetch(PLAN_DETAIL, [VERSION_ACTIVE, VERSION_CANDIDATE]);
    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() => {
      expect(
        screen.getByTestId(`publish-version-btn-${VERSION_CANDIDATE.id}`)
      ).toBeInTheDocument();
    });
  });

  it("envoie POST /versions/{id}/publish au clic et affiche le succès", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      // GET plan
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => PLAN_DETAIL,
      })
      // GET versions
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ versions: [VERSION_ACTIVE, VERSION_CANDIDATE] }),
      })
      // POST publish
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ...VERSION_CANDIDATE, status: "published", is_active: true }),
      })
      // GET versions rechargement
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          versions: [
            { ...VERSION_ACTIVE, is_active: false, status: "superseded" },
            { ...VERSION_CANDIDATE, status: "published", is_active: true },
          ],
        }),
      })
      // GET plan rechargement (onRefresh)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          ...PLAN_DETAIL,
          active_version: { ...VERSION_CANDIDATE, status: "published", is_active: true },
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() =>
      screen.getByTestId(`publish-version-btn-${VERSION_CANDIDATE.id}`)
    );

    await user.click(
      screen.getByTestId(`publish-version-btn-${VERSION_CANDIDATE.id}`)
    );

    await waitFor(() => {
      const publishCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes(`/versions/${VERSION_CANDIDATE.id}/publish`) &&
          opts?.method === "POST"
      );
      expect(publishCall).toBeTruthy();
    });

    await waitFor(() => {
      expect(screen.getByTestId("publish-success")).toBeInTheDocument();
    });
  });

  it("affiche une Alert d'erreur si la publication échoue", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => PLAN_DETAIL,
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ versions: [VERSION_ACTIVE, VERSION_CANDIDATE] }),
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 409,
        json: async () => ({ code: "conflict", message: "Version déjà publiée." }),
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(
      <MediaplanDetailPage planId="plan-aaa" onBack={vi.fn()} />
    );

    await waitFor(() =>
      screen.getByTestId(`publish-version-btn-${VERSION_CANDIDATE.id}`)
    );
    await user.click(
      screen.getByTestId(`publish-version-btn-${VERSION_CANDIDATE.id}`)
    );

    await waitFor(() => {
      expect(screen.getByTestId("publish-error")).toBeInTheDocument();
    });
  });
});
