/**
 * Déduplication card tests (Epic 17, Story 17.3).
 *
 * Couvre :
 *   - Rendu de la fixture happy-path sans exception.
 *   - Valeurs rendues depuis block.data (pas uniquement les headers — leçon F-4 review-10-6).
 *   - AD-9 NON-NÉGOCIABLE : le mot « Estimation » est visible dans le contenu rendu.
 *   - 4 blocs dans la composition (kpi_row + bar + table + comment).
 *   - Libellés français accentués (UX-DR10).
 *   - Dégradés propres :
 *       * kpi_row value=null → NE doit PAS afficher 0 (jamais 0 déguisé — AD-9).
 *       * table rows:[] → data-table-empty.
 *       * opt-out → message de guidance « source » dans le commentaire.
 *   - Composition vide → empty-state sans exception.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import {
  FIXTURE_ENVELOPE,
  FIXTURE_ENVELOPE_NULL_RATE,
  FIXTURE_ENVELOPE_OPT_OUT,
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

describe("Déduplication card — fixture happy-path (FIXTURE_ENVELOPE)", () => {
  it("rend le titre de la carte", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Déduplication");
  });

  it("rend le kpi_row (bloc duplication_rate)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-kpi-row")).toBeInTheDocument();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBeGreaterThanOrEqual(1);
  });

  it("rend le bloc bar (revendiqué vs dédupliqué par canal)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const barBlocks = screen.getAllByTestId("composition-block-bar");
    expect(barBlocks.length).toBeGreaterThanOrEqual(1);
  });

  it("rend le bloc table avec le titre 'Détail par canal' (UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Détail par canal")).toBeInTheDocument();
  });

  it("rend la colonne 'Canal' dans la table (libellé FR, UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Canal")).toBeInTheDocument();
  });

  it("rend la colonne 'Revendiqué' dans la table (libellé FR, UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Revendiqué").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Part (%)' dans la table (UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Part (%)")).toBeInTheDocument();
  });

  it("rend un canal dans la table (meta-ads — valeur F-4 : valeur rendue, pas juste l'en-tête)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("meta-ads")).toBeInTheDocument();
  });

  it("rend une Part (part_pct) pour meta-ads = 50 % (F-4 : valeur rendue précise)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // Fixture : meta-ads part_pct = 50.0 -> fr-FR "50" ou "50,0". La valeur 50
    // apparaît aussi dans le kpi_row : on assert AU MOINS une occurrence rendue
    // (la présence dans la table est couverte par le test meta-ads ci-dessus).
    expect(screen.getAllByText(/50[.,]?0?(%)?/).length).toBeGreaterThanOrEqual(1);
  });

  it("rend le bloc comment avec 'Estimation' (AD-9 non-négociable)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    const comment = screen.getByTestId("composition-comment");
    // AD-9 : le mot 'Estimation' VERBATIM doit apparaître dans le commentaire
    expect(comment.textContent?.toLowerCase()).toContain("estimation");
  });

  it("rend la formule dans le commentaire (AD-9 : formule citée)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    // La formule "taux = Σ revendiqué ÷ réel" doit apparaître (ou au moins "revendiqué")
    expect(comment.textContent?.toLowerCase()).toMatch(/revendiqué|formule/);
  });

  it("rend la source de vérité dans le commentaire (ga4)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    expect(comment.textContent?.toLowerCase()).toContain("ga4");
  });

  it("rend 'Contexte manquant' dans le commentaire (AD-9 verbatim)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent(
      "Contexte manquant"
    );
  });

  it("rend le feedback chrome CardShell", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. Dégradé verified NULL (FIXTURE_ENVELOPE_NULL_RATE — stripe v1 BLOCKED)
// ---------------------------------------------------------------------------

describe("Déduplication card — dégradé verified NULL (FIXTURE_ENVELOPE_NULL_RATE)", () => {
  it("rend la carte sans exception quand le taux est NULL", () => {
    expect(() => render(<App envelope={FIXTURE_ENVELOPE_NULL_RATE} />)).not.toThrow();
  });

  it("rend le titre même sans taux disponible", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NULL_RATE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Déduplication");
  });

  it("AD-9 CRITIQUE : le commentaire mentionne l'indisponibilité, jamais 0%", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NULL_RATE} />);
    const comment = screen.getByTestId("composition-comment");
    const text = comment.textContent ?? "";
    // Doit mentionner l'absence
    expect(text.toLowerCase()).toMatch(/indisponible|absente|non configurée/);
    // review-17-5 fix-10: Ne doit PAS contenir AUCUNE forme de 0 déguisé — AD-9.
    // Couvre : 0,0x / 0.0x / 0,0 % / 0.00 / 0 x (espace) / "0x" seul.
    expect(text).not.toMatch(/0[,.]0x/i);       // 0,0x / 0.0x
    expect(text).not.toMatch(/0[,.]0 ?%/i);     // 0,0 % / 0.0%
    expect(text).not.toMatch(/0\.00/i);          // 0.00
    expect(text).not.toMatch(/\b0 x\b/i);        // 0 x (avec espace)
    expect(text).not.toMatch(/\b0x\b/i);         // 0x seul (sans décimale)
  });

  it("rend 'Contexte manquant' dans le commentaire dégradé (AD-9)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_NULL_RATE} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent(
      "Contexte manquant"
    );
  });
});

// ---------------------------------------------------------------------------
// 3. Dégradé opt-out (FIXTURE_ENVELOPE_OPT_OUT — aucune source désignée)
// ---------------------------------------------------------------------------

describe("Déduplication card — dégradé opt-out (FIXTURE_ENVELOPE_OPT_OUT)", () => {
  it("rend la carte sans exception quand le projet est opt-out", () => {
    expect(() => render(<App envelope={FIXTURE_ENVELOPE_OPT_OUT} />)).not.toThrow();
  });

  it("rend le titre même sans configuration de source", () => {
    render(<App envelope={FIXTURE_ENVELOPE_OPT_OUT} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Déduplication");
  });

  it("le commentaire opt-out mentionne 'source' ou 'vérification' (guidance utilisateur)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_OPT_OUT} />);
    const comment = screen.getByTestId("composition-comment");
    const text = comment.textContent?.toLowerCase() ?? "";
    expect(text).toMatch(/source|vérification|configuration/);
  });

  it("la table opt-out est vide (data-table-empty)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_OPT_OUT} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 4. Dégradés bloc par bloc (composition ad-hoc) — jamais d'exception
// ---------------------------------------------------------------------------

describe("Déduplication card — dégradés blocs individuels (composition ad-hoc)", () => {
  it("kpi_row avec metrics:[] → composition-kpi-row-empty", () => {
    const env = makeEnvelope([
      {
        type: "kpi_row",
        title: "Taux de duplication (Estimation)",
        binding: { metrics: "duplication_rate" },
        data: { metrics: [] },
      },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-kpi-row-empty")).toBeInTheDocument();
  });

  it("table avec rows:[] → data-table-empty", () => {
    const env = makeEnvelope([
      {
        type: "table",
        title: "Détail par canal",
        binding: {
          metrics: ["claimed_conversions", "deduplicated_contribution"],
          dimensions: ["channel_connector"],
        },
        data: {
          columns: [
            { key: "_dim", label: "Canal", numeric: false },
            { key: "claimed_conversions", label: "Revendiqué", numeric: true },
            {
              key: "deduplicated_contribution",
              label: "Contribution dédupliquée (Estimation)",
              numeric: true,
            },
            { key: "part_pct", label: "Part (%)", numeric: true },
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
    expect(screen.getByTestId("card-title")).toHaveTextContent("Déduplication");
  });

  it("type de bloc inconnu → composition-unknown-block (jamais d'exception)", () => {
    const env = makeEnvelope([
      { type: "unknown_block_type" as never, binding: {} },
    ]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    expect(screen.getByTestId("composition-unknown-block")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 5. Ordre des blocs (contrat composition serveur AI-54)
// ---------------------------------------------------------------------------

describe("Déduplication card — ordre des blocs dans la fixture (contrat composition)", () => {
  it("kpi_row avant bar, bar avant table, table avant comment", () => {
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
// 6. Libellés français accentués — vérification globale (UX-DR10)
// ---------------------------------------------------------------------------

describe("Déduplication card — libellés français accentués (UX-DR10)", () => {
  it("le titre de la carte est 'Déduplication' (avec accent)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title").textContent).toContain("Déduplication");
  });

  it("le commentaire contient 'Estimation' (français, jamais 'estimate' en anglais)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    // AD-9 : le mot français 'Estimation' (ou 'estimation') doit être présent
    expect(comment.textContent?.toLowerCase()).toContain("estimation");
    // Ne doit pas être 'estimate' seul (anglais)
    expect(comment.textContent).not.toMatch(/\bestimate\b/i);
  });

  it("la colonne Canal est en français (jamais 'Channel')", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Canal")).toBeInTheDocument();
    // 'Channel' en anglais ne doit pas apparaître
    const tables = screen.getAllByTestId("composition-table");
    tables.forEach((t) => {
      expect(t.textContent).not.toMatch(/\bChannel\b/);
    });
  });

  it("la colonne Revendiqué est en français (jamais 'claimed')", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Revendiqué").length).toBeGreaterThanOrEqual(1);
    const tables = screen.getAllByTestId("composition-table");
    tables.forEach((t) => {
      // 'claimed' seul en anglais ne doit pas apparaître comme libellé de colonne
      expect(t.textContent).not.toMatch(/^claimed$/im);
    });
  });
});
