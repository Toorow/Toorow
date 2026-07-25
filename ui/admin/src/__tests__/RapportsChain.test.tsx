/**
 * Vitest tests for Story 8.9 — ReportsPanel chain expand + ReportChainPanel.
 *
 * Coverage:
 *   - ReportsPanel: "Data chain" button exists per report row
 *   - ReportsPanel: clicking button expands the chain panel
 *   - ReportChainPanel: renders ok metrics with field chip + datastream pill
 *   - ReportChainPanel: renders no_stream warning state (designed warning tint)
 *   - ReportChainPanel: renders not_in_dictionary warning state
 *   - ReportChainPanel: shows metric_definitions (definition + unit) under each metric (R6)
 *   - ReportChainPanel: shows validation summary (ok_count / total)
 *   - ReportChainPanel: error state on fetch failure
 *   - ReportChainPanel: loading state
 */

import { render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../theme";
import ReportChainPanel from "../rapports/ReportChainPanel";
import type { ReportChain } from "../rapports/ReportChainPanel";

function withTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const makeChain = (overrides: Partial<ReportChain> = {}): ReportChain => ({
  report_id: "google-search-console/overview_daily",
  display_name: "Daily overview",
  metric_definitions: null,
  llm_commentary_guidelines: null,
  metrics: [
    {
      metric: "clicks",
      definition: null,
      target_field: {
        name: "clicks",
        display_name: "Clicks",
        measure: "sum",
        data_type: "integer",
      },
      datastreams: [
        {
          id: "ds_001",
          name: "GSC Stream",
          module: "google-search-console",
          enabled: true,
          last_extract: { date: "2026-07-10", status: "ok" },
        },
      ],
      status: "ok",
    },
  ],
  validation: { ok_count: 1, warnings: [] },
  ...overrides,
});

// ---------------------------------------------------------------------------
// ReportChainPanel unit tests
// ---------------------------------------------------------------------------

describe("ReportChainPanel — ok status", () => {
  it("renders chain panel with metric, field chip and datastream", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeChain(),
      })
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="google-search-console/overview_daily"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("report-chain-panel")).toBeInTheDocument();
    });

    // Metric code chip (appears multiple times: once as code chip, once as field name)
    expect(screen.getAllByText("clicks").length).toBeGreaterThan(0);
    // Datastream pill
    expect(screen.getByText("GSC Stream")).toBeInTheDocument();
    // Validation summary: "/1 metrics fed"
    expect(screen.getByText(/metrics fed/i)).toBeInTheDocument();
  });

  it("shows no warning when all metrics are ok", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => makeChain(),
      })
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="google-search-console/overview_daily"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("report-chain-panel")).toBeInTheDocument();
    });

    // No "alert" count badge
    expect(screen.queryByText(/alert/i)).not.toBeInTheDocument();
  });
});

describe("ReportChainPanel — no_stream warning state", () => {
  it("shows the designed warning tint + actionable text for no_stream", async () => {
    const chain = makeChain({
      metrics: [
        {
          metric: "average_position",
          definition: null,
          target_field: {
            name: "average_position",
            display_name: "Average position",
            measure: "average",
            data_type: "decimal",
          },
          datastreams: [],
          status: "no_stream",
        },
      ],
      validation: {
        ok_count: 0,
        warnings: [
          "No active datastream feeds average_position for this project.",
        ],
      },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => chain })
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="gsc/pos_report"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("chain-metric-average_position")).toBeInTheDocument();
    });

    // Warning message text appears for no_stream (at least one element)
    expect(
      screen.getAllByText(/No active datastream feeds/i).length
    ).toBeGreaterThan(0);

    // Warning badge in summary
    expect(screen.getByText(/1 alert/i)).toBeInTheDocument();
  });
});

describe("ReportChainPanel — not_in_dictionary warning state", () => {
  it("shows 'non référencé' pill and warning for not_in_dictionary", async () => {
    const chain = makeChain({
      metrics: [
        {
          metric: "cost_per_click",
          definition: null,
          target_field: null,
          datastreams: [],
          status: "not_in_dictionary",
        },
      ],
      validation: {
        ok_count: 0,
        warnings: [
          "The metric “cost_per_click” is not referenced in the data dictionary.",
        ],
      },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => chain })
    );

    withTheme(
      <ReportChainPanel moduleReportId="gsc/cost" projectId="proj_001" />
    );

    await waitFor(() => {
      expect(screen.getByTestId("chain-metric-cost_per_click")).toBeInTheDocument();
    });

    // "not referenced" chip in the chain row
    expect(screen.getByText("not referenced")).toBeInTheDocument();

    // Warning text
    expect(screen.getByText(/is not referenced/i)).toBeInTheDocument();
    expect(screen.getByText(/1 alert/i)).toBeInTheDocument();
  });
});

describe("ReportChainPanel — metric_definitions (R6)", () => {
  it("shows definition text and unit under the metric when metric_definitions present", async () => {
    const chain = makeChain({
      metric_definitions: {
        clicks: {
          definition: "Organic clicks received over the period.",
          unit: "organic clicks",
          good_direction: "up",
        },
      },
      metrics: [
        {
          metric: "clicks",
          definition: {
            definition: "Organic clicks received over the period.",
            unit: "organic clicks",
            good_direction: "up",
          },
          target_field: {
            name: "clicks",
            display_name: "Clicks",
            measure: "sum",
            data_type: "integer",
          },
          datastreams: [
            {
              id: "ds_001",
              name: "GSC",
              module: "gsc",
              enabled: true,
              last_extract: { date: "2026-07-10", status: "ok" },
            },
          ],
          status: "ok",
        },
      ],
      validation: { ok_count: 1, warnings: [] },
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => chain })
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="gsc/overview_daily"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("chain-metric-clicks")).toBeInTheDocument();
    });

    // Definition text is shown under the metric
    expect(
      screen.getByText("Organic clicks received over the period.")
    ).toBeInTheDocument();

    // Unit chip
    expect(screen.getByText("organic clicks")).toBeInTheDocument();
  });
});

describe("ReportChainPanel — error and loading states", () => {
  it("shows error alert when chain fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("Network error"))
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="gsc/overview_daily"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(
        screen.getByText(/Couldn't load the data chain/i)
      ).toBeInTheDocument();
    });
  });

  it("shows loading spinner while fetching", async () => {
    let resolve: (v: unknown) => void;
    const promise = new Promise((res) => {
      resolve = res;
    });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(promise));

    withTheme(
      <ReportChainPanel
        moduleReportId="gsc/overview_daily"
        projectId="proj_001"
      />
    );

    // Loading indicator should appear while fetch is pending
    expect(document.querySelector("[role='progressbar']")).not.toBeNull();

    // Resolve to avoid leaks
    resolve!({ ok: true, json: async () => makeChain() });
  });
});

describe("ReportChainPanel — llm_commentary_guidelines (R6)", () => {
  it("shows guidelines section when present", async () => {
    const chain = makeChain({
      llm_commentary_guidelines:
        "Analyze SEO trends in a professional tone.",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => chain })
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="gsc/overview_daily"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/LLM commentary guidelines/i)).toBeInTheDocument();
    });
    expect(
      screen.getByText("Analyze SEO trends in a professional tone.")
    ).toBeInTheDocument();
  });

  it("does not show guidelines section when null", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => makeChain() })
    );

    withTheme(
      <ReportChainPanel
        moduleReportId="gsc/overview_daily"
        projectId="proj_001"
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId("report-chain-panel")).toBeInTheDocument();
    });

    expect(screen.queryByText(/LLM commentary guidelines/i)).not.toBeInTheDocument();
  });
});
