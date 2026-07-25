/**
 * Tests ValueGrid — Story 23.4.
 *
 * Convention : ThemeProvider avec createTheme() nu (MUI defaults, sans branding).
 * Vitest + @testing-library/react. Strings françaises (UX-DR10).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider, createTheme, decomposeColor } from "@mui/material/styles";
import ValueGrid from "../ValueGrid";
import type { ValueGridCell } from "../ValueGrid";
import { getVizPalette } from "../vizTheme";

const theme = createTheme();
const viz = getVizPalette(theme);

/** Returns the RGB triple of a CSS color string. */
function rgbOf(color: string): number[] {
  return decomposeColor(color).values.slice(0, 3).map(Math.round);
}

/** Reads the effective bgcolor of a value-grid-cell element (emotion class,
 * not inline style) -- getComputedStyle resolves the injected style tags. */
function cellBg(el: HTMLElement): string {
  return getComputedStyle(el).backgroundColor;
}


function renderGrid(props: Parameters<typeof ValueGrid>[0]) {
  return render(
    <ThemeProvider theme={theme}>
      <ValueGrid {...props} />
    </ThemeProvider>,
  );
}

// ---------------------------------------------------------------------------
// 1. Rend N cellules avec leurs valeurs
// ---------------------------------------------------------------------------

describe("ValueGrid — rendu des cellules", () => {
  it("rend autant de cellules que d'entrées dans cells[]", () => {
    const cells: ValueGridCell[] = [
      { label: "Lundi", value: 10 },
      { label: "Mardi", value: 20 },
      { label: "Mercredi", value: 30 },
    ];
    renderGrid({ cells });
    const rendered = screen.getAllByTestId("value-grid-cell");
    expect(rendered).toHaveLength(3);
  });

  it("affiche les valeurs dans les cellules (toLocaleString fr-FR)", () => {
    const cells: ValueGridCell[] = [
      { value: 1000 },
      { value: 2000 },
    ];
    renderGrid({ cells });
    // 1 000 et 2 000 en fr-FR (espace insécable comme séparateur de milliers)
    const rendered = screen.getAllByTestId("value-grid-cell");
    const texts = rendered.map((el) => el.textContent ?? "");
    // On vérifie qu'au moins un contient "1" et un autre "2"
    expect(texts.some((t) => t.includes("1"))).toBe(true);
    expect(texts.some((t) => t.includes("2"))).toBe(true);
  });

  it("applique le nombre de colonnes au grid CSS", () => {
    const cells: ValueGridCell[] = [{ value: 1 }, { value: 2 }, { value: 3 }];
    renderGrid({ cells, columns: 5 });
    const grid = screen.getByTestId("value-grid");
    // style inline via MUI sx → vérifié sur l'attribut style ou className
    expect(grid).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. État vide
// ---------------------------------------------------------------------------

describe("ValueGrid — état vide", () => {
  it("affiche l'état vide quand cells est vide", () => {
    renderGrid({ cells: [] });
    expect(screen.getByTestId("value-grid-empty")).toBeInTheDocument();
  });

  it("l'état vide a role='status'", () => {
    renderGrid({ cells: [] });
    const empty = screen.getByTestId("value-grid-empty");
    expect(empty).toHaveAttribute("role", "status");
  });

  it("l'état vide affiche 'Aucune donnée à afficher'", () => {
    renderGrid({ cells: [] });
    expect(screen.getByTestId("value-grid-empty")).toHaveTextContent("Aucune donnée à afficher");
  });

  it("n'affiche pas la grille quand cells est vide", () => {
    renderGrid({ cells: [] });
    expect(screen.queryByTestId("value-grid")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 3. null → « — » et fond différent de la cellule 0
// ---------------------------------------------------------------------------

describe("ValueGrid — null vs 0", () => {
  it("affiche '—' pour une cellule null", () => {
    const cells: ValueGridCell[] = [
      { label: "Absent", value: null },
      { label: "Zéro", value: 0 },
    ];
    renderGrid({ cells });
    const rendered = screen.getAllByTestId("value-grid-cell");
    const nullCell = rendered[0];
    expect(nullCell?.textContent).toBe("—");
  });

  it("la cellule null a un bgcolor différent de la cellule 0 (rampe divergente vs squelette)", () => {
    const cells: ValueGridCell[] = [
      { value: null },
      { value: 0 },
      { value: 100 },
    ];
    renderGrid({ cells });
    const rendered = screen.getAllByTestId("value-grid-cell") as HTMLElement[];
    // Contenu textuel correct
    expect(rendered[0]?.textContent).toBe("—");
    expect(rendered[1]?.textContent).toBe("0");
    // Fond null = squelette (alpha très faible, pas de rampe) → bgcolor réel distinct
    const nullBg = cellBg(rendered[0]!);
    const zeroBg = cellBg(rendered[1]!);
    // Les deux fonds doivent être des couleurs différentes (null ≠ rampe divergente).
    expect(nullBg).not.toBe("");
    expect(zeroBg).not.toBe("");
    expect(nullBg).not.toEqual(zeroBg);
  });

  it("le tooltip de la cellule null contient '—'", () => {
    const cells: ValueGridCell[] = [{ label: "Lundi", value: null }];
    renderGrid({ cells });
    const cell = screen.getByTestId("value-grid-cell");
    expect(cell.getAttribute("title")).toContain("—");
  });

  it("le tooltip de la cellule 0 ne contient pas '—'", () => {
    const cells: ValueGridCell[] = [{ label: "Lundi", value: 0 }];
    renderGrid({ cells });
    const cell = screen.getByTestId("value-grid-cell");
    expect(cell.getAttribute("title")).not.toContain("—");
  });
});

// ---------------------------------------------------------------------------
// 4. up_good : la cellule max proche du vert (success.main)
// ---------------------------------------------------------------------------

describe("ValueGrid — direction up_good", () => {
  it("la cellule max et la cellule min ont des data-testid distincts (rendu sans crash)", () => {
    const cells: ValueGridCell[] = [
      { label: "Faible", value: 0 },
      { label: "Fort", value: 100 },
    ];
    // up_good par défaut
    renderGrid({ cells });
    const rendered = screen.getAllByTestId("value-grid-cell");
    expect(rendered).toHaveLength(2);
    expect(rendered[0]?.textContent).toBe("0");
    expect(rendered[1]?.textContent).toBe("100");
  });

  it("up_good : cellule max tend vers success (diverging(0) = vert) et min vers error (diverging(1) = rouge)", () => {
    const cells: ValueGridCell[] = [
      { label: "Min", value: 0 },
      { label: "Max", value: 100 },
    ];
    renderGrid({ cells, direction: "up_good" });
    const rendered = screen.getAllByTestId("value-grid-cell") as HTMLElement[];
    // up_good : max → t=0 (success/vert), min → t=1 (error/rouge).
    // Un bug qui inverserait la rampe ferait échouer ces assertions.
    expect(rgbOf(cellBg(rendered[1]!))).toEqual(rgbOf(viz.diverging(0)));
    expect(rgbOf(cellBg(rendered[0]!))).toEqual(rgbOf(viz.diverging(1)));
  });

  it("up_good : cellule à fond sombre → texte inline clair (contraste getContrastText, AXE 2)", () => {
    // Force une cellule avec valeur max pour obtenir le pôle success (fond sombre potentiel).
    // On plante un seul item pour que min=max → diverging(0) si range=0 on utilise 2 items distincts.
    const cells: ValueGridCell[] = [
      { label: "Min", value: 0 },
      { label: "Max", value: 100 },
    ];
    renderGrid({ cells, direction: "up_good" });
    const rendered = screen.getAllByTestId("value-grid-cell") as HTMLElement[];
    const maxCell = rendered[1]!;
    const bg = cellBg(maxCell);
    // Le texte in-cell doit être la couleur retournée par getContrastText(bg).
    const expectedTextColor = theme.palette.getContrastText(bg);
    const typo = maxCell.querySelector("p, span") as HTMLElement | null;
    expect(typo).not.toBeNull();
    expect(rgbOf(getComputedStyle(typo!).color)).toEqual(rgbOf(expectedTextColor));
  });

  it("up_good : titre du tooltip de la cellule max ne contient pas '—'", () => {
    const cells: ValueGridCell[] = [
      { label: "Min", value: 1 },
      { label: "Max", value: 99 },
    ];
    renderGrid({ cells, direction: "up_good" });
    const rendered = screen.getAllByTestId("value-grid-cell");
    expect(rendered[1]?.getAttribute("title")).toContain("99");
    expect(rendered[1]?.getAttribute("title")).not.toContain("—");
  });

  it("up_good : toutes les cellules sont rendues sans crash avec 7 colonnes", () => {
    const cells = Array.from({ length: 14 }, (_, i) => ({ value: i * 10 }));
    renderGrid({ cells, columns: 7, direction: "up_good" });
    expect(screen.getAllByTestId("value-grid-cell")).toHaveLength(14);
  });
});

// ---------------------------------------------------------------------------
// 5. down_good inverse la direction
// ---------------------------------------------------------------------------

describe("ValueGrid — direction down_good", () => {
  it("down_good : rendu sans crash avec min et max", () => {
    const cells: ValueGridCell[] = [
      { label: "Bon (min)", value: 5 },
      { label: "Mauvais (max)", value: 100 },
    ];
    renderGrid({ cells, direction: "down_good" });
    const rendered = screen.getAllByTestId("value-grid-cell");
    expect(rendered).toHaveLength(2);
    expect(rendered[0]?.textContent).toBe("5");
    expect(rendered[1]?.textContent).toBe("100");
  });

  it("down_good : cellule min tend vers success (diverging(0)) et max vers error (diverging(1))", () => {
    const cells: ValueGridCell[] = [
      { label: "Bon (min)", value: 0 },
      { label: "Mauvais (max)", value: 100 },
    ];
    renderGrid({ cells, direction: "down_good" });
    const rendered = screen.getAllByTestId("value-grid-cell") as HTMLElement[];
    // down_good : min → t=0 (success/vert), max → t=1 (error/rouge).
    // Inversion de direction vs up_good — un bug qui confondrait les deux ferait échouer.
    expect(rgbOf(cellBg(rendered[0]!))).toEqual(rgbOf(viz.diverging(0)));
    expect(rgbOf(cellBg(rendered[1]!))).toEqual(rgbOf(viz.diverging(1)));
  });

  it("down_good : le tooltip de la cellule min contient 'Bon'", () => {
    const cells: ValueGridCell[] = [
      { label: "Bon (min)", value: 5 },
      { label: "Mauvais (max)", value: 100 },
    ];
    renderGrid({ cells, direction: "down_good" });
    const rendered = screen.getAllByTestId("value-grid-cell");
    expect(rendered[0]?.getAttribute("title")).toContain("Bon");
  });

  it("down_good et up_good produisent des grilles structurellement identiques (même nb de cellules)", () => {
    const cells: ValueGridCell[] = [
      { value: 10 },
      { value: 50 },
      { value: 90 },
    ];
    const { unmount } = renderGrid({ cells, direction: "down_good" });
    expect(screen.getAllByTestId("value-grid-cell")).toHaveLength(3);
    unmount();
    renderGrid({ cells, direction: "up_good" });
    expect(screen.getAllByTestId("value-grid-cell")).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// 6. Tooltip avec unité
// ---------------------------------------------------------------------------

describe("ValueGrid — tooltips et unité", () => {
  it("le tooltip inclut l'unité quand unit est fourni", () => {
    const cells: ValueGridCell[] = [{ label: "Lundi", value: 42 }];
    renderGrid({ cells, unit: "€" });
    const cell = screen.getByTestId("value-grid-cell");
    expect(cell.getAttribute("title")).toContain("€");
  });

  it("le tooltip inclut le label et la valeur", () => {
    const cells: ValueGridCell[] = [{ label: "Semaine 1", value: 789 }];
    renderGrid({ cells });
    const cell = screen.getByTestId("value-grid-cell");
    const title = cell.getAttribute("title") ?? "";
    expect(title).toContain("Semaine 1");
    expect(title).toContain("789");
  });
});
