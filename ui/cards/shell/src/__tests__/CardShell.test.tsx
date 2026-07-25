/**
 * CardShell chrome tests (Epic 9, Stories 9.1 + 9.2b).
 * Verifies the shared chrome: title, question subtitle, freshness badge, rendered-
 * comment slot with source affordance, definitions popover, feedback/export chrome, footer.
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import CardShell from "../CardShell";
import type { CardMeta } from "../types";

const META: CardMeta = {
  freshness: { last_pull: "2026-07-13T08:00:00Z", cadence_hours: 24, stale_since: null },
  provenance: { source_system: "google-analytics", pull_id: "pull_abc", pull_ids: ["pull_abc"] },
  alerts: [],
  card_selection: {
    chosen: "kpi",
    mode: "suggested",
    answers_question: "Comment évoluent mes KPI ?",
    alternatives: [],
  },
};

function renderShell(extra?: Partial<React.ComponentProps<typeof CardShell>>) {
  return render(
    <CardShell
      title="Synthèse KPI"
      answersQuestion="Comment évoluent mes KPI ?"
      meta={META}
      dateRange={{ start: "2026-06-13", end: "2026-07-13" }}
      renderedComment={"Sessions: 220 (google-analytics:sessions, pull_abc)\n\nContexte manquant pour cette période."}
      metricDefinitions={{
        sessions: { definition: "Nombre de séances.", unit: "séances", direction: "up_good" },
      }}
      {...extra}
    >
      <div data-testid="card-body">HERO</div>
    </CardShell>,
  );
}

describe("CardShell", () => {
  it("renders the title, question subtitle and body", () => {
    renderShell();
    expect(screen.getByTestId("card-title")).toHaveTextContent("Synthèse KPI");
    expect(screen.getByText("Comment évoluent mes KPI ?")).toBeInTheDocument();
    expect(screen.getByTestId("card-body")).toHaveTextContent("HERO");
  });

  it("renders a freshness badge from meta.freshness.last_pull", () => {
    renderShell();
    expect(screen.getByTestId("card-freshness-badge")).toHaveTextContent("MAJ 2026-07-13");
  });

  it("renders the deterministic cited comment in the comment slot", () => {
    renderShell();
    const comment = screen.getByTestId("card-rendered-comment");
    expect(comment).toHaveTextContent("Sessions: 220");
    expect(comment).toHaveTextContent("Contexte manquant");
  });

  it("reveals provenance via the source affordance on focus", () => {
    renderShell();
    const btn = screen.getByTestId("card-source-affordance");
    fireEvent.focus(btn);
    expect(screen.getByTestId("card-source-tooltip")).toHaveTextContent("google-analytics");
    expect(screen.getByTestId("card-source-tooltip")).toHaveTextContent("pull_abc");
  });

  it("toggles the definitions popover fed by metric_definitions", () => {
    renderShell();
    const toggle = screen.getByTestId("card-definitions-toggle");
    fireEvent.click(toggle);
    expect(screen.getByTestId("card-definitions-panel")).toHaveTextContent("Nombre de séances.");
  });

  it("omits the definitions popover when no definitions are provided", () => {
    renderShell({ metricDefinitions: undefined });
    expect(screen.queryByTestId("card-definitions-toggle")).not.toBeInTheDocument();
  });

  it("renders the footer with source system and date range", () => {
    renderShell();
    const footer = screen.getByTestId("card-footer");
    expect(footer).toHaveTextContent("google-analytics");
    expect(footer).toHaveTextContent("2026-06-13");
    expect(footer).toHaveTextContent("2026-07-13");
  });

  it("renders the feedback chrome row when feedbackProps is provided (Story 9.2b)", () => {
    renderShell({
      feedbackProps: {
        projectId: "proj_test",
        traceId: "trace_abc",
        reportRef: "card:kpi:2026-07-13",
        module: "card-kpi",
      },
    });
    expect(screen.getByTestId("card-chrome-row")).toBeInTheDocument();
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
    expect(screen.getByTestId("card-export-button")).toBeInTheDocument();
  });

  it("does NOT render the feedback chrome row when feedbackProps is absent (backward compat)", () => {
    renderShell({ feedbackProps: undefined });
    expect(screen.queryByTestId("card-chrome-row")).not.toBeInTheDocument();
  });

  it("feedback thumbs-up click selects rating (Story 9.2b)", () => {
    renderShell({
      feedbackProps: {
        projectId: "proj_test",
        traceId: null,
        reportRef: "card:kpi:2026-07-13",
        module: "card-kpi",
      },
    });
    const thumbsUp = screen.getByTestId("card-feedback-thumbs-up");
    fireEvent.click(thumbsUp);
    // Envoyer doit être activé
    expect(screen.getByTestId("card-feedback-submit")).not.toBeDisabled();
  });
});
