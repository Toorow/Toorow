/**
 * Tests DotMatrix — Story 23.4.
 *
 * Convention : ThemeProvider avec createTheme() nu (MUI defaults, sans branding).
 * Vitest + @testing-library/react. Strings françaises (UX-DR10).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider, createTheme } from "@mui/material/styles";
import DotMatrix from "../DotMatrix";
import type { DotMatrixGroup } from "../DotMatrix";

const theme = createTheme();

function renderMatrix(props: Parameters<typeof DotMatrix>[0]) {
  return render(
    <ThemeProvider theme={theme}>
      <DotMatrix {...props} />
    </ThemeProvider>,
  );
}

// ---------------------------------------------------------------------------
// 1. Bon nombre de points par groupe
// ---------------------------------------------------------------------------

describe("DotMatrix — nombre de points", () => {
  it("rend autant de points que de valeurs dans le groupe", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Septembre", values: [10, 20, 30, null, 50] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    expect(dots).toHaveLength(5);
  });

  it("rend le total de points sur plusieurs groupes", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Octobre", values: [1, 2, 3] },
      { label: "Novembre", values: [4, 5, 6, 7] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    expect(dots).toHaveLength(7); // 3 + 4
  });

  it("rend exactement columns points par ligne (data-testid présents même en wrap)", () => {
    const values = Array.from({ length: 40 }, (_, i) => i);
    const groups: DotMatrixGroup[] = [{ label: "Jan", values }];
    renderMatrix({ groups, columns: 10 });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    expect(dots).toHaveLength(40);
  });
});

// ---------------------------------------------------------------------------
// 2. État vide
// ---------------------------------------------------------------------------

describe("DotMatrix — état vide", () => {
  it("affiche l'état vide quand groups est vide", () => {
    renderMatrix({ groups: [] });
    expect(screen.getByTestId("dot-matrix-empty")).toBeInTheDocument();
  });

  it("l'état vide a role='status'", () => {
    renderMatrix({ groups: [] });
    expect(screen.getByTestId("dot-matrix-empty")).toHaveAttribute("role", "status");
  });

  it("affiche 'Aucune donnée à afficher' dans l'état vide", () => {
    renderMatrix({ groups: [] });
    expect(screen.getByTestId("dot-matrix-empty")).toHaveTextContent("Aucune donnée à afficher");
  });

  it("n'affiche pas la matrice quand groups est vide", () => {
    renderMatrix({ groups: [] });
    expect(screen.queryByTestId("dot-matrix")).not.toBeInTheDocument();
  });

  it("affiche l'état vide quand tous les groupes ont values=[]", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Vide", values: [] },
    ];
    renderMatrix({ groups });
    expect(screen.getByTestId("dot-matrix-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 3. Totaux et labels affichés
// ---------------------------------------------------------------------------

describe("DotMatrix — totaux et labels", () => {
  it("affiche le label de chaque groupe", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Septembre", values: [1, 2] },
      { label: "Octobre", values: [3, 4] },
    ];
    renderMatrix({ groups });
    // Le label apparaît dans le header du groupe ET dans les <title>/aria des
    // points — getAllByText évite l'ambiguïté multi-nœuds.
    expect(screen.getAllByText("Septembre").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Octobre").length).toBeGreaterThanOrEqual(1);
  });

  it("affiche le total string tel quel", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Septembre", values: [1], total: "$32,134" },
    ];
    renderMatrix({ groups });
    expect(screen.getByTestId("dot-matrix-total")).toHaveTextContent("$32,134");
  });

  it("formate le total number en fr-FR", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Octobre", values: [1], total: 32134 },
    ];
    renderMatrix({ groups });
    const totalEl = screen.getByTestId("dot-matrix-total");
    // 32 134 en fr-FR (espace insécable ou espace normal selon l'environnement)
    expect(totalEl.textContent).toMatch(/32/);
    expect(totalEl.textContent).toMatch(/134/);
  });

  it("inclut l'unité dans le total formaté si unit est fourni", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Nov", values: [1], total: 1000 },
    ];
    renderMatrix({ groups, unit: "€" });
    const totalEl = screen.getByTestId("dot-matrix-total");
    expect(totalEl.textContent).toContain("€");
  });

  it("n'affiche pas de total si total est absent", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Décembre", values: [1, 2] },
    ];
    renderMatrix({ groups });
    expect(screen.queryByTestId("dot-matrix-total")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 4. null vs 0 : opacités/couleurs distinctes
// ---------------------------------------------------------------------------

describe("DotMatrix — null vs 0", () => {
  it("le point null et le point 0 ont des data-alpha distincts", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Test", values: [null, 0, 100] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    const alphaNullStr = dots[0]?.getAttribute("data-alpha") ?? "0";
    const alphaZeroStr = dots[1]?.getAttribute("data-alpha") ?? "0";
    const alphaNullVal = parseFloat(alphaNullStr);
    const alphaZeroVal = parseFloat(alphaZeroStr);
    // null < 0 en alpha (null = éteint)
    expect(alphaNullVal).toBeLessThan(alphaZeroVal);
  });

  it("le tooltip du point null contient '—'", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Groupe", values: [null] },
    ];
    renderMatrix({ groups });
    const dot = screen.getByTestId("dot-matrix-dot");
    // Le <title> est l'enfant text du <circle>
    expect(dot.querySelector("title")?.textContent).toContain("—");
  });

  it("le tooltip du point 0 contient '0' et pas '—'", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Groupe", values: [0] },
    ];
    renderMatrix({ groups });
    const dot = screen.getByTestId("dot-matrix-dot");
    const titleText = dot.querySelector("title")?.textContent ?? "";
    expect(titleText).toContain("0");
    expect(titleText).not.toContain("—");
  });

  it("le point 0 a un alpha > ALPHA_NULL (0.04)", () => {
    const groups: DotMatrixGroup[] = [
      { label: "G", values: [0, 100] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    const alphaZero = parseFloat(dots[0]?.getAttribute("data-alpha") ?? "0");
    expect(alphaZero).toBeGreaterThan(0.04);
  });
});

// ---------------------------------------------------------------------------
// 5. Normalisation sur le max global (comparabilité inter-groupes)
// ---------------------------------------------------------------------------

describe("DotMatrix — normalisation max global", () => {
  it("le point max d'un groupe faible n'est pas au max d'alpha (normalisé globalement)", () => {
    // Groupe A : max local = 10. Groupe B : max global = 100.
    // Le point 10 du groupe A ne doit PAS avoir alpha=1.0 car le max global est 100.
    const groups: DotMatrixGroup[] = [
      { label: "Faible", values: [10] },       // max local = 10
      { label: "Fort", values: [100] },         // max global = 100
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    // Premier point = valeur 10/100 = 0.10 de normalisation globale
    // alpha = ALPHA_MIN + 0.10 * (1 - ALPHA_MIN) = 0.10 + 0.10 * 0.90 = 0.19
    const alphaFaible = parseFloat(dots[0]?.getAttribute("data-alpha") ?? "1");
    // Il doit être nettement inférieur à 1 (pas au max global)
    expect(alphaFaible).toBeLessThan(0.5);
    // Et le point 100 doit avoir alpha proche de 1
    const alpheFort = parseFloat(dots[1]?.getAttribute("data-alpha") ?? "0");
    expect(alpheFort).toBeCloseTo(1.0, 2);
  });

  it("le max global est le point le plus opaque de tous les groupes", () => {
    const groups: DotMatrixGroup[] = [
      { label: "G1", values: [5, 15, 30] },
      { label: "G2", values: [10, 50, 20] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    const alphas = dots.map((d) => parseFloat(d.getAttribute("data-alpha") ?? "0"));
    const maxAlpha = Math.max(...alphas);
    expect(maxAlpha).toBeCloseTo(1.0, 2);
    // Un seul point doit avoir alpha ≈ 1 : celui de valeur 50 (max global)
    const maxIdx = alphas.indexOf(maxAlpha);
    // G1 a 3 points (idx 0,1,2), G2 a 3 points (idx 3,4,5) → idx 4 = valeur 50
    expect(maxIdx).toBe(4);
  });

  it("quand tous les groupes ont la même valeur max, tous les points max ont alpha=1", () => {
    const groups: DotMatrixGroup[] = [
      { label: "G1", values: [100] },
      { label: "G2", values: [100] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    const alphas = dots.map((d) => parseFloat(d.getAttribute("data-alpha") ?? "0"));
    expect(alphas[0]).toBeCloseTo(1.0, 2);
    expect(alphas[1]).toBeCloseTo(1.0, 2);
  });

  it("quand le max global est 0, tous les points non-null ont alpha = ALPHA_MIN (0.10)", () => {
    const groups: DotMatrixGroup[] = [
      { label: "G", values: [0, 0, 0] },
    ];
    renderMatrix({ groups });
    const dots = screen.getAllByTestId("dot-matrix-dot");
    const alphas = dots.map((d) => parseFloat(d.getAttribute("data-alpha") ?? "0"));
    alphas.forEach((a) => expect(a).toBeCloseTo(0.10, 2));
  });
});

// ---------------------------------------------------------------------------
// 6. Accessibilité
// ---------------------------------------------------------------------------

describe("DotMatrix — accessibilité", () => {
  it("le SVG de chaque groupe a role='img'", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Sept", values: [1, 2] },
    ];
    renderMatrix({ groups });
    const svgs = document.querySelectorAll("svg[role='img']");
    expect(svgs.length).toBeGreaterThan(0);
  });

  it("le SVG a un aria-label incluant le label du groupe", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Novembre", values: [10], total: 100 },
    ];
    renderMatrix({ groups });
    const svg = document.querySelector("svg[role='img']");
    expect(svg?.getAttribute("aria-label")).toContain("Novembre");
  });

  it("le SVG a un aria-label incluant le total quand fourni", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Décembre", values: [5], total: "$5,000" },
    ];
    renderMatrix({ groups });
    const svg = document.querySelector("svg[role='img']");
    expect(svg?.getAttribute("aria-label")).toContain("$5,000");
  });

  it("le tooltip SVG <title> du groupe contient le label", () => {
    const groups: DotMatrixGroup[] = [
      { label: "Octobre", values: [42] },
    ];
    renderMatrix({ groups });
    const svgTitle = document.querySelector("svg[role='img'] > title");
    expect(svgTitle?.textContent).toContain("Octobre");
  });
});
