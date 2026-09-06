/**
 * Story 76-8 — the chrome rules the whole card family shares.
 *
 *   * ONE comment section. The shell owns it; a composition that carries two
 *     comment blocks renders the first only. « COMMENTAIRE » twice on one card
 *     was measured on `card-keywords` and `card-usertypes`.
 *   * PROVENANCE IN DOUBLE. A slug in a sentence addressed to a person is a
 *     defect (`console-presentation.md` §4): the human label carries the
 *     meaning, the token stays beside it, marked technical.
 *   * A UNIT THAT ONLY RESTATES THE LABEL IS DROPPED.
 */

import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import CardShell from "../CardShell";
import CardComposition from "../CardComposition";
import type { CardData, CardMeta, CompositionBlock } from "../types";

const META: CardMeta = {
  freshness: { last_pull: "2026-07-13T08:00:00Z", cadence_hours: 24, stale_since: null },
  provenance: {
    source_system: "google-search-console",
    pull_id: "pull_FIXTURE0000000000000",
    pull_ids: ["pull_FIXTURE0000000000000"],
  },
  alerts: [],
  card_selection: {
    chosen: "keywords",
    mode: "suggested",
    answers_question: "Comment se comportent mes requêtes ?",
    alternatives: [],
  },
};

function renderShell() {
  return render(
    <CardShell
      title="Keywords"
      meta={META}
      dateRange={{ start: "2026-06-14", end: "2026-07-13" }}
      renderedComment="Contexte manquant pour cette période."
      metricDefinitions={{
        clicks: { definition: "Nombre de clics.", unit: "clics", direction: "up_good" },
        cost: { definition: "Coût total.", unit: "EUR", direction: "down_good" },
      }}
    >
      <div data-testid="card-body">HERO</div>
    </CardShell>,
  );
}

describe("provenance is shown in double (76-8)", () => {
  it("names the source in the footer and keeps its token beside it, marked technical", () => {
    renderShell();
    const footer = screen.getByTestId("card-footer-source");
    expect(footer).toHaveTextContent("Google Search Console");
    const token = footer.querySelector("code");
    expect(token).not.toBeNull();
    expect(token!.textContent).toBe("google-search-console");
  });

  it("names the run rather than printing `pull_…` on its own", () => {
    renderShell();
    fireEvent.focus(screen.getByTestId("card-source-affordance"));
    const run = screen.getByTestId("card-source-run");
    expect(run).toHaveTextContent("Run");
    const token = screen.getByTestId("card-source-run-id");
    expect(token.tagName.toLowerCase()).toBe("code");
    expect(token.textContent).toBe("pull_FIXTURE0000000000000");
    // The system is shown the same way, on the same line pattern.
    expect(screen.getByTestId("card-source-system")).toHaveTextContent("Google Search Console");
  });

  it("says the absence rather than an empty source line", () => {
    render(
      <CardShell
        title="Keywords"
        meta={{ ...META, provenance: undefined }}
        dateRange={{ start: "2026-06-14", end: "2026-07-13" }}
      >
        <div>HERO</div>
      </CardShell>,
    );
    expect(screen.getByTestId("card-footer-source")).toHaveTextContent("—");
  });
});

describe("a unit earns its parenthesis or loses it (76-8)", () => {
  it("drops a word unit and keeps a currency in the definitions panel", () => {
    renderShell();
    fireEvent.click(screen.getByTestId("card-definitions-toggle"));
    const panel = screen.getByTestId("card-definitions-panel");
    expect(panel.textContent).not.toMatch(/Clics \(clics\)/i);
    expect(panel.textContent).toMatch(/\(EUR\)/);
  });
});

describe("one comment section per card (76-8)", () => {
  const DATA = {
    card_id: "keywords",
    card_type: "keywords",
    title: "Keywords",
    date_range: { start: "2026-06-14", end: "2026-07-13" },
    metrics: {},
    series: {},
    rendered_comment: "Le commentaire de repli.",
  } as unknown as CardData;

  it("renders the first comment block and drops a second one", () => {
    const blocks: CompositionBlock[] = [
      { type: "comment", data: { text: "Le premier commentaire." } },
      { type: "comment", data: { text: "Le second commentaire." } },
    ] as unknown as CompositionBlock[];
    render(<CardComposition blocks={blocks} data={DATA} />);
    expect(screen.getAllByTestId("composition-block-comment")).toHaveLength(1);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Le premier commentaire.");
    expect(screen.queryByText(/Le second commentaire\./)).toBeNull();
  });

  it("still renders the single comment a well-formed composition carries", () => {
    const blocks: CompositionBlock[] = [
      { type: "comment", data: { text: "Le seul commentaire." } },
    ] as unknown as CompositionBlock[];
    render(<CardComposition blocks={blocks} data={DATA} />);
    expect(screen.getAllByTestId("composition-block-comment")).toHaveLength(1);
  });

  it("does not add a second heading when the shell already owns one", () => {
    renderShell();
    const card = screen.getByTestId("card-shell");
    const headings = (card.textContent ?? "").split("Commentaire").length - 1;
    expect(headings).toBe(1);
  });
});
