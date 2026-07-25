/**
 * Vitest tests — MappingTab (Story 22.7, onglet Mapping).
 *
 * Coverage :
 *   - Affichage des mappings actifs et orphelins (badge texte + aria)
 *   - Section [Actuals non mappés] toujours visible
 *   - Validation client : somme ≠ 100 % → blocage avec message FR
 *   - Validation client : somme = 100 % → envoi PUT autorisé
 *   - 422 serveur → Alert honnête
 *   - État vide actuals non mappés
 *   - Erreur chargement actuals → Alert (jamais silencieux — AD-9)
 *
 * AI-54 : fixtures calquées sur les réponses réelles de l'API.
 * AD-9 : actuals non mappés jamais filtrés silencieusement.
 * WCAG 2.2 AA : statut jamais par la couleur seule.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import MappingTab from "../mediaplans/MappingTab";
import type { LineMapping, MediaPlanDetail } from "../mediaplans/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54)
// ---------------------------------------------------------------------------

const PLAN: MediaPlanDetail = {
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
    source_note: null,
    is_active: true,
    created_by: "alice@example.com",
    created_at: "2026-04-10T00:00:00Z",
    published_at: "2026-04-11T00:00:00Z",
  },
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

const MAPPING_ACTIVE: LineMapping = {
  id: "map-001",
  plan_id: "plan-aaa",
  line_key: "digital/meta",
  connector: "meta_ads",
  campaign_ref: "campaign_A",
  split_weight: 0.7,
  status: "active",
  created_at: "2026-04-12T00:00:00Z",
  updated_at: "2026-04-12T00:00:00Z",
};

const MAPPING_ORPHANED: LineMapping = {
  id: "map-002",
  plan_id: "plan-aaa",
  line_key: "digital/meta",
  connector: "meta_ads",
  campaign_ref: "campaign_B",
  split_weight: 0.3,
  status: "orphaned",
  created_at: "2026-03-01T00:00:00Z",
  updated_at: "2026-03-01T00:00:00Z",
};

function stubMappingFetch(
  byLine: Record<string, LineMapping[]> = {},
  actuals: object[] = []
) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (url.includes("unmapped-actuals")) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ actuals, as_of: null }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ by_line: byLine }),
      });
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : affichage des mappings
// ---------------------------------------------------------------------------

describe("MappingTab — affichage", () => {
  it("affiche les mappings actifs et orphelins avec badge texte (WCAG)", async () => {
    stubMappingFetch(
      { "digital/meta": [MAPPING_ACTIVE, MAPPING_ORPHANED] },
      []
    );
    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() => {
      // Mappings présents
      expect(screen.getByTestId(`mapping-row-${MAPPING_ACTIVE.id}`)).toBeInTheDocument();
      expect(screen.getByTestId(`mapping-row-${MAPPING_ORPHANED.id}`)).toBeInTheDocument();
    });

    // Badge "Orphaned" avec aria-label (WCAG — pas couleur seule)
    expect(screen.getByLabelText(/state: orphaned/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/state: active/i)).toBeInTheDocument();
  });

  it("affiche la section [Actuals non mappés] toujours présente", async () => {
    stubMappingFetch({}, []);
    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("unmapped-actuals-section")).toBeInTheDocument();
    });
  });

  it("affiche le message 'aucune dépense non mappée' quand la liste est vide", async () => {
    stubMappingFetch({}, []);
    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("unmapped-empty")).toBeInTheDocument();
    });
  });

  it("affiche les actuals non mappés dans un tableau", async () => {
    stubMappingFetch({}, [
      {
        connector: "google_ads",
        campaign_ref: "campaign_X",
        spend: 5000,
        currency: "EUR",
        date_debut: null,
        date_fin: null,
      },
    ]);
    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("unmapped-table")).toBeInTheDocument();
      expect(screen.getByText("google_ads")).toBeInTheDocument();
      expect(screen.getByText("campaign_X")).toBeInTheDocument();
    });
  });

  it("affiche une Alert si les actuals sont indisponibles (503 — AD-9 jamais silencieux)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (url.includes("unmapped-actuals")) {
          return Promise.resolve({
            ok: false,
            status: 503,
            json: async () => ({
              code: "warehouse_unavailable",
              message: "Dépenses réelles indisponibles pour le moment",
            }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ by_line: {} }),
        });
      })
    );
    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() => {
      expect(screen.getByTestId("unmapped-error")).toBeInTheDocument();
      expect(screen.getByText(/indisponibles/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : validation client somme splits
// ---------------------------------------------------------------------------

describe("MappingTab — validation somme splits", () => {
  it("bloque l'envoi si la somme ≠ 100 % et affiche un message FR", async () => {
    const user = userEvent.setup();
    stubMappingFetch({ "digital/meta": [] }, []);
    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() =>
      screen.getByTestId("edit-mapping-digital/meta")
    );
    await user.click(screen.getByTestId("edit-mapping-digital/meta"));

    // Ajouter deux entrées avec somme ≠ 100 %
    const addBtn = screen.getByText("+ Add campaign");
    await user.click(addBtn);
    await user.click(addBtn);

    // Mettre des splits qui ne font pas 100 %
    const splitInputs = screen.getAllByLabelText(/split percentage/i);
    await user.clear(splitInputs[0]);
    await user.type(splitInputs[0], "30");
    await user.clear(splitInputs[1]);
    await user.type(splitInputs[1], "30");

    // Le bouton Enregistrer doit être désactivé (somme != 100 %)
    const saveBtn = screen.getByTestId("mapping-save-btn-digital/meta");
    expect(saveBtn).toBeDisabled();

    // L'indicateur de somme affiche le pourcentage incorrect
    const indicator = screen.getByTestId("split-sum-indicator");
    expect(indicator).toBeInTheDocument();
    expect(indicator.textContent).toMatch(/must be 100%/i);
  });

  it("autorise l'envoi si la somme = 100 % et envoie PUT", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string, opts?: { method?: string }) => {
        if (url.includes("unmapped-actuals")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ actuals: [], as_of: null }),
          });
        }
        if (opts?.method === "PUT") {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ mappings: [] }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ by_line: { "digital/meta": [] } }),
        });
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<MappingTab plan={PLAN} />);

    await waitFor(() =>
      screen.getByTestId("edit-mapping-digital/meta")
    );
    await user.click(screen.getByTestId("edit-mapping-digital/meta"));

    // Ajouter une entrée et la mettre à 100 %
    await user.click(screen.getByText("+ Add campaign"));

    const splitInputs = screen.getAllByLabelText(/split percentage/i);
    await user.clear(splitInputs[0]);
    await user.type(splitInputs[0], "100");

    // Le bouton doit être actif
    const saveBtn = screen.getByTestId("mapping-save-btn-digital/meta");
    expect(saveBtn).not.toBeDisabled();

    await user.click(saveBtn);

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        ([url, opts]) =>
          typeof url === "string" &&
          url.includes("/mappings") &&
          opts?.method === "PUT"
      );
      expect(putCall).toBeTruthy();
    });
  });

  it("affiche une Alert d'erreur si le serveur retourne 422 (somme != 100 %)", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockImplementation((url: string, opts?: { method?: string }) => {
        if (url.includes("unmapped-actuals")) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ actuals: [], as_of: null }),
          });
        }
        if (opts?.method === "PUT") {
          return Promise.resolve({
            ok: false,
            status: 422,
            json: async () => ({
              code: "validation_error",
              message: "La somme des splits doit être 1.0.",
            }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ by_line: { "digital/meta": [] } }),
        });
      });
    vi.stubGlobal("fetch", fetchMock);

    renderWithTheme(<MappingTab plan={PLAN} />);
    await waitFor(() => screen.getByTestId("edit-mapping-digital/meta"));
    await user.click(screen.getByTestId("edit-mapping-digital/meta"));

    await user.click(screen.getByText("+ Add campaign"));
    const splitInputs = screen.getAllByLabelText(/split percentage/i);
    await user.clear(splitInputs[0]);
    await user.type(splitInputs[0], "100");

    const saveBtn = screen.getByTestId("mapping-save-btn-digital/meta");
    await user.click(saveBtn);

    await waitFor(() => {
      expect(
        screen.getByTestId("mapping-save-error-digital/meta")
      ).toBeInTheDocument();
    });
  });
});
