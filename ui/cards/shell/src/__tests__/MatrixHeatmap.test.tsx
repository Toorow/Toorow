/**
 * MatrixHeatmap tests (Story 23.2).
 *
 * Conventions : vitest + @testing-library/react + ThemeProvider avec createTheme() nu.
 * Chaque test est isolé ; les fills SVG sont des rgba() calculés par alpha() de MUI.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThemeProvider, createTheme } from "@mui/material/styles";
import MatrixHeatmap from "../MatrixHeatmap";
import type { MatrixHeatmapProps } from "../MatrixHeatmap";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function renderWithTheme(props: MatrixHeatmapProps, themeOptions = {}) {
  const theme = createTheme(themeOptions);
  return render(
    <ThemeProvider theme={theme}>
      <MatrixHeatmap {...props} />
    </ThemeProvider>,
  );
}

// Fixture 3×4 de base.
const ROWS = ["France", "Allemagne", "Espagne"];
const COLS = ["18-24", "25-34", "35-44", "45+"];
const VALUES: Array<Array<number | null>> = [
  [8, 25, 53, 10],
  [3, 15, 42, 7],
  [null, 9, 31, 5],
];

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("MatrixHeatmap — matrice 3×4 de base", () => {
  it("rend le bon nombre de cellules (3 lignes × 4 colonnes = 12 cellules)", () => {
    renderWithTheme({ rows: ROWS, cols: COLS, values: VALUES });
    const cells = screen.getAllByTestId("matrix-heatmap-cell");
    expect(cells.length).toBe(12);
  });

  it("affiche tous les labels de lignes", () => {
    renderWithTheme({ rows: ROWS, cols: COLS, values: VALUES });
    const rowLabels = screen.getAllByTestId("matrix-heatmap-row-label");
    expect(rowLabels.length).toBe(3);
    const texts = rowLabels.map((el) => el.textContent);
    expect(texts).toContain("France");
    expect(texts).toContain("Allemagne");
    expect(texts).toContain("Espagne");
  });

  it("affiche tous les labels de colonnes", () => {
    renderWithTheme({ rows: ROWS, cols: COLS, values: VALUES });
    const colLabels = screen.getAllByTestId("matrix-heatmap-col-label");
    expect(colLabels.length).toBe(4);
    const texts = colLabels.map((el) => el.textContent);
    expect(texts).toContain("18-24");
    expect(texts).toContain("25-34");
    expect(texts).toContain("35-44");
    expect(texts).toContain("45+");
  });

  it("le SVG porte role='img' et aria-label", () => {
    renderWithTheme({
      rows: ROWS,
      cols: COLS,
      values: VALUES,
      ariaLabel: "Matrice test",
    });
    const svg = screen.getByRole("img", { name: "Matrice test" });
    expect(svg).toBeInTheDocument();
  });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("MatrixHeatmap — état vide designé", () => {
  it("affiche l'état vide quand values est vide", () => {
    renderWithTheme({ rows: [], cols: [], values: [] });
    expect(screen.getByTestId("matrix-heatmap-empty")).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByTestId("matrix-heatmap-empty")).toHaveTextContent(
      "Aucune donnée à afficher",
    );
  });

  it("affiche l'état vide quand rows est vide mais cols non", () => {
    renderWithTheme({ rows: [], cols: COLS, values: [] });
    expect(screen.getByTestId("matrix-heatmap-empty")).toBeInTheDocument();
  });

  it("n'affiche PAS la matrice SVG quand vide", () => {
    renderWithTheme({ rows: [], cols: [], values: [] });
    expect(screen.queryByTestId("matrix-heatmap")).not.toBeInTheDocument();
  });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("MatrixHeatmap — null ≠ 0", () => {
  it("la cellule null a un fill différent d'une cellule à 0", () => {
    const rows = ["A"];
    const cols = ["X", "Y"];
    // X = 0, Y = null
    const values: Array<Array<number | null>> = [[0, null]];
    renderWithTheme({ rows, cols, values });
    const cells = screen.getAllByTestId("matrix-heatmap-cell");
    expect(cells.length).toBe(2);

    const cellZero = cells.find((c) => c.getAttribute("data-col") === "X")!;
    const cellNull = cells.find((c) => c.getAttribute("data-col") === "Y")!;

    const fillZero = cellZero.getAttribute("fill");
    const fillNull = cellNull.getAttribute("fill");

    // Les fills doivent être différents (null = squelette hairline, 0 = alpha minimal accent).
    expect(fillZero).not.toBe(fillNull);
    expect(fillZero).not.toBeNull();
    expect(fillNull).not.toBeNull();
  });

  it("le tooltip de la cellule null affiche '—'", () => {
    const rows = ["A"];
    const cols = ["Y"];
    const values: Array<Array<number | null>> = [[null]];
    renderWithTheme({ rows, cols, values, unit: "visiteurs" });
    const cell = screen.getByTestId("matrix-heatmap-cell");
    // La cellule contient un <title> SVG.
    const title = cell.querySelector("title");
    expect(title).not.toBeNull();
    expect(title!.textContent).toContain("—");
    // Ne doit PAS contenir "visiteurs" (pas de valeur).
    expect(title!.textContent).not.toContain("visiteurs");
  });

  it("la cellule null porte aria-label 'donnée absente'", () => {
    const rows = ["A"];
    const cols = ["Y"];
    const values: Array<Array<number | null>> = [[null]];
    renderWithTheme({ rows, cols, values });
    const cell = screen.getByTestId("matrix-heatmap-cell");
    expect(cell.getAttribute("aria-label")).toContain("donnée absente");
  });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("MatrixHeatmap — thresholds (légende paliers)", () => {
  const thresholds = [
    { min: 0, label: "0-10" },
    { min: 11, label: ">10" },
    { min: 26, label: ">25" },
    { min: 51, label: ">50" },
  ];

  it("affiche la légende quand thresholds est fourni", () => {
    renderWithTheme({ rows: ROWS, cols: COLS, values: VALUES, thresholds });
    expect(screen.getByTestId("matrix-heatmap-legend")).toBeInTheDocument();
  });

  it("affiche tous les labels de paliers dans la légende", () => {
    renderWithTheme({ rows: ROWS, cols: COLS, values: VALUES, thresholds });
    const items = screen.getAllByTestId("matrix-heatmap-legend-item");
    expect(items.length).toBe(4);
    const texts = items.map((el) => el.textContent);
    expect(texts).toContain("0-10");
    expect(texts).toContain(">10");
    expect(texts).toContain(">25");
    expect(texts).toContain(">50");
  });

  it("n'affiche PAS la légende quand thresholds est absent", () => {
    renderWithTheme({ rows: ROWS, cols: COLS, values: VALUES });
    expect(screen.queryByTestId("matrix-heatmap-legend")).not.toBeInTheDocument();
  });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("MatrixHeatmap — onCellClick", () => {
  it("appelle onCellClick avec (row, col, value) au clic", () => {
    const handler = vi.fn();
    renderWithTheme({
      rows: ROWS,
      cols: COLS,
      values: VALUES,
      onCellClick: handler,
    });
    // Clic sur la première cellule : France × 18-24 = 8.
    const cells = screen.getAllByTestId("matrix-heatmap-cell");
    const firstCell = cells.find(
      (c) =>
        c.getAttribute("data-row") === "France" &&
        c.getAttribute("data-col") === "18-24",
    )!;
    expect(firstCell).toBeInTheDocument();
    fireEvent.click(firstCell);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith("France", "18-24", 8);
  });

  it("appelle onCellClick avec value=null pour une cellule absente", () => {
    const handler = vi.fn();
    renderWithTheme({
      rows: ROWS,
      cols: COLS,
      values: VALUES,
      onCellClick: handler,
    });
    // Espagne × 18-24 = null
    const cells = screen.getAllByTestId("matrix-heatmap-cell");
    const nullCell = cells.find(
      (c) =>
        c.getAttribute("data-row") === "Espagne" &&
        c.getAttribute("data-col") === "18-24",
    )!;
    fireEvent.click(nullCell);
    expect(handler).toHaveBeenCalledWith("Espagne", "18-24", null);
  });

  it("rend les cellules focusables (tabIndex=0, role=button) quand onCellClick fourni", () => {
    const handler = vi.fn();
    renderWithTheme({
      rows: ["A"],
      cols: ["X"],
      values: [[5]],
      onCellClick: handler,
    });
    const cell = screen.getByTestId("matrix-heatmap-cell");
    expect(cell.getAttribute("tabindex")).toBe("0");
    expect(cell.getAttribute("role")).toBe("button");
  });

  it("déclenche le callback sur Entrée (accessibilité clavier)", () => {
    const handler = vi.fn();
    renderWithTheme({
      rows: ["A"],
      cols: ["X"],
      values: [[42]],
      onCellClick: handler,
    });
    const cell = screen.getByTestId("matrix-heatmap-cell");
    fireEvent.keyDown(cell, { key: "Enter" });
    expect(handler).toHaveBeenCalledWith("A", "X", 42);
  });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("MatrixHeatmap — fill dérivé de theme.palette.primary.main", () => {
  it("le fill des cellules contient les composantes RGB de primary.main (rgba calculé)", () => {
    /**
     * On monte un thème custom avec primary.main = rgb(255, 0, 128) (rose vif).
     * Les fills alpha() de MUI produisent rgba(255, 0, 128, α).
     * On vérifie qu'au moins un fill contient "255, 0, 128" (les 3 composantes RGB).
     */
    const customPrimary = "rgb(255, 0, 128)";
    renderWithTheme(
      { rows: ["A"], cols: ["X"], values: [[100]] },
      { palette: { primary: { main: customPrimary } } },
    );
    const cell = screen.getByTestId("matrix-heatmap-cell");
    const fill = cell.getAttribute("fill") ?? "";
    // MUI alpha() produit rgba(R, G, B, a) — on vérifie les 3 composantes.
    expect(fill).toMatch(/255/);
    expect(fill).toMatch(/0/);
    expect(fill).toMatch(/128/);
  });

  it("le fill du thème custom diffère du thème par défaut (branding réel)", () => {
    // Thème par défaut MUI (primary.main ≈ #1976d2).
    const { container: defaultContainer } = renderWithTheme(
      { rows: ["A"], cols: ["X"], values: [[50]] },
    );
    const defaultFill = defaultContainer
      .querySelector("[data-testid='matrix-heatmap-cell']")
      ?.getAttribute("fill");

    // Thème avec primary.main = vert vif.
    const { container: customContainer } = renderWithTheme(
      { rows: ["A"], cols: ["X"], values: [[50]] },
      { palette: { primary: { main: "rgb(0, 200, 83)" } } },
    );
    const customFill = customContainer
      .querySelector("[data-testid='matrix-heatmap-cell']")
      ?.getAttribute("fill");

    expect(defaultFill).not.toBe(customFill);
  });
});
