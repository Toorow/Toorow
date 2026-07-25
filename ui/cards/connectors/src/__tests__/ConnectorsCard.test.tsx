/**
 * Connectors card tests (Epic 9, Story 9.8).
 *
 * - Renders fixture without throwing.
 * - Table renders 3 rows with French connector names and statuses.
 * - Status cells: correct French labels + status dot present for each status bucket.
 * - Comment block renders (cited comment text).
 * - Designed empty state: "Aucun connecteur configuré" when rows=[].
 * - Feedback bar present (CardShell chrome).
 * - Partial fixture (single row) renders without crash.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import {
  FIXTURE_ENVELOPE,
  FIXTURE_ENVELOPE_EMPTY,
  FIXTURE_ENVELOPE_PARTIAL,
} from "../fixture";
import type { CardEnvelope } from "@toorow/card-shell";

// ---------------------------------------------------------------------------
// Happy-path fixture renders
// ---------------------------------------------------------------------------

describe("Connectors card — fixture renders without throwing", () => {
  it("renders the card title", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Connecteurs");
  });

  it("renders the connectors table", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("connectors-table")).toBeInTheDocument();
  });

  it("renders 3 connector rows", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const rows = screen.getAllByTestId("connectors-table-row");
    expect(rows).toHaveLength(3);
  });

  it("renders connector names in the table", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("google-analytics")).toBeInTheDocument();
    expect(screen.getByText("google-ads")).toBeInTheDocument();
    expect(screen.getByText("meta-ads")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Status cells: French labels + status dots
// ---------------------------------------------------------------------------

describe("Connectors card — status cells", () => {
  it("renders French status labels for all status buckets", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const statusCells = screen.getAllByTestId("connector-status-cell");
    const labels = statusCells.map((c) => c.dataset.status);
    expect(labels).toContain("Opérationnel");
    expect(labels).toContain("Obsolète");
    expect(labels).toContain("En erreur");
  });

  it("renders a status dot for each status cell", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const dots = screen.getAllByTestId("connector-status-dot");
    // 3 connectors -> 3 dots
    expect(dots).toHaveLength(3);
  });

  it("renders status label text for Opérationnel", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const labels = screen.getAllByTestId("connector-status-label");
    const operational = labels.find((l) => l.textContent === "Opérationnel");
    expect(operational).toBeInTheDocument();
  });

  it("renders status label text for Obsolète", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const labels = screen.getAllByTestId("connector-status-label");
    const obsolete = labels.find((l) => l.textContent === "Obsolète");
    expect(obsolete).toBeInTheDocument();
  });

  it("renders status label text for En erreur", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    const labels = screen.getAllByTestId("connector-status-label");
    const error = labels.find((l) => l.textContent === "En erreur");
    expect(error).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Last extract and flows columns
// ---------------------------------------------------------------------------

describe("Connectors card — last_extract and flows columns", () => {
  it("renders last_extract dates", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("2026-07-14")).toBeInTheDocument();
    expect(screen.getByText("2026-07-01")).toBeInTheDocument();
  });

  it("renders flows as comma-separated names", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByText("rapport-seo, rapport-audience")).toBeInTheDocument();
    expect(screen.getByText("rapport-performance")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Comment block
// ---------------------------------------------------------------------------

describe("Connectors card — comment block", () => {
  it("renders the cited comment from the composition", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("composition-comment")).toBeInTheDocument();
    expect(screen.getByTestId("composition-comment")).toHaveTextContent(
      "google-ads",
    );
  });
});

// ---------------------------------------------------------------------------
// Feedback chrome (CardShell)
// ---------------------------------------------------------------------------

describe("Connectors card — feedback chrome", () => {
  it("renders the feedback bar", () => {
    render(<App envelope={FIXTURE_ENVELOPE} />);
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Designed empty state: FIXTURE_ENVELOPE_EMPTY (no connectors)
// ---------------------------------------------------------------------------

describe("Connectors card — designed empty state (FIXTURE_ENVELOPE_EMPTY)", () => {
  it("renders the designed empty state text when rows are empty", () => {
    render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />);
    expect(screen.getByTestId("connectors-table-empty")).toBeInTheDocument();
    expect(screen.getByTestId("connectors-table-empty")).toHaveTextContent(
      "Aucun connecteur configuré",
    );
  });

  it("shows a muted hint in the empty state", () => {
    render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />);
    expect(screen.getByTestId("connectors-table-empty")).toHaveTextContent(
      "Configurez un datastream",
    );
  });

  it("does NOT render the table when rows are empty", () => {
    render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />);
    expect(screen.queryByTestId("connectors-table")).not.toBeInTheDocument();
  });

  it("card title still renders in empty state (never blank)", () => {
    render(<App envelope={FIXTURE_ENVELOPE_EMPTY} />);
    expect(screen.getByTestId("card-title")).toHaveTextContent("Connecteurs");
  });
});

// ---------------------------------------------------------------------------
// Partial fixture (single connector)
// ---------------------------------------------------------------------------

describe("Connectors card — partial fixture (FIXTURE_ENVELOPE_PARTIAL)", () => {
  it("renders without crash with a single connector row", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PARTIAL} />);
    expect(screen.getByTestId("connectors-table")).toBeInTheDocument();
    const rows = screen.getAllByTestId("connectors-table-row");
    expect(rows).toHaveLength(1);
  });

  it("renders the comment block in the partial fixture", () => {
    render(<App envelope={FIXTURE_ENVELOPE_PARTIAL} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent(
      "Tous les connecteurs sont opérationnels.",
    );
  });
});

// ---------------------------------------------------------------------------
// Unknown / missing status value -> Inconnu fallback
// ---------------------------------------------------------------------------

describe("Connectors card — unknown status fallback", () => {
  it("renders 'Inconnu' when status value is empty or unrecognised", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "table",
            title: "Connecteurs",
            binding: {},
            data: {
              columns: [
                { key: "connector",    label: "Connecteur", numeric: false, sortable: true },
                { key: "status",       label: "Statut",     numeric: false, sortable: true },
                { key: "last_extract", label: "Dernier extrait", numeric: false, sortable: true },
                { key: "flows",        label: "Flows alimentés", numeric: false, sortable: false },
              ],
              rows: [
                {
                  connector:    "test-connector",
                  status:       "",
                  last_extract: "2026-07-10",
                  flows:        "rapport-test",
                },
              ],
            },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    const labels = screen.getAllByTestId("connector-status-label");
    // Empty string -> "Inconnu"
    expect(labels[0].textContent).toBe("Inconnu");
  });
});

// ---------------------------------------------------------------------------
// Table block absent -> empty state (not blank)
// ---------------------------------------------------------------------------

describe("Connectors card — no table block in composition", () => {
  it("renders connectors-no-table-block when composition has no table block (F-5 fix)", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "comment",
            binding: {},
            data: { text: "Aucun données disponibles." },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    // F-5 fix: distinct testid for no-table-block vs empty-rows cases.
    expect(screen.getByTestId("connectors-no-table-block")).toBeInTheDocument();
    // The old duplicate "connectors-table-empty" testid no longer appears here.
    expect(screen.queryByTestId("connectors-table-empty")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// F-5: Two table blocks — composition order preserved, both render
// ---------------------------------------------------------------------------

describe("Connectors card — two table blocks in composition (F-5)", () => {
  it("renders both table blocks in composition order", () => {
    const env: CardEnvelope = {
      ...FIXTURE_ENVELOPE,
      data: {
        ...FIXTURE_ENVELOPE.data,
        composition: [
          {
            type: "table",
            title: "Table A",
            binding: {},
            data: {
              columns: [
                { key: "connector", label: "Connecteur", numeric: false, sortable: true },
                { key: "status",    label: "Statut",     numeric: false, sortable: true },
                { key: "last_extract", label: "Dernier extrait", numeric: false, sortable: true },
                { key: "flows",     label: "Flows alimentés", numeric: false, sortable: false },
              ],
              rows: [
                { connector: "ga4", status: "Opérationnel", last_extract: "2026-07-14", flows: "rapport-seo" },
              ],
            },
          },
          {
            type: "table",
            title: "Table B",
            binding: {},
            data: {
              columns: [
                { key: "connector", label: "Connecteur", numeric: false, sortable: true },
                { key: "status",    label: "Statut",     numeric: false, sortable: true },
                { key: "last_extract", label: "Dernier extrait", numeric: false, sortable: true },
                { key: "flows",     label: "Flows alimentés", numeric: false, sortable: false },
              ],
              rows: [
                { connector: "meta-ads", status: "En erreur", last_extract: "2026-07-10", flows: "rapport-ads" },
              ],
            },
          },
        ],
      },
    };
    render(<App envelope={env} />);
    // Both tables must render.
    const tables = screen.getAllByTestId("connectors-table");
    expect(tables).toHaveLength(2);
    // First table: ga4 row; second table: meta-ads row — order preserved.
    expect(tables[0]).toHaveTextContent("ga4");
    expect(tables[1]).toHaveTextContent("meta-ads");
  });
});
