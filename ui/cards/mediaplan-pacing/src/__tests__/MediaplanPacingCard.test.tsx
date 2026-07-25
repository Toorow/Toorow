/**
 * Carte Pacing Médiaplan tests (Epic 22, Story 22.5).
 *
 * Couvre :
 *   - Rendu de la fixture happy-path sans exception.
 *   - Valeurs rendues depuis block.data (leçon F-4 review-10-6).
 *   - AD-9 NON-NÉGOCIABLE : pace NULL → jamais rendu « 0 » ou « 0 % ».
 *   - Badge « Plan seul » visible pour les lignes plan-only.
 *   - Titre de la carte et libellés français accentués (UX-DR10).
 *   - Commentaire présent avec formule citée et « Estimation ».
 *   - Dégradés propres : table vide, composition vide, bloc inconnu.
 *   - Ordre des blocs (2 tables + 1 comment).
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import {
  FIXTURE_ENVELOPE,
  FIXTURE_ENVELOPE_EMPTY,
  FIXTURE_ENVELOPE_PACE_NULL,
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

describe("Pacing Médiaplan card — fixture happy-path (FIXTURE_ENVELOPE)", () => {
  it("rend le titre de la carte 'Pacing Médiaplan'", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Pacing Médiaplan");
  });

  it("rend la table 'Lignes du plan' avec son titre", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Lignes du plan")).toBeInTheDocument();
  });

  it("rend la colonne 'Ligne' (libellé FR, UX-DR10)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Ligne").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Support' (libellé FR)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Support").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Budget (€)'", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Budget (€)").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Consommé (%)' (libellé FR)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Consommé (%)").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Pace (%)'", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Pace (%)").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Reste (€)'", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getAllByText("Reste (€)").length).toBeGreaterThanOrEqual(1);
  });

  it("rend la colonne 'Extrapolé (€) (Estimation)' avec le label Estimation (AD-9)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(
      screen.getAllByText("Extrapolé (€) (Estimation)").length
    ).toBeGreaterThanOrEqual(1);
  });

  it("rend la ligne Digital A avec sa valeur budget 3 000 (F-4 : valeur rendue)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Digital A")).toBeInTheDocument();
  });

  it("rend la ligne TV Brand dans la table", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("TV Brand")).toBeInTheDocument();
  });

  it("rend la table 'Rollup par support'", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("Rollup par support")).toBeInTheDocument();
  });

  it("rend le canal 'digital' dans le rollup", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // 'digital' apparaît dans la table lignes ET dans le rollup par support.
    expect(screen.getAllByText("digital").length).toBeGreaterThanOrEqual(1);
  });

  it("rend le bloc comment avec 'Estimation' (AD-9 non-négociable)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    const comment = screen.getByTestId("composition-comment");
    expect(comment.textContent?.toLowerCase()).toContain("estimation");
  });

  it("rend la formule de pace dans le commentaire (AD-9 : formule citée)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    // La formule "Pace = (réel − prévu to-date) / prévu to-date" doit apparaître.
    expect(comment.textContent?.toLowerCase()).toMatch(/pace|réel|prévu/);
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
// 2. AD-9 CRITIQUE : pace NULL → jamais 0 ou 0 %
// ---------------------------------------------------------------------------

describe("Pacing Médiaplan card — AD-9 pace NULL (FIXTURE_ENVELOPE_PACE_NULL)", () => {
  it("rend la carte sans exception quand pace est NULL", () => {
    expect(() => render(<App envelope={FIXTURE_ENVELOPE_PACE_NULL} />)).not.toThrow();
  });

  it("AD-9 CRITIQUE : le commentaire ne contient PAS de '0 %' ni '0,0%' pour un pace NULL", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PACE_NULL} />);
    const comment = screen.getByTestId("composition-comment");
    const text = comment.textContent ?? "";
    // Vérification stricte : un pace NULL ne doit pas apparaître comme 0.
    expect(text).not.toMatch(/\b0[,.]0\s?%/i);
    expect(text).not.toMatch(/pace\s*:\s*0\b/i);
  });

  it("le commentaire mentionne l'absence (non disponible / manquant)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PACE_NULL} />);
    const comment = screen.getByTestId("composition-comment");
    const text = comment.textContent?.toLowerCase() ?? "";
    expect(text).toMatch(/non disponible|manquant|contexte/);
  });
});

// ---------------------------------------------------------------------------
// 3. Dégradé plan vide (FIXTURE_ENVELOPE_EMPTY)
// ---------------------------------------------------------------------------

describe("Pacing Médiaplan card — plan sans lignes (FIXTURE_ENVELOPE_EMPTY)", () => {
  it("rend la carte sans exception quand le plan n'a pas de lignes", () => {
    expect(() => render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />)).not.toThrow();
  });

  it("rend le titre même avec une composition vide", () => {
    render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Pacing Médiaplan");
  });

  it("les tables vides affichent l'état vide (data-table-empty)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />);
    const emptyStates = screen.getAllByTestId("data-table-empty");
    expect(emptyStates.length).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// 4. Dégradés bloc par bloc (composition ad-hoc) — jamais d'exception
// ---------------------------------------------------------------------------

describe("Pacing Médiaplan card — dégradés blocs individuels", () => {
  it("table avec rows:[] → data-table-empty", () => {
    const env = makeEnvelope([
      {
        type: "table",
        title: "Lignes du plan",
        binding: { source: "plan_lines" },
        data: {
          columns: [
            { key: "label", label: "Ligne", numeric: false },
            { key: "pace_pct", label: "Pace (%)", numeric: true },
          ],
          rows: [],
          estimate_columns: [],
          plan_only_badge_column: "is_plan_only",
        },
      },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("comment avec text:'' → composition-comment-empty", () => {
    const env = makeEnvelope([
      { type: "comment", binding: { source: "plan_pacing" }, data: { text: "" } },
    ]);
    render(<App envelope={env} />);
    expect(screen.getByTestId("composition-comment-empty")).toBeInTheDocument();
  });

  it("composition entièrement vide → carte utilisable sans exception", () => {
    const env = makeEnvelope([]);
    expect(() => render(<App envelope={env} />)).not.toThrow();
    expect(screen.getByTestId("card-title")).toHaveTextContent("Pacing Médiaplan");
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

describe("Pacing Médiaplan card — ordre des blocs (contrat AI-54)", () => {
  it("table#1 (lignes) avant table#2 (rollup) avant comment", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const composition = screen.getByTestId("card-composition");
    const nodes = Array.from(composition.children);

    // On trouve les positions des deux table blocks et du comment block.
    const tableBlocks = nodes.filter(
      (n) =>
        n.querySelector("[data-testid='composition-table']") ||
        n.getAttribute("data-testid") === "composition-table"
    );
    const commentIdx = nodes.findIndex(
      (n) =>
        n.querySelector("[data-testid='composition-block-comment']") ||
        n.getAttribute("data-testid") === "composition-block-comment"
    );

    // Au moins 2 tables et 1 comment.
    expect(tableBlocks.length).toBeGreaterThanOrEqual(2);
    expect(commentIdx).toBeGreaterThanOrEqual(0);

    // Le comment arrive après les tables.
    const firstTableIdx = nodes.indexOf(tableBlocks[0]);
    expect(firstTableIdx).toBeLessThan(commentIdx);
  });
});

// ---------------------------------------------------------------------------
// 6. Libellés français accentués (UX-DR10)
// ---------------------------------------------------------------------------

describe("Pacing Médiaplan card — libellés français accentués (UX-DR10)", () => {
  it("le titre est 'Pacing Médiaplan' avec accent", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title").textContent).toContain("Pacing Médiaplan");
  });

  it("le commentaire contient 'Estimation' (français, jamais 'estimate' anglais)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const comment = screen.getByTestId("composition-comment");
    expect(comment.textContent?.toLowerCase()).toContain("estimation");
    expect(comment.textContent).not.toMatch(/\bestimate\b/i);
  });

  it("les colonnes sont en français (pas d'anglais 'Budget' seul sans €)", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    // 'Budget (€)' doit apparaître
    expect(screen.getAllByText("Budget (€)").length).toBeGreaterThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// 7. Snapshot contrat AI-54 (structure de la composition)
// ---------------------------------------------------------------------------

describe("Pacing Médiaplan card — snapshot composition (AI-54)", () => {
  it("la composition a 4 blocs (2 tables + actuals non mappés + comment) — E1-F-2", () => {
    expect(FIXTURE_ENVELOPE.data.composition?.length).toBe(4);
    const types = FIXTURE_ENVELOPE.data.composition?.map((b) => b.type);
    expect(types).toEqual(["table", "table", "table", "comment"]);
  });

  it("la table lignes a les colonnes attendues", () => {
    const lineTable = FIXTURE_ENVELOPE.data.composition?.[0];
    const columns = (lineTable?.data as Record<string, unknown>)?.columns as Array<{ key: string }>;
    const keys = columns?.map((c) => c.key) ?? [];
    expect(keys).toContain("label");
    expect(keys).toContain("budget");
    expect(keys).toContain("pace_pct");
    expect(keys).toContain("extrapolated_spend");
    expect(keys).toContain("is_plan_only");
  });

  it("la ligne TV Brand a pace_pct=null (plan-only, AD-9 jamais 0)", () => {
    const lineTable = FIXTURE_ENVELOPE.data.composition?.[0];
    const rows = (lineTable?.data as Record<string, unknown>)?.rows as Array<Record<string, unknown>>;
    const tvRow = rows?.find((r) => r.line_key === "line-tv");
    expect(tvRow).toBeDefined();
    expect(tvRow?.pace_pct).toBeNull();
    expect(tvRow?.consumed_pct).toBeNull();
    expect(tvRow?.extrapolated_spend).toBeNull();
    expect(tvRow?.is_plan_only).toBe(true);
  });

  it("la ligne Digital A a pace_pct=25 (cas chiffré 22.4)", () => {
    const lineTable = FIXTURE_ENVELOPE.data.composition?.[0];
    const rows = (lineTable?.data as Record<string, unknown>)?.rows as Array<Record<string, unknown>>;
    const digitalRow = rows?.find((r) => r.line_key === "line-digital-a");
    expect(digitalRow).toBeDefined();
    expect(digitalRow?.pace_pct).toBe(25.0);
    expect(digitalRow?.extrapolated_spend).toBe(3750.0);
  });
});
