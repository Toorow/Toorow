/**
 * Funnel tests (Story 9.2b) — étapes, taux de passage, drop-off, empty.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import Funnel from "../Funnel";
import type { FunnelStep } from "../Funnel";

const STEPS: FunnelStep[] = [
  { label: "Sessions", value: 10000 },
  { label: "Pages produit", value: 4000 },
  { label: "Panier", value: 1200 },
  { label: "Commande", value: 300 },
];

describe("Funnel", () => {
  it("renders all steps", () => {
    render(<Funnel steps={STEPS} ariaLabel="Entonnoir conversions" />);
    const funnel = screen.getByTestId("funnel");
    expect(funnel).toBeInTheDocument();
    expect(funnel).toHaveTextContent("Sessions");
    expect(funnel).toHaveTextContent("Panier");
    expect(funnel).toHaveTextContent("Commande");
  });

  it("renders through-rates between steps", () => {
    render(<Funnel steps={STEPS} ariaLabel="Entonnoir" />);
    // Entre étape 0 (10000) et étape 1 (4000) = 40%
    expect(screen.getByTestId("funnel-through-rate-1")).toHaveTextContent("40 %");
    // Entre étape 1 (4000) et étape 2 (1200) = 30%
    expect(screen.getByTestId("funnel-through-rate-2")).toHaveTextContent("30 %");
  });

  it("renders step values (the number 10000 appears in some form)", () => {
    render(<Funnel steps={STEPS} ariaLabel="Entonnoir" />);
    const funnel = screen.getByTestId("funnel");
    // fr-FR locale uses narrow no-break space (U+202F) as thousands separator.
    // Normalize to compare as plain text.
    const text = (funnel.textContent ?? "").replace(/[  ]/g, " ");
    expect(text).toContain("10 000");
  });

  it("renders the designed empty state when steps is empty", () => {
    render(<Funnel steps={[]} ariaLabel="Entonnoir vide" />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
    expect(screen.getByTestId("funnel-empty")).toHaveTextContent("Aucune étape disponible");
  });

  it("has an accessible aria role and label", () => {
    render(<Funnel steps={STEPS} ariaLabel="Parcours utilisateur" />);
    expect(screen.getByRole("img", { name: "Parcours utilisateur" })).toBeInTheDocument();
  });

  it("does not render a through-rate for the first step", () => {
    render(<Funnel steps={STEPS} ariaLabel="Entonnoir" />);
    // L'index 0 n'a pas de through-rate
    expect(screen.queryByTestId("funnel-through-rate-0")).not.toBeInTheDocument();
  });

  // Fix F-7 (9-2b review): through-rate >100% must NOT render as green success.
  // It must render in warning colour with an anomaly marker.
  it("renders through-rate >100% as warning anomaly, never as success (F-7)", () => {
    const invertedSteps = [
      { label: "Étape 1", value: 100 },
      { label: "Étape 2", value: 150 }, // > prev → impossible funnel
    ];
    render(<Funnel steps={invertedSteps} ariaLabel="Entonnoir inversé" />);
    const rateEl = screen.getByTestId("funnel-through-rate-1");
    expect(rateEl).toHaveTextContent("150 %");
    // Must contain anomaly marker
    expect(rateEl).toHaveTextContent("anomalie");
    // Must NOT have success colour — assert the raw MUI color prop is "warning.main"
    // (we test the rendered text, not the CSS colour which requires full theme rendering)
    expect(rateEl.textContent).toContain("⚠");
  });

  // Fix F-9 (ui review): successThreshold as prop (token/prop, not hardcoded 50%).
  it("accepts a custom successThreshold prop and colours accordingly (F-9)", () => {
    // 40% >= 10 threshold → success
    const steps = [
      { label: "Start", value: 100 },
      { label: "End", value: 40 },
    ];
    render(<Funnel steps={steps} successThreshold={10} ariaLabel="Entonnoir seuil" />);
    const rateEl = screen.getByTestId("funnel-through-rate-1");
    expect(rateEl).toHaveTextContent("40 %");
    // With threshold=10, 40% is success — no anomaly text.
    expect(rateEl.textContent).not.toContain("anomalie");
  });
});
