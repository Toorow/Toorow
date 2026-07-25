/**
 * Tests RankedList (Story 23.5).
 *
 * Conventions :
 *   - vitest + @testing-library/react
 *   - ThemeProvider + createTheme() nu (test isolation complète, pas de branding)
 *   - AD-9 : delta null → jamais de triangle, affiche « — »
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider, createTheme, decomposeColor } from "@mui/material/styles";
import RankedList from "../RankedList";
import type { RankedListEntry } from "../RankedList";

const theme = createTheme();

/** Returns the RGB triple of a CSS color string (rounds to nearest integer). */
function rgbOf(color: string): number[] {
  return decomposeColor(color).values.slice(0, 3).map(Math.round);
}

/** Reads the effective `color` of an element (emotion class, not inline style)
 * as an RGB triple -- getComputedStyle resolves the injected style tags. */
function inlineRgb(el: HTMLElement): number[] {
  return rgbOf(getComputedStyle(el).color);
}

function wrap(ui: React.ReactElement) {
  return render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const ENTRIES: RankedListEntry[] = [
  { label: "Travel", value: 2540, secondary: 760, delta: 120 },
  { label: "Presentation", value: 2304, secondary: 650, delta: -88 },
  { label: "Education", value: 1980, secondary: 540, delta: 0 },
  { label: "Finance", value: 1200, secondary: 310, delta: null },
];

// ---------------------------------------------------------------------------
// 1. Rendu des lignes avec labels et valeurs fr-FR
// ---------------------------------------------------------------------------

describe("RankedList — rendu de base", () => {
  it("rend toutes les lignes avec leurs labels", () => {
    wrap(<RankedList entries={ENTRIES} />);
    expect(screen.getAllByTestId("ranked-list-row")).toHaveLength(4);
    expect(screen.getByText("Travel")).toBeInTheDocument();
    expect(screen.getByText("Presentation")).toBeInTheDocument();
    expect(screen.getByText("Education")).toBeInTheDocument();
    expect(screen.getByText("Finance")).toBeInTheDocument();
  });

  it("formate les valeurs principales en fr-FR (séparateur espace fine / espace)", () => {
    wrap(<RankedList entries={ENTRIES} />);
    const values = screen.getAllByTestId("ranked-list-value");
    // 2540 → "2 540" en fr-FR (espace insécable ou fine)
    expect(values[0]!.textContent).toMatch(/2.?540/);
    expect(values[1]!.textContent).toMatch(/2.?304/);
  });

  it("affiche les valeurs secondaires quand fournies", () => {
    wrap(<RankedList entries={ENTRIES} />);
    const secondaries = screen.getAllByTestId("ranked-list-secondary");
    expect(secondaries.length).toBeGreaterThanOrEqual(3);
    expect(secondaries[0]!.textContent).toMatch(/760/);
  });

  it("affiche l'unité dans les valeurs quand unit fournie", () => {
    wrap(<RankedList entries={[{ label: "Alpha", value: 1500, delta: 10 }]} unit="€" />);
    const val = screen.getByTestId("ranked-list-value");
    expect(val.textContent).toContain("€");
  });
});

// ---------------------------------------------------------------------------
// 2. Couleurs sémantiques up_good
// ---------------------------------------------------------------------------

describe("RankedList — semanticDirection up_good (défaut)", () => {
  it("delta positif up_good → ▲ success (AD-9 : couleur inline = success.main)", () => {
    wrap(
      <RankedList
        entries={[{ label: "A", value: 100, delta: 10 }]}
        semanticDirection="up_good"
      />,
    );
    const symbol = screen.getByTestId("ranked-list-delta-symbol") as HTMLElement;
    expect(symbol.textContent).toBe("▲");
    // sx={{ color: theme.palette.success.main }} est injecté en style inline par MUI/jsdom.
    // Un swap success/error dans resolveColor ferait échouer cette assertion.
    expect(inlineRgb(symbol)).toEqual(rgbOf(theme.palette.success.main));
  });

  it("delta négatif up_good → ▼ error (couleur inline = error.main)", () => {
    wrap(
      <RankedList
        entries={[{ label: "B", value: 50, delta: -5 }]}
        semanticDirection="up_good"
      />,
    );
    const symbol = screen.getByTestId("ranked-list-delta-symbol") as HTMLElement;
    expect(symbol.textContent).toBe("▼");
    expect(inlineRgb(symbol)).toEqual(rgbOf(theme.palette.error.main));
  });

  it("delta positif up_good → aria-label contient « en hausse »", () => {
    wrap(
      <RankedList
        entries={[{ label: "Sessions", value: 2000, delta: 100 }]}
        semanticDirection="up_good"
      />,
    );
    const row = screen.getByTestId("ranked-list-row");
    expect(row.getAttribute("aria-label")).toContain("en hausse");
  });

  it("delta négatif up_good → aria-label contient « en baisse »", () => {
    wrap(
      <RankedList
        entries={[{ label: "Sessions", value: 1800, delta: -50 }]}
        semanticDirection="up_good"
      />,
    );
    const row = screen.getByTestId("ranked-list-row");
    expect(row.getAttribute("aria-label")).toContain("en baisse");
  });
});

// ---------------------------------------------------------------------------
// 3. down_good inverse les couleurs (ex. CPA, position)
// ---------------------------------------------------------------------------

describe("RankedList — semanticDirection down_good", () => {
  it("delta positif down_good → ▲ error (monter est mauvais : couleur inline = error.main)", () => {
    wrap(
      <RankedList
        entries={[{ label: "CPA", value: 35, delta: 5 }]}
        semanticDirection="down_good"
      />,
    );
    const symbol = screen.getByTestId("ranked-list-delta-symbol") as HTMLElement;
    expect(symbol.textContent).toBe("▲");
    // down_good + positif = mauvais → error. Un swap success/error casse ici.
    expect(inlineRgb(symbol)).toEqual(rgbOf(theme.palette.error.main));
  });

  it("delta négatif down_good → ▼ success (baisser est bon : couleur inline = success.main)", () => {
    wrap(
      <RankedList
        entries={[{ label: "CPA", value: 28, delta: -3 }]}
        semanticDirection="down_good"
      />,
    );
    const symbol = screen.getByTestId("ranked-list-delta-symbol") as HTMLElement;
    expect(symbol.textContent).toBe("▼");
    expect(inlineRgb(symbol)).toEqual(rgbOf(theme.palette.success.main));
  });
});

// ---------------------------------------------------------------------------
// 4. delta null → « — », aucun triangle (AD-9)
// ---------------------------------------------------------------------------

describe("RankedList — delta null (AD-9)", () => {
  it("delta null → affiche « — » discret et aucun triangle", () => {
    wrap(
      <RankedList
        entries={[{ label: "Finance", value: 1200, delta: null }]}
      />,
    );
    // Pas de symbole ▲/▼
    expect(screen.queryByTestId("ranked-list-delta-symbol")).toBeNull();
    // Le marqueur null est présent
    expect(screen.getByTestId("ranked-list-delta-null")).toBeInTheDocument();
    expect(screen.getByTestId("ranked-list-delta-null").textContent).toBe("—");
  });

  it("delta absent (undefined) → même comportement que null", () => {
    wrap(
      <RankedList
        entries={[{ label: "NoData", value: 500 }]}
      />,
    );
    expect(screen.queryByTestId("ranked-list-delta-symbol")).toBeNull();
    expect(screen.getByTestId("ranked-list-delta-null")).toBeInTheDocument();
  });

  it("delta null → aria-label contient « — »", () => {
    wrap(
      <RankedList
        entries={[{ label: "Finance", value: 1200, delta: null }]}
      />,
    );
    const row = screen.getByTestId("ranked-list-row");
    expect(row.getAttribute("aria-label")).toContain("—");
  });

  it("delta = 0 → symbole « = » en text.secondary (pas de fausse tendance)", () => {
    wrap(
      <RankedList
        entries={[{ label: "Stable", value: 1000, delta: 0 }]}
      />,
    );
    const symbol = screen.getByTestId("ranked-list-delta-symbol");
    expect(symbol.textContent).toBe("=");
  });
});

// ---------------------------------------------------------------------------
// 5. maxRows tronque
// ---------------------------------------------------------------------------

describe("RankedList — maxRows", () => {
  it("tronque les entrées au nombre demandé", () => {
    wrap(<RankedList entries={ENTRIES} maxRows={2} />);
    expect(screen.getAllByTestId("ranked-list-row")).toHaveLength(2);
    expect(screen.getByText("Travel")).toBeInTheDocument();
    expect(screen.getByText("Presentation")).toBeInTheDocument();
    expect(screen.queryByText("Education")).toBeNull();
  });

  it("maxRows >= length → affiche tout", () => {
    wrap(<RankedList entries={ENTRIES} maxRows={10} />);
    expect(screen.getAllByTestId("ranked-list-row")).toHaveLength(4);
  });
});

// ---------------------------------------------------------------------------
// 6. État vide designé
// ---------------------------------------------------------------------------

describe("RankedList — état vide", () => {
  it("rend l'état vide quand entries est vide", () => {
    wrap(<RankedList entries={[]} />);
    const empty = screen.getByTestId("ranked-list-empty");
    expect(empty).toBeInTheDocument();
    expect(empty).toHaveAttribute("role", "status");
    expect(empty.textContent).toContain("Aucune donnée à afficher");
  });

  it("ne rend pas de lignes quand l'état vide est actif", () => {
    wrap(<RankedList entries={[]} />);
    expect(screen.queryByTestId("ranked-list-row")).toBeNull();
  });
});
