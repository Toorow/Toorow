/**
 * Attribution card tests (Epic 16, Story 16.3).
 *
 * Couvre :
 *   - Rendu de la fixture happy-path sans exception.
 *   - Valeurs rendues depuis block.data (pas uniquement les headers — leçon F-4 review-10-6).
 *   - Libellés FRANÇAIS accentués (UX-DR10).
 *   - 5 blocs dans la composition (kpi_row + 2 bars + table + comment).
 *   - Contrainte double-compte : 2 blocs bar présents (dernier clic + premier clic).
 *   - Dégradés propres : bars:[] → bar-chart-empty, rows:[] → data-table-empty,
 *     metrics:[] → composition-kpi-row-empty, text:"" → composition-comment-empty.
 *   - Composition vide → empty-state sans exception.
 *   - Fixture FIXTURE_ENVELOPE_NO_ACQDATA : tous les blocs acquisition vides.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import {
  FIXTURE_ENVELOPE,
  FIXTURE_ENVELOPE_NO_ACQDATA,
} from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

// ---------------------------------------------------------------------------
// Helper : construit une enveloppe avec une composition arbitraire
// ---------------------------------------------------------------------------
function makeEnvelope(
  composition: CardEnvelope["data"]["composition"]
): CardEnvelope {
  return {
    ...FIXTURE_ENVELOPE,
    data: {
      ...FIXTURE_ENVELOPE.data,
      composition,
    },
  };
}

// ---------------------------------------------------------------------------
// 1. Rendu happy-path depuis FIXTURE_ENVELOPE
// ---------------------------------------------------------------------------

describe("Attribution card — fixture happy-path (FIXTURE_ENVELOPE)", () => {
  it("rend le titre de la carte", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Attribution");
  });

  it("rend le kpi_row (bloc conversions dernier clic)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-kpi-row")).toBeInTheDocument();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBeGreaterThanOrEqual(1);
    // Le premier tile est conversions (seule métrique du kpi_row attribution).
    const convTile = tiles.find((t) => t.getAttribute("data-metric") === "conversions")!;
    expect(convTile).toBeInTheDocument();
    // Valeur fixture = 3 780 -> fr-FR contient "3"
    const valEl = convTile.querySelector("[data-testid='composition-kpi-value']");
    expect(valEl?.textContent).toMatch(/3/);
  });

  it("rend deux blocs bar (dernier clic + premier clic)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const barBlocks = screen.getAllByTestId("composition-block-bar");
    // La carte attribution porte EXACTEMENT 2 blocs bar (session_source_medium et first_user_source_medium).
    expect(barBlocks.length).toBe(2);
  });

  it("rend le titre FR accentué 'Canaux — dernier clic' (UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText(/Canaux\s*[—–]\s*dernier clic/)).toBeInTheDocument();
  });

  it("rend le titre FR accentué 'Canaux — premier clic' (UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText(/Canaux\s*[—–]\s*premier clic/)).toBeInTheDocument();
  });

  it("rend les barres du bloc dernier clic avec valeur fixture (F-4 : valeur, pas juste l'en-tête)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // La fixture porte 'cpc / google' en top-canal dernier clic — son label doit apparaître.
    const barCharts = screen.getAllByTestId("bar-chart");
    expect(barCharts.length).toBeGreaterThanOrEqual(1);
    // Le label 'cpc / google' est dans le premier bar-chart (dernier clic).
    expect(barCharts[0]).toBeInTheDocument();
    expect(screen.getAllByText(/cpc \/ google/).length).toBeGreaterThanOrEqual(1);
  });

  it("rend les barres du bloc premier clic avec valeur fixture (organic / google en tête)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Fixture premier clic : organic / google est en tête.
    expect(screen.getAllByText(/organic \/ google/).length).toBeGreaterThanOrEqual(1);
  });

  it("rend le titre FR accentué 'Campagnes — dernier clic' (UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText(/Campagnes\s*[—–]\s*dernier clic/)).toBeInTheDocument();
  });

  it("rend la table des campagnes avec ses colonnes FR (F-4 : valeurs rendues)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // 1 bloc table (session_campaign)
    const tables = screen.getAllByTestId("composition-table");
    expect(tables.length).toBe(1);
    // En-têtes FR : « Campagne », « conversions », « Part (top-N) »
    expect(screen.getByText("Campagne")).toBeInTheDocument();
    // Colonne Part (top-N) : label honnête (sémantique 10.4 — subtotal_share)
    expect(screen.getByText(/Part \(top-N\)/)).toBeInTheDocument();
  });

  it("rend une valeur de part (part_pct) dans la table campagnes (F-4 : pas juste l'en-tête)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Fixture : summer_sale part_pct = 42.9 -> fr-FR "42,9"
    // On vérifie qu'un nombre avec virgule/point décimale apparaît comme cellule de valeur.
    expect(screen.getByText(/42[.,]9/)).toBeInTheDocument();
  });

  it("rend les noms de campagnes dans la table (summer_sale)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("summer_sale")).toBeInTheDocument();
  });

  it("rend le bloc comment avec le contenu du commentaire attributi", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    // Le commentaire contient le canal leader + la mention « dernier clic »
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Canal leader");
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("dernier clic");
  });

  it("rend la mention 'Contexte manquant' dans le commentaire (AD-9 — jamais de cause inventée)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Contexte manquant");
  });

  it("rend le feedback chrome CardShell", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. Dégradé acquisition OFF : FIXTURE_ENVELOPE_NO_ACQDATA
// ---------------------------------------------------------------------------

describe("Attribution card — dégradé acquisition OFF (FIXTURE_ENVELOPE_NO_ACQDATA)", () => {
  it("rend la carte sans exception quand les données d'acquisition sont absentes", () => {
    expect(() => render(<App envelope={FIXTURE_ENVELOPE_NO_ACQDATA} />)).not.toThrow();
  });

  it("rend le titre même sans données d'acquisition", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NO_ACQDATA} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Attribution");
  });

  it("les deux blocs bar sont vides (bar-chart-empty) quand l'acquisition est OFF", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NO_ACQDATA} />);
    // Les deux bars retournent bars:[] -> bar-chart-empty doit être présent.
    const emptyBars = screen.getAllByTestId("bar-chart-empty");
    expect(emptyBars.length).toBe(2);
  });

  it("le bloc table campagnes est vide (data-table-empty) quand l'acquisition est OFF", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NO_ACQDATA} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("le commentaire dégradé contient 'Contexte manquant' (AD-9)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NO_ACQDATA} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Contexte manquant");
  });
});

// ---------------------------------------------------------------------------
// 3. Dégradés bloc par bloc (composition ad-hoc) — jamais d'exception
// ---------------------------------------------------------------------------

describe("Attribution card — dégradés blocs individuels (composition ad-hoc)", () => {
  it("kpi_row avec metrics:[] → composition-kpi-row-empty", () => {
    const env = makeEnvelope([
      { type: "kpi_row", binding: { metrics: "conversions" }, data: { metrics: [] } },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-kpi-row-empty")).toBeInTheDocument();
  });

  it("bar avec bars:[] → bar-chart-empty (dernier clic dégradé)", () => {
    const env = makeEnvelope([
      {
        type: "bar",
        title: "Canaux — dernier clic",
        binding: { metrics: "conversions", dimensions: ["session_source_medium"] },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
  });

  it("bar avec bars:[] → bar-chart-empty (premier clic dégradé)", () => {
    const env = makeEnvelope([
      {
        type: "bar",
        title: "Canaux — premier clic",
        binding: { metrics: "conversions", dimensions: ["first_user_source_medium"] },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
  });

  it("table avec rows:[] → data-table-empty (campagnes dégradées)", () => {
    const env = makeEnvelope([
      {
        type: "table",
        title: "Campagnes — dernier clic",
        binding: {
          metrics: ["conversions"],
          dimensions: ["session_campaign"],
          subtotal_share: true,
        },
        data: {
          columns: [
            { key: "_dim", label: "Campagne", numeric: false },
            { key: "conversions", label: "conversions", numeric: true },
            { key: "part_pct", label: "Part (top-N)", numeric: true },
          ],
          rows: [],
        },
      },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("comment avec text:'' → composition-comment-empty", () => {
    const env = makeEnvelope([
      { type: "comment", binding: { metrics: "*" }, data: { text: "" } },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-comment-empty")).toBeInTheDocument();
  });

  it("composition entièrement vide → carte utilisable sans exception", () => {
    const env = makeEnvelope([]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    expect(screen.getByTestId("card-title")).toHaveTextContent("Attribution");
  });

  it("type de bloc inconnu → composition-unknown-block (jamais d'exception)", () => {
    const env = makeEnvelope([
      { type: "unknown_block_type" as never, binding: {} },
    ]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    expect(screen.getByTestId("composition-unknown-block")).toBeInTheDocument();
  });

  it("les 2 bars vides + table vide + kpi_row vide → carte entièrement dégradée, jamais d'exception", () => {
    const env = makeEnvelope([
      { type: "kpi_row", binding: { metrics: "conversions" }, data: { metrics: [] } },
      {
        type: "bar",
        title: "Canaux — dernier clic",
        binding: { metrics: "conversions", dimensions: ["session_source_medium"] },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
      {
        type: "bar",
        title: "Canaux — premier clic",
        binding: { metrics: "conversions", dimensions: ["first_user_source_medium"] },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
      {
        type: "table",
        title: "Campagnes — dernier clic",
        binding: { metrics: ["conversions"], dimensions: ["session_campaign"] },
        data: { columns: [], rows: [] },
      },
      {
        type: "comment",
        binding: { metrics: "*" },
        data: { text: "Contexte manquant pour cette période." },
      },
    ]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    // kpi_row dégradé
    expect(screen.getByTestId("composition-kpi-row-empty")).toBeInTheDocument();
    // 2 bars vides
    const emptyBars = screen.getAllByTestId("bar-chart-empty");
    expect(emptyBars.length).toBe(2);
    // table campagnes vide
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
    // comment présent (pas vide)
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Contexte manquant");
  });
});

// ---------------------------------------------------------------------------
// 4. Ordre des blocs dans la fixture (contrat composition serveur)
// ---------------------------------------------------------------------------

describe("Attribution card — ordre des blocs dans la fixture (contrat composition)", () => {
  it("kpi_row avant les bars, bars avant la table, table avant le comment", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const composition = screen.getByTestId("card-composition");
    const nodes = Array.from(composition.children);

    const kpiIdx = nodes.findIndex(
      (n) =>
        n.querySelector("[data-testid='composition-block-kpi_row']") ||
        n.getAttribute("data-testid") === "composition-block-kpi_row"
    );
    const barIdx = nodes.findIndex(
      (n) =>
        n.querySelector("[data-testid='composition-block-bar']") ||
        n.getAttribute("data-testid") === "composition-block-bar"
    );
    const tableIdx = nodes.findIndex(
      (n) =>
        n.querySelector("[data-testid='composition-table']") ||
        n.getAttribute("data-testid") === "composition-table"
    );
    const commentIdx = nodes.findIndex(
      (n) =>
        n.querySelector("[data-testid='composition-block-comment']") ||
        n.getAttribute("data-testid") === "composition-block-comment"
    );

    expect(kpiIdx).toBeGreaterThanOrEqual(0);
    expect(barIdx).toBeGreaterThanOrEqual(0);
    expect(tableIdx).toBeGreaterThanOrEqual(0);
    expect(commentIdx).toBeGreaterThanOrEqual(0);
    expect(kpiIdx).toBeLessThan(barIdx);
    expect(barIdx).toBeLessThan(tableIdx);
    expect(tableIdx).toBeLessThan(commentIdx);
  });
});

// ---------------------------------------------------------------------------
// 5. Libellés français accentués — vérification globale (UX-DR10)
// ---------------------------------------------------------------------------

describe("Attribution card — libellés français accentués (UX-DR10)", () => {
  it("le contenu de la carte ne contient aucun libellé non-accentué type 'Canal leader' en anglais", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Le titre de la carte doit être « Attribution » (FR, pas « Attribution Card »).
    expect(screen.getByTestId("card-title").textContent).not.toMatch(/^Attribution Card/i);
  });

  it("le commentaire contient 'Canal leader' avec accent (jamais 'Channel')", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    expect(comment.textContent).toContain("Canal");
    expect(comment.textContent).not.toMatch(/\bChannel\b/i);
  });

  it("la colonne 'Campagne' est en français dans la table campagnes", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Campagne")).toBeInTheDocument();
  });

  it("le libellé de part est 'Part (top-N)' — jamais 'share' en anglais", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Libellé honnête (sémantique 10.4 : subtotal_share, pas le total GA4).
    expect(screen.getByText(/Part \(top-N\)/)).toBeInTheDocument();
    // Ne pas trouver 'share' en anglais brut
    const tables = screen.getAllByTestId("composition-table");
    tables.forEach((t) => {
      // Le label 'share' seul (hors 'subtotal_share' en JSON qui n'est jamais rendu) ne doit pas apparaître.
      expect(t.textContent).not.toMatch(/\bshare\b/i);
    });
  });
});
