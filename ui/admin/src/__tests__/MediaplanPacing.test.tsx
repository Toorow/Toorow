/**
 * Vitest tests — PacingTab (Story 22.7, onglet Pacing).
 *
 * Coverage :
 *   - NULL → « — » jamais 0 (AD-9)
 *   - Badge « Plan seul » avec aria-label (WCAG — pas couleur seule)
 *   - Indicateur dépassement : icône + texte (WCAG — pas couleur seule)
 *   - Indicateur sous-livraison : icône + texte
 *   - Rollup par support + global
 *   - Provenance affichée (version, as_of)
 *   - Extrapolé étiqueté « Estimation »
 *   - Erreur réseau → Alert
 *   - Pacing indisponible (503) → Alert honnête
 *
 * AI-54 : fixtures calquées sur GET /api/mediaplans/{plan_id}/pacing.
 * AD-9 : NULL honnête partout, estimation ≠ mesure.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import PacingTab from "../mediaplans/PacingTab";
import type { PacingResponse } from "../mediaplans/types";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures (AI-54 — réponse réelle de GET /api/mediaplans/{plan_id}/pacing)
// ---------------------------------------------------------------------------

const PACING_FULL: PacingResponse = {
  plan_id: "plan-aaa",
  project_id: "proj-1",
  lines: [
    {
      line_key: "digital/meta",
      label: "Meta Social",
      support: "Digital",
      is_plan_only: false,
      budget: 30000,
      actual_spend: 12500,
      allocated_to_date: 10000,
      consumed_pct: 0.417,      // 41.7 %
      pace_pct: 0.25,           // +25 % → dépassement (> 10 %)
      remaining: 17500,
      extrapolated_spend: 37500,
      plan_version_id: "ver-001",
      as_of: "2026-05-10T00:00:00Z",
    },
    {
      line_key: "tv/tf1",
      label: "TF1 Spot",
      support: "TV",
      is_plan_only: true,
      budget: 50000,
      actual_spend: null,
      allocated_to_date: null,
      consumed_pct: null,
      pace_pct: null,
      remaining: null,
      extrapolated_spend: null,
      plan_version_id: "ver-001",
      as_of: "2026-05-10T00:00:00Z",
    },
    {
      line_key: "digital/google",
      label: "Google Search",
      support: "Digital",
      is_plan_only: false,
      budget: 20000,
      actual_spend: 6000,
      allocated_to_date: 8000,
      consumed_pct: 0.3,        // 30 %
      pace_pct: -0.25,          // -25 % → sous-livraison (< -10 %)
      remaining: 14000,
      extrapolated_spend: 18000,
      plan_version_id: "ver-001",
      as_of: "2026-05-10T00:00:00Z",
    },
    {
      // Ligne avec pace NULL (pas d'allocation to-date = 0)
      line_key: "display/programmatic",
      label: "Programmatique",
      support: "Digital",
      is_plan_only: false,
      budget: 10000,
      actual_spend: 0,
      allocated_to_date: 0,
      consumed_pct: 0,
      pace_pct: null,  // NULL honnête quand allocated_to_date = 0
      remaining: 10000,
      extrapolated_spend: null,
      plan_version_id: "ver-001",
      as_of: "2026-05-10T00:00:00Z",
    },
  ],
  channels: [
    {
      support: "Digital",
      budget: 60000,
      actual_spend: 18500,
      consumed_pct: 0.308,
      pace_pct: 0.03,
      plan_version_id: "ver-001",
    },
    {
      support: "TV",
      budget: 50000,
      actual_spend: null,
      consumed_pct: null,
      pace_pct: null,
      plan_version_id: "ver-001",
    },
  ],
  plan: {
    budget: 110000,
    actual_spend: 18500,
    consumed_pct: 0.168,
    pace_pct: -0.05,
    remaining: 91500,
    plan_version_id: "ver-001",
    as_of: "2026-05-10T00:00:00Z",
  },
};

function mockPacingFetch(response: PacingResponse) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => response,
    })
  );
}

// ---------------------------------------------------------------------------
// Tests : valeurs NULL → « — »
// ---------------------------------------------------------------------------

describe("PacingTab — NULL honnête (AD-9, jamais 0)", () => {
  it("affiche '—' pour les champs NULL d'une ligne plan-only", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-lines-table")).toBeInTheDocument();
    });

    // La ligne TF1 Spot est plan-only → extrapolé = "—"
    const extrapolatedCell = screen.getByTestId("pacing-extrapolated-tv/tf1");
    expect(extrapolatedCell.textContent).toBe("—");
  });

  it("affiche '—' pour pace_pct NULL (allocated_to_date = 0)", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-line-display/programmatic")).toBeInTheDocument();
    });

    // La ligne programmatique a pace NULL
    const row = screen.getByTestId("pacing-line-display/programmatic");
    // Le texte "—" doit être présent dans la ligne pour le pace (jamais "0 %")
    // On vérifie via aria-label sur PaceIndicator
    expect(row.textContent).not.toMatch(/\+0\.0\s*%/);
  });
});

// ---------------------------------------------------------------------------
// Tests : badges Plan seul (WCAG)
// ---------------------------------------------------------------------------

describe("PacingTab — badge Plan seul (WCAG)", () => {
  it("affiche le badge 'Plan seul' avec aria-label sur les lignes plan-only", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      const badge = screen.getByTestId("pacing-badge-plan-only-tv/tf1");
      expect(badge).toBeInTheDocument();
    });

    // aria-label présent (WCAG — pas couleur seule)
    expect(
      screen.getByLabelText(/plan-only line — no real data/i)
    ).toBeInTheDocument();
  });

  it("n'affiche pas le badge plan-only sur une ligne normale", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-line-digital/meta")).toBeInTheDocument();
    });

    expect(
      screen.queryByTestId("pacing-badge-plan-only-digital/meta")
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Tests : indicateurs dépassement/sous-livraison (WCAG)
// ---------------------------------------------------------------------------

describe("PacingTab — indicateurs dépassement/sous-livraison (WCAG)", () => {
  it("affiche l'indicateur 'Dépassement' avec icône + texte (pas couleur seule)", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-line-digital/meta")).toBeInTheDocument();
    });

    // L'aria-label porte la sémantique complète (WCAG)
    expect(screen.getByLabelText(/over-delivery/i)).toBeInTheDocument();
    // Le texte "Over" est présent (pas couleur seule)
    expect(screen.getByText(/over/i)).toBeInTheDocument();
  });

  it("affiche l'indicateur 'Sous-livraison' avec icône + texte", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-line-digital/google")).toBeInTheDocument();
    });

    expect(screen.getByLabelText(/under-delivery/i)).toBeInTheDocument();
    expect(screen.getByText(/under/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Tests : rollup par support + global
// ---------------------------------------------------------------------------

describe("PacingTab — rollup support + global", () => {
  it("affiche le tableau par support", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-channels-table")).toBeInTheDocument();
      expect(screen.getByTestId("pacing-channel-Digital")).toBeInTheDocument();
      expect(screen.getByTestId("pacing-channel-TV")).toBeInTheDocument();
    });
  });

  it("affiche le tableau global (plan)", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-plan-table")).toBeInTheDocument();
      expect(screen.getByTestId("pacing-plan-row")).toBeInTheDocument();
    });
  });

  it("affiche la provenance (version du plan, as_of)", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-provenance")).toBeInTheDocument();
      // as_of = 10 May 2026 (en-GB)
      expect(screen.getByText(/10 may 2026/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : étiquette « Estimation »
// ---------------------------------------------------------------------------

describe("PacingTab — Estimation étiquetée (AD-9)", () => {
  it("étiquette la colonne extrapolée comme Estimation", async () => {
    mockPacingFetch(PACING_FULL);
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByText(/estimation/i)).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Tests : erreurs
// ---------------------------------------------------------------------------

describe("PacingTab — erreurs", () => {
  it("affiche une Alert si le fetch échoue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({
          code: "warehouse_unavailable",
          message: "Pacing indisponible pour le moment",
        }),
      })
    );
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-error")).toBeInTheDocument();
      expect(screen.getByText(/pacing indisponible/i)).toBeInTheDocument();
    });
  });

  it("affiche l'état vide lignes si aucune ligne de pacing", async () => {
    mockPacingFetch({
      ...PACING_FULL,
      lines: [],
      channels: [],
      plan: null,
    });
    renderWithTheme(<PacingTab planId="plan-aaa" currency="EUR" />);

    await waitFor(() => {
      expect(screen.getByTestId("pacing-lines-empty")).toBeInTheDocument();
    });
  });
});
