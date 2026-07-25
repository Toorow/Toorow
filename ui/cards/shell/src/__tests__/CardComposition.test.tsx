/**
 * CardComposition tests (Stories 9.2c + 9.3-9.6) — each block type driven from block.data,
 * empty-safe, Graph+Table, unknown block, never throws.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import CardComposition from "../CardComposition";
import type { CompositionBlock, CardData } from "../types";

// Minimal CardData fixture for composition rendering.
const CARD_DATA: CardData = {
  card_id: "kpi",
  card_type: "kpi",
  title: "Synthèse KPI",
  answers_question: "Comment évoluent mes KPI ?",
  date_range: { start: "2026-06-14", end: "2026-07-13" },
  connectors: ["google-analytics"],
  metrics: {
    sessions: { value: 42150, delta: 3820, delta_pct: 9.97 },
    conversions: { value: 1284, delta: 210, delta_pct: 19.55 },
  },
  series: {
    sessions: [
      { date: "2026-07-01", value: 1400 },
      { date: "2026-07-02", value: 1450 },
    ],
    conversions: [
      { date: "2026-07-01", value: 42 },
      { date: "2026-07-02", value: 45 },
    ],
  },
  rendered_comment:
    "Sessions: 42 150 (+9,97 %) — Conversions: 1 284 (+19,55 %).\nContexte manquant.",
};

const KPI_COMPOSITION: CompositionBlock[] = [
  { type: "kpi_row", binding: { metrics: "*" } },
  { type: "comment", binding: {} },
];

// ---------------------------------------------------------------------------
// kpi_row block (backward-compat path: no block.data -> uses data.metrics)
// ---------------------------------------------------------------------------

describe("CardComposition — kpi_row block (legacy, no block.data)", () => {
  it("renders the kpi_row block with hero values from data.metrics", () => {
    render(<CardComposition blocks={KPI_COMPOSITION} data={CARD_DATA} />);
    expect(screen.getByTestId("card-composition")).toBeInTheDocument();
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-kpi-row")).toBeInTheDocument();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(2);
    const values = screen.getAllByTestId("composition-kpi-value");
    const texts = values.map((v) => v.textContent);
    expect(texts.some((t) => t?.includes("42"))).toBe(true);
  });

  it("renders delta for each metric (legacy path)", () => {
    render(<CardComposition blocks={KPI_COMPOSITION} data={CARD_DATA} />);
    const deltas = screen.getAllByTestId("composition-kpi-delta");
    expect(deltas.length).toBeGreaterThan(0);
    const texts = deltas.map((d) => d.textContent);
    expect(texts.some((t) => t?.includes("+") && t?.includes("9") && t?.includes("%"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// kpi_row block driven by block.data (9.3-9.6 server payload shape)
// ---------------------------------------------------------------------------

describe("CardComposition — kpi_row block (block.data path, 9.3-9.6)", () => {
  it("renders hero values from block.data.metrics[]", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "kpi_row",
        binding: { metrics: ["clicks", "impressions"] },
        data: {
          metrics: [
            { metric: "clicks", value: 3500, delta: 200, delta_pct: 6.1, direction: "up_good" },
            { metric: "impressions", value: 82000, delta: -500, delta_pct: -0.6, direction: "up_good" },
          ],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-kpi-row")).toBeInTheDocument();
    const tiles = screen.getAllByTestId("composition-kpi-tile");
    expect(tiles.length).toBe(2);
    const clicksTile = tiles.find((t) => t.getAttribute("data-metric") === "clicks")!;
    expect(clicksTile).toBeInTheDocument();
    expect(clicksTile.querySelector("[data-testid='composition-kpi-value']")?.textContent).toMatch(/3/);
  });

  it("renders direction-aware delta colors from block.data", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "kpi_row",
        binding: { metrics: ["cpa"] },
        data: {
          metrics: [
            { metric: "cpa", value: 30, delta: -5, delta_pct: -14.3, direction: "down_good" },
          ],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    const delta = screen.getByTestId("composition-kpi-delta");
    expect(delta.textContent).toContain("-14.3");
  });

  it("renders empty state when block.data.metrics is empty", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "kpi_row",
        binding: { metrics: [] },
        data: { metrics: [] },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-kpi-row-empty")).toBeInTheDocument();
  });

  it("renders kpi_row empty state when data.metrics is also empty (legacy fallback)", () => {
    const data = { ...CARD_DATA, metrics: {}, series: {} };
    render(<CardComposition blocks={[{ type: "kpi_row", binding: { metrics: "*" } }]} data={data} />);
    expect(screen.getByTestId("composition-kpi-row-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// comment block
// ---------------------------------------------------------------------------

describe("CardComposition — comment block", () => {
  it("renders the rendered_comment from data (legacy)", () => {
    render(<CardComposition blocks={KPI_COMPOSITION} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    expect(screen.getByTestId("composition-comment")).toBeInTheDocument();
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Sessions");
  });

  it("renders comment text from block.data.text (9.3-9.6)", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "comment",
        binding: {},
        data: { text: "Clics en hausse de 6 % vs période précédente (google-search-console)." },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Clics en hausse");
  });

  it("renders empty state when comment text is empty", () => {
    const data = { ...CARD_DATA, rendered_comment: "" };
    render(<CardComposition blocks={[{ type: "comment", binding: {} }]} data={data} />);
    expect(screen.getByTestId("composition-comment-empty")).toBeInTheDocument();
  });

  it("block.data.text takes precedence over rendered_comment", () => {
    const blocks: CompositionBlock[] = [
      { type: "comment", binding: {}, data: { text: "Texte du bloc." } },
    ];
    render(<CardComposition blocks={blocks} data={{ ...CARD_DATA, rendered_comment: "Texte de data." }} />);
    expect(screen.getByTestId("composition-comment")).toHaveTextContent("Texte du bloc.");
  });
});

// ---------------------------------------------------------------------------
// bar block driven by block.data (9.3 keywords, 9.5 usertypes)
// ---------------------------------------------------------------------------

describe("CardComposition — bar block (block.data)", () => {
  it("renders bars from block.data.bars", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "bar",
        binding: { metrics: "clicks", dimensions: ["query"] },
        title: "Top requêtes",
        data: {
          orientation: "horizontal",
          dimension: "query",
          bars: [
            { label: "bottes de randonnée", value: 1200 },
            { label: "chaussures trail", value: 850 },
            { label: "semelles ortho", value: 400 },
          ],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-bar")).toBeInTheDocument();
    expect(screen.getByTestId("bar-chart")).toBeInTheDocument();
  });

  it("renders bar empty state when block.data.bars is empty", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "bar",
        binding: { metrics: "clicks" },
        data: { orientation: "horizontal", dimension: null, bars: [] },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
  });

  it("renders bar empty state when block has no data", () => {
    render(<CardComposition blocks={[{ type: "bar", binding: { metrics: "clicks" } }]} data={CARD_DATA} />);
    expect(screen.getByTestId("bar-chart-empty")).toBeInTheDocument();
  });

  it("uses vertical variant when orientation is vertical", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "bar",
        binding: { metrics: "active_users", dimensions: ["country"] },
        data: {
          orientation: "vertical",
          dimension: "country",
          bars: [
            { label: "FR", value: 5000 },
            { label: "BE", value: 1200 },
          ],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("bar-chart")).toBeInTheDocument();
    expect(screen.getByTestId("bar-chart").getAttribute("data-variant")).toBe("vertical");
  });
});

// ---------------------------------------------------------------------------
// donut block driven by block.data (9.4 conversions, 9.5 usertypes)
// ---------------------------------------------------------------------------

describe("CardComposition — donut block (block.data)", () => {
  it("renders donut from block.data.slices", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "donut",
        binding: { metrics: "conversions", dimensions: ["connector"] },
        title: "Par source",
        data: {
          total: 320,
          dimension: "connector",
          slices: [
            { label: "google-ads", value: 200, pct: 62.5 },
            { label: "meta-ads", value: 80, pct: 25.0 },
            { label: "organic", value: 40, pct: 12.5 },
          ],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-donut")).toBeInTheDocument();
    expect(screen.getByTestId("donut")).toBeInTheDocument();
    expect(screen.getByTestId("donut-legend")).toBeInTheDocument();
  });

  it("renders donut empty state when slices is empty", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "donut",
        binding: { metrics: "conversions" },
        data: { total: 0, dimension: null, slices: [] },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
  });

  it("renders donut empty state when block has no data", () => {
    render(<CardComposition blocks={[{ type: "donut", binding: { metrics: "conversions" } }]} data={CARD_DATA} />);
    expect(screen.getByTestId("donut-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// gauge block driven by block.data (9.4 conversions)
// ---------------------------------------------------------------------------

describe("CardComposition — gauge block (block.data)", () => {
  it("renders gauge from block.data", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "gauge",
        binding: { numerator: "cost", denominator: "conversions", direction: "down_good" },
        title: "CPA vs objectif",
        data: {
          value: 32.5,
          target: 50.0,
          target_source: "default",
          unit: "EUR",
          direction: "down_good",
          label: "CPA vs objectif",
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-gauge")).toBeInTheDocument();
    expect(screen.getByTestId("gauge")).toBeInTheDocument();
  });

  it("renders gauge empty state when value is null (zero-division)", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "gauge",
        binding: { numerator: "cost", denominator: "conversions" },
        data: {
          value: null,
          target: 50.0,
          target_source: "default",
          unit: "EUR",
          direction: "down_good",
          label: "CPA vs objectif",
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("gauge-empty")).toBeInTheDocument();
  });

  it("renders gauge empty state when block has no data", () => {
    render(<CardComposition blocks={[{ type: "gauge", binding: { metrics: "cpa" } }]} data={CARD_DATA} />);
    expect(screen.getByTestId("gauge-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// funnel block driven by block.data (9.6 journey)
// ---------------------------------------------------------------------------

describe("CardComposition — funnel block (block.data)", () => {
  it("renders funnel steps from block.data.steps", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "funnel",
        binding: { steps: ["sessions", "active_users", "conversions"] },
        title: "Entonnoir de conversion",
        data: {
          steps: [
            { label: "sessions", value: 10000, rate: 1.0 },
            { label: "active_users", value: 7500, rate: 0.75 },
            { label: "conversions", value: 500, rate: 0.067 },
          ],
          overall_rate: 0.05,
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-funnel")).toBeInTheDocument();
    expect(screen.getByTestId("funnel")).toBeInTheDocument();
  });

  it("renders funnel empty state when steps is empty", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "funnel",
        binding: { steps: [] },
        data: { steps: [], overall_rate: null },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
  });

  it("renders funnel empty state when block has no data", () => {
    render(<CardComposition blocks={[{ type: "funnel", binding: { metrics: "*" } }]} data={CARD_DATA} />);
    expect(screen.getByTestId("funnel-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// line block driven by block.data
// ---------------------------------------------------------------------------

describe("CardComposition — line block (block.data)", () => {
  it("renders LineChart from block.data.series", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "line",
        binding: { metrics: "sessions" },
        title: "Courbe sessions",
        data: {
          series: [
            {
              name: "sessions",
              points: [
                { x: "2026-07-01", y: 1400 },
                { x: "2026-07-02", y: 1450 },
                { x: "2026-07-03", y: 1380 },
              ],
            },
          ],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-line")).toBeInTheDocument();
    expect(screen.getByTestId("line-chart")).toBeInTheDocument();
  });

  it("renders line empty state when series is empty", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "line",
        binding: { metrics: "sessions" },
        data: { series: [] },
      },
    ];
    const data = { ...CARD_DATA, series: {} };
    render(<CardComposition blocks={blocks} data={data} />);
    expect(screen.getByTestId("line-chart-empty")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// table block (Graph+Table composition)
// ---------------------------------------------------------------------------

describe("CardComposition — table block (Graph+Table)", () => {
  it("renders a table block alongside a kpi_row (Graph+Table composition)", () => {
    const tableBlock: CompositionBlock = {
      type: "table",
      binding: { metrics: "sessions" },
      title: "Détail sessions",
      data: {
        columns: [
          { key: "date", label: "Date", numeric: false },
          { key: "value", label: "Sessions", numeric: true },
        ],
        rows: [
          { date: "2026-07-01", value: 1400 },
          { date: "2026-07-02", value: 1450 },
        ],
      },
    };
    const blocks: CompositionBlock[] = [
      { type: "kpi_row", binding: { metrics: "*" } },
      tableBlock,
      { type: "comment", binding: {} },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-table")).toBeInTheDocument();
    expect(screen.getByTestId("data-table")).toBeInTheDocument();
    const rows = screen.getAllByTestId("data-table-row");
    expect(rows.length).toBe(2);
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
  });

  it("renders DataTable empty state when table block has no rows", () => {
    const tableBlock: CompositionBlock = {
      type: "table",
      binding: { metrics: "sessions" },
      data: { columns: [], rows: [] },
    };
    render(<CardComposition blocks={[tableBlock]} data={CARD_DATA} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("renders DataTable empty state when table block has no data", () => {
    render(<CardComposition blocks={[{ type: "table", binding: {} }]} data={CARD_DATA} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("renders a full bar+table card (keywords composition)", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "kpi_row",
        binding: {},
        data: {
          metrics: [
            { metric: "clicks", value: 3500, delta: 200, delta_pct: 6.1, direction: "up_good" },
            { metric: "impressions", value: 82000, delta: -500, delta_pct: -0.6, direction: "up_good" },
          ],
        },
      },
      {
        type: "bar",
        binding: { metrics: "clicks", dimensions: ["query"] },
        title: "Top requêtes par clics",
        data: {
          orientation: "horizontal",
          dimension: "query",
          bars: [
            { label: "bottes de randonnée", value: 1200 },
            { label: "chaussures trail", value: 850 },
          ],
        },
      },
      {
        type: "table",
        binding: {},
        title: "Détail requêtes",
        data: {
          columns: [
            { key: "_dim", label: "Requête", numeric: false },
            { key: "clicks", label: "Clics", numeric: true },
            { key: "impressions", label: "Impressions", numeric: true },
          ],
          rows: [
            { _dim: "bottes de randonnée", clicks: 1200, impressions: 18000 },
            { _dim: "chaussures trail", clicks: 850, impressions: 12000 },
          ],
        },
      },
      {
        type: "comment",
        binding: {},
        data: { text: "« bottes de randonnée » est la requête leader avec 1 200 clics." },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-block-bar")).toBeInTheDocument();
    expect(screen.getByTestId("composition-table")).toBeInTheDocument();
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
    expect(screen.getAllByTestId("data-table-row").length).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// F-3 (9-3-to-9-6 ui review): funnel overall_rate → NumberHero headline
// ---------------------------------------------------------------------------

describe("CardComposition — funnel overall_rate NumberHero (F-3)", () => {
  it("renders the overall_rate as a NumberHero headline above the funnel", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "funnel",
        binding: { steps: ["sessions", "active_users", "conversions"] },
        title: "Entonnoir de conversion",
        data: {
          steps: [
            { label: "Sessions", value: 42150, rate: 1.0 },
            { label: "Utilisateurs actifs", value: 31820, rate: 0.755 },
            { label: "Conversions", value: 1284, rate: 0.040 },
          ],
          overall_rate: 0.030,
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    const hero = screen.getByTestId("funnel-overall-rate");
    expect(hero).toBeInTheDocument();
    // 0.030 * 100 = 3.0 % — rendered as "3 %" or "3,0 %" in fr-FR
    const heroValue = screen.getByTestId("funnel-overall-rate-value");
    expect(heroValue.textContent).toMatch(/3/);
    expect(heroValue.textContent).toMatch(/%/);
    // Label must say "Taux de conversion global"
    expect(hero.textContent).toContain("Taux de conversion global");
  });

  it("does NOT render the overall_rate hero when overall_rate is null", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "funnel",
        binding: {},
        data: {
          steps: [{ label: "Sessions", value: 100, rate: 1.0 }],
          overall_rate: null,
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.queryByTestId("funnel-overall-rate")).not.toBeInTheDocument();
  });

  it("does NOT render the overall_rate hero when overall_rate is absent (no field)", () => {
    const blocks: CompositionBlock[] = [
      {
        type: "funnel",
        binding: {},
        data: {
          steps: [{ label: "Sessions", value: 100, rate: 1.0 }],
        },
      },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.queryByTestId("funnel-overall-rate")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// unknown block type + resilience
// ---------------------------------------------------------------------------

describe("CardComposition — unknown block type", () => {
  it("renders the designed unknown-block state for unrecognised type", () => {
    const blocks: CompositionBlock[] = [
      { type: "heatmap" as never, binding: { metrics: "*" } },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-unknown-block")).toBeInTheDocument();
    expect(screen.getByTestId("composition-unknown-block")).toHaveTextContent("heatmap");
  });

  it("never throws when blocks is empty", () => {
    render(<CardComposition blocks={[]} data={CARD_DATA} />);
    expect(screen.getByTestId("card-composition-empty")).toBeInTheDocument();
  });

  it("renders other blocks even when one is unknown (resilience)", () => {
    const blocks: CompositionBlock[] = [
      { type: "kpi_row", binding: { metrics: "*" } },
      { type: "unknown_future_block" as never, binding: { metrics: "*" } },
      { type: "comment", binding: {} },
    ];
    render(<CardComposition blocks={blocks} data={CARD_DATA} />);
    expect(screen.getByTestId("composition-block-kpi_row")).toBeInTheDocument();
    expect(screen.getByTestId("composition-unknown-block")).toBeInTheDocument();
    expect(screen.getByTestId("composition-block-comment")).toBeInTheDocument();
  });
});
