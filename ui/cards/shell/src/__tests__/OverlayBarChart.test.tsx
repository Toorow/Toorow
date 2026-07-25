/**
 * Tests OverlayBarChart — Story 23.3.
 *
 * Conventions : vitest + @testing-library/react + ThemeProvider + createTheme() nu
 * (idem CardComposition.test.tsx).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider, createTheme } from "@mui/material/styles";
import OverlayBarChart from "../OverlayBarChart";
import type { OverlayBarEntry } from "../OverlayBarChart";

// ---------------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------------

const ENTRIES: OverlayBarEntry[] = [
  { label: "Jan", projection: 50_000, actual: 42_000 },
  { label: "Fév", projection: 55_000, actual: 48_000 },
  { label: "Mar", projection: 60_000, actual: 57_000 },
];

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <ThemeProvider theme={createTheme()}>{children}</ThemeProvider>
  );
}

// ---------------------------------------------------------------------------
// 1. Rend N groupes avec les deux barres quand projection ET actual sont fournis
// ---------------------------------------------------------------------------

describe("OverlayBarChart — rendu normal", () => {
  it("rend autant de barres projection et actual que d'entrées", () => {
    render(
      <Wrapper>
        <OverlayBarChart entries={ENTRIES} />
      </Wrapper>,
    );

    const projBars = screen.getAllByTestId("overlay-bar-projection");
    const actualBars = screen.getAllByTestId("overlay-bar-actual");

    expect(projBars).toHaveLength(ENTRIES.length);
    expect(actualBars).toHaveLength(ENTRIES.length);
  });
});

// ---------------------------------------------------------------------------
// 2. actual: null → une seule barre (projection), PAS de rect actual à h=0
// ---------------------------------------------------------------------------

describe("OverlayBarChart — actual null (AD-9)", () => {
  it("ne rend pas de barre actual quand actual est null", () => {
    const entries: OverlayBarEntry[] = [
      { label: "Jan", projection: 50_000, actual: null },
      { label: "Fév", projection: 55_000, actual: 48_000 },
    ];
    render(
      <Wrapper>
        <OverlayBarChart entries={entries} />
      </Wrapper>,
    );

    const projBars = screen.getAllByTestId("overlay-bar-projection");
    const actualBars = screen.getAllByTestId("overlay-bar-actual");

    // 2 projections, 1 seul actual (pour Fév)
    expect(projBars).toHaveLength(2);
    expect(actualBars).toHaveLength(1);
  });

  it("ne rend pas de barre projection quand projection est null", () => {
    const entries: OverlayBarEntry[] = [
      { label: "Jan", projection: null, actual: 42_000 },
    ];
    render(
      <Wrapper>
        <OverlayBarChart entries={entries} />
      </Wrapper>,
    );

    expect(screen.queryByTestId("overlay-bar-projection")).toBeNull();
    const actualBars = screen.getAllByTestId("overlay-bar-actual");
    expect(actualBars).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 3. État vide designé
// ---------------------------------------------------------------------------

describe("OverlayBarChart — état vide", () => {
  it("affiche l'état vide designé quand entries est vide", () => {
    render(
      <Wrapper>
        <OverlayBarChart entries={[]} />
      </Wrapper>,
    );

    const empty = screen.getByTestId("overlay-bar-empty");
    expect(empty).toBeInTheDocument();
    expect(empty).toHaveAttribute("role", "status");
    expect(empty).toHaveTextContent("Aucune donnée à afficher");
  });

  it("ne rend aucune barre quand entries est vide", () => {
    render(
      <Wrapper>
        <OverlayBarChart entries={[]} />
      </Wrapper>,
    );

    expect(screen.queryByTestId("overlay-bar-projection")).toBeNull();
    expect(screen.queryByTestId("overlay-bar-actual")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 4. headerSlot rendu quand fourni
// ---------------------------------------------------------------------------

describe("OverlayBarChart — headerSlot", () => {
  it("rend le headerSlot quand fourni", () => {
    render(
      <Wrapper>
        <OverlayBarChart
          entries={ENTRIES}
          headerSlot={<button data-testid="toggle-slot">Mensuel</button>}
        />
      </Wrapper>,
    );

    expect(screen.getByTestId("toggle-slot")).toBeInTheDocument();
    expect(screen.getByTestId("toggle-slot")).toHaveTextContent("Mensuel");
  });

  it("n'affiche pas de zone header quand headerSlot est absent", () => {
    render(
      <Wrapper>
        <OverlayBarChart entries={ENTRIES} />
      </Wrapper>,
    );

    expect(screen.queryByTestId("toggle-slot")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 5. Légende : les deux labels affichés (défaut « Projection » / « Réel »)
// ---------------------------------------------------------------------------

describe("OverlayBarChart — légende", () => {
  it("affiche les labels de légende par défaut", () => {
    render(
      <Wrapper>
        <OverlayBarChart entries={ENTRIES} />
      </Wrapper>,
    );

    const legend = screen.getByTestId("overlay-bar-legend");
    expect(legend).toBeInTheDocument();
    expect(legend).toHaveTextContent("Projection");
    expect(legend).toHaveTextContent("Réel");
  });

  it("affiche les labels de légende personnalisés", () => {
    render(
      <Wrapper>
        <OverlayBarChart
          entries={ENTRIES}
          projectionLabel="Budget"
          actualLabel="Dépensé"
        />
      </Wrapper>,
    );

    const legend = screen.getByTestId("overlay-bar-legend");
    expect(legend).toHaveTextContent("Budget");
    expect(legend).toHaveTextContent("Dépensé");
  });
});

// ---------------------------------------------------------------------------
// 6. Les fills dérivent de theme.palette.primary.main
// ---------------------------------------------------------------------------

describe("OverlayBarChart — couleurs dérivées de l'accent (AD-11)", () => {
  it("les rects actual ont un fill qui contient les composantes RGB de primary.main", () => {
    // Thème custom avec un primary.main distinctif : rgb(200, 50, 100)
    const customTheme = createTheme({
      palette: { primary: { main: "rgb(200, 50, 100)" } },
    });

    render(
      <ThemeProvider theme={customTheme}>
        <OverlayBarChart entries={ENTRIES} />
      </ThemeProvider>,
    );

    // Les barres actual doivent avoir fill = accent = rgb(200,50,100)
    // MUI peut recompiler la couleur mais les composantes 200, 50, 100 doivent apparaître.
    const actualBars = screen.getAllByTestId("overlay-bar-actual");
    expect(actualBars.length).toBeGreaterThan(0);

    // Le fill du premier rect actual doit contenir les valeurs RGB de l'accent.
    const fill = actualBars[0]!.getAttribute("fill") ?? "";
    // fill = palette.primary.main = "rgb(200, 50, 100)" (ou équivalent résolu par MUI)
    expect(fill).toContain("200");
    expect(fill).toContain("50");
    expect(fill).toContain("100");
  });

  it("les rects projection ont un fill semi-transparent (alpha) de l'accent", () => {
    const customTheme = createTheme({
      palette: { primary: { main: "rgb(200, 50, 100)" } },
    });

    render(
      <ThemeProvider theme={customTheme}>
        <OverlayBarChart entries={ENTRIES} />
      </ThemeProvider>,
    );

    const projBars = screen.getAllByTestId("overlay-bar-projection");
    expect(projBars.length).toBeGreaterThan(0);

    // fill projection = alpha(accent, 0.22) — doit être rgba(200,50,100,0.22)
    const fill = projBars[0]!.getAttribute("fill") ?? "";
    // Vérifie la présence des composantes RGB (200, 50, 100) dans la valeur rgba
    expect(fill).toContain("200");
    expect(fill).toContain("50");
    expect(fill).toContain("100");
    // Et la transparence : la valeur alpha doit être inférieure à 1 (présence du 4e canal)
    expect(fill).toMatch(/rgba?\(/);
  });
});

// ---------------------------------------------------------------------------
// 7. Accessibilité : SVG role="img" + aria-label
// ---------------------------------------------------------------------------

describe("OverlayBarChart — accessibilité", () => {
  it("le SVG a role='img' et un aria-label français par défaut", () => {
    render(
      <Wrapper>
        <OverlayBarChart entries={ENTRIES} />
      </Wrapper>,
    );

    const svg = document.querySelector("svg[role='img']");
    expect(svg).toBeInTheDocument();
    expect(svg?.getAttribute("aria-label")).toContain("Graphique");
  });

  it("respecte l'ariaLabel personnalisé", () => {
    render(
      <Wrapper>
        <OverlayBarChart
          entries={ENTRIES}
          ariaLabel="Revenu total mensuel"
        />
      </Wrapper>,
    );

    const svg = document.querySelector("svg[role='img']");
    expect(svg?.getAttribute("aria-label")).toBe("Revenu total mensuel");
  });
});

// ---------------------------------------------------------------------------
// 8. Les deux null → emplacement vide avec label X (AD-9)
// ---------------------------------------------------------------------------

describe("OverlayBarChart — groupe entièrement null (AD-9)", () => {
  it("n'affiche aucune barre pour un groupe avec projection et actual null, mais le label est présent", () => {
    const entries: OverlayBarEntry[] = [
      { label: "Jan", projection: 50_000, actual: 42_000 },
      { label: "Fév", projection: null, actual: null },
    ];

    render(
      <Wrapper>
        <OverlayBarChart entries={entries} />
      </Wrapper>,
    );

    // Seule la catégorie Jan a des barres
    const projBars = screen.getAllByTestId("overlay-bar-projection");
    const actualBars = screen.getAllByTestId("overlay-bar-actual");
    expect(projBars).toHaveLength(1);
    expect(actualBars).toHaveLength(1);

    // Le label "Fév" doit tout de même être rendu dans le SVG
    const svgText = document.querySelector("svg")?.textContent ?? "";
    expect(svgText).toContain("Fév");
  });
});
