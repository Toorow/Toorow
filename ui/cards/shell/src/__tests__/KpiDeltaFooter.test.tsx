/**
 * Tests KpiDeltaFooter (Story 23.5).
 *
 * Conventions :
 *   - vitest + @testing-library/react
 *   - ThemeProvider + createTheme() nu (test isolation complète, pas de branding)
 *   - AD-9 : delta_pct null → aucune flèche, affiche « — »
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider, createTheme, decomposeColor } from "@mui/material/styles";
import KpiDeltaFooter from "../KpiDeltaFooter";
import type { KpiDeltaItem } from "../KpiDeltaFooter";

const theme = createTheme();

/** Returns the RGB triple of a CSS color string. */
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

const ITEMS: KpiDeltaItem[] = [
  { label: "Monthly", value: 8097, delta_pct: 19.6, caption: "44.214 USD" },
  { label: "Yearly", value: 97164, delta_pct: -3.2, caption: "100.521 USD" },
];

// ---------------------------------------------------------------------------
// 1. Rendu des items (label, valeur héros, caption)
// ---------------------------------------------------------------------------

describe("KpiDeltaFooter — rendu de base", () => {
  it("rend le bon nombre de blocs KPI", () => {
    wrap(<KpiDeltaFooter items={ITEMS} />);
    expect(screen.getAllByTestId("kpi-delta-footer-item")).toHaveLength(2);
  });

  it("rend les labels", () => {
    wrap(<KpiDeltaFooter items={ITEMS} />);
    expect(screen.getByText("Monthly")).toBeInTheDocument();
    expect(screen.getByText("Yearly")).toBeInTheDocument();
  });

  it("formate la valeur numérique héros en fr-FR", () => {
    wrap(<KpiDeltaFooter items={ITEMS} />);
    const values = screen.getAllByTestId("kpi-delta-footer-value");
    // 8097 → "8 097" en fr-FR
    expect(values[0]!.textContent).toMatch(/8.?097/);
  });

  it("rend une valeur string héros telle quelle", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "ROAS", value: "$8,097", delta_pct: 5.0 }]}
      />,
    );
    const value = screen.getByTestId("kpi-delta-footer-value");
    expect(value.textContent).toBe("$8,097");
  });

  it("rend la caption discrète sous le delta", () => {
    wrap(<KpiDeltaFooter items={ITEMS} />);
    const captions = screen.getAllByTestId("kpi-delta-footer-caption");
    expect(captions[0]!.textContent).toBe("44.214 USD");
    expect(captions[1]!.textContent).toBe("100.521 USD");
  });

  it("value null → héros affiche « — »", () => {
    wrap(
      <KpiDeltaFooter items={[{ label: "Revenu", value: null, delta_pct: 5.0 }]} />,
    );
    const value = screen.getByTestId("kpi-delta-footer-value");
    expect(value.textContent).toBe("—");
  });

  it("rend le data-testid kpi-delta-footer sur le conteneur", () => {
    wrap(<KpiDeltaFooter items={ITEMS} />);
    expect(screen.getByTestId("kpi-delta-footer")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. delta_pct positif up_good → success + ↑
// ---------------------------------------------------------------------------

describe("KpiDeltaFooter — semanticDirection up_good (défaut)", () => {
  it("delta_pct positif → flèche ↑ et texte du pourcentage affiché", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "Monthly", value: 8097, delta_pct: 19.6 }]}
        semanticDirection="up_good"
      />,
    );
    const delta = screen.getByTestId("kpi-delta-footer-delta");
    expect(delta.textContent).toContain("↑");
    // 19.6 → "19,6 %" en fr-FR
    expect(delta.textContent).toMatch(/19[,.]6/);
  });

  it("delta_pct négatif up_good → flèche ↓", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "Yearly", value: 97164, delta_pct: -3.2 }]}
        semanticDirection="up_good"
      />,
    );
    const delta = screen.getByTestId("kpi-delta-footer-delta");
    expect(delta.textContent).toContain("↓");
    expect(delta.textContent).toMatch(/3[,.]2/);
  });

  it("delta_pct positif up_good → couleur inline = success.main (AD-9)", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "Monthly", value: 8097, delta_pct: 19.6 }]}
        semanticDirection="up_good"
      />,
    );
    const delta = screen.getByTestId("kpi-delta-footer-delta") as HTMLElement;
    expect(delta.textContent).toContain("+");
    // sx={{ color: success.main }} injecté en style inline par MUI/jsdom.
    // Un swap success/error dans resolveColor ferait échouer cette assertion.
    expect(inlineRgb(delta)).toEqual(rgbOf(theme.palette.success.main));
  });
});

// ---------------------------------------------------------------------------
// 3. down_good inverse les couleurs
// ---------------------------------------------------------------------------

describe("KpiDeltaFooter — semanticDirection down_good", () => {
  it("delta_pct positif down_good → ↑ error (monter est mauvais : couleur inline = error.main)", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "CPA", value: 45, delta_pct: 8.3 }]}
        semanticDirection="down_good"
      />,
    );
    const delta = screen.getByTestId("kpi-delta-footer-delta") as HTMLElement;
    expect(delta.textContent).toContain("↑");
    // down_good + positif = mauvais → error. Un swap success/error casse ici.
    expect(inlineRgb(delta)).toEqual(rgbOf(theme.palette.error.main));
  });

  it("delta_pct négatif down_good → ↓ success (baisser est bon : couleur inline = success.main)", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "CPA", value: 28, delta_pct: -12.5 }]}
        semanticDirection="down_good"
      />,
    );
    const delta = screen.getByTestId("kpi-delta-footer-delta") as HTMLElement;
    expect(delta.textContent).toContain("↓");
    expect(inlineRgb(delta)).toEqual(rgbOf(theme.palette.success.main));
  });
});

// ---------------------------------------------------------------------------
// 4. delta_pct null → « — » sans flèche (AD-9)
// ---------------------------------------------------------------------------

describe("KpiDeltaFooter — delta_pct null (AD-9)", () => {
  it("delta_pct null → affiche « — » et aucune flèche", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "Revenue", value: 5000, delta_pct: null }]}
      />,
    );
    expect(screen.queryByTestId("kpi-delta-footer-delta")).toBeNull();
    const nullEl = screen.getByTestId("kpi-delta-footer-delta-null");
    expect(nullEl).toBeInTheDocument();
    expect(nullEl.textContent).toBe("—");
  });

  it("delta_pct absent (undefined) → même comportement que null", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "Sessions", value: 1000 }]}
      />,
    );
    expect(screen.queryByTestId("kpi-delta-footer-delta")).toBeNull();
    expect(screen.getByTestId("kpi-delta-footer-delta-null")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 5. items vide → ne rend rien (container.firstChild null)
// ---------------------------------------------------------------------------

describe("KpiDeltaFooter — état vide", () => {
  it("items vide → rend null (container.firstChild est null)", () => {
    const { container } = wrap(<KpiDeltaFooter items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("items vide → aucun bloc item rendu", () => {
    wrap(<KpiDeltaFooter items={[]} />);
    expect(screen.queryByTestId("kpi-delta-footer-item")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 6. neutral → flèches en text.secondary (pas success/error)
// ---------------------------------------------------------------------------

describe("KpiDeltaFooter — semanticDirection neutral", () => {
  it("delta_pct positif neutral → ↑ présent, pas de coloration success/error", () => {
    wrap(
      <KpiDeltaFooter
        items={[{ label: "Vues", value: 3000, delta_pct: 5.0 }]}
        semanticDirection="neutral"
      />,
    );
    const delta = screen.getByTestId("kpi-delta-footer-delta");
    expect(delta.textContent).toContain("↑");
    expect(delta).toBeInTheDocument();
  });
});
