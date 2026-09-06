import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import PivotMatrix, { decodePivotMatrix } from "../pivotMatrix";

const matrix = {
  result_id: "qr_pivot",
  content_hash: "sha256_pivot",
  row_fields: ["k_campaign"],
  column_fields: ["k_day"],
  value_fields: [{ name: "m_spend", canonical_field_id: "spend", datastream_id: "ds_ads" }],
  row_keys: [["Brand"], ["Generic"]],
  column_keys: [["2026-08-01"], ["2026-08-02"]],
  cells: [
    {
      row_key: ["Brand"],
      column_key: ["2026-08-01"],
      values: { m_spend: { value: 12 } },
      contributing_rows: 2,
    },
    {
      row_key: ["Brand"],
      column_key: ["2026-08-02"],
      values: { m_spend: { value: 8 } },
      contributing_rows: 1,
    },
    {
      row_key: ["Generic"],
      column_key: ["2026-08-01"],
      values: { m_spend: { value: null, absent_reason: "no_contributing_row" } },
      contributing_rows: 0,
    },
  ],
  filters_applied: [],
  bounds: {
    response_bytes: 800,
    rows_truncated: false,
    columns_truncated: false,
  },
  row_subtotals: [
    { row_key: ["Brand"], values: { m_spend: { value: 20 } } },
    { row_key: ["Generic"], values: { m_spend: { value: null } } },
  ],
  column_subtotals: [
    { column_key: ["2026-08-01"], values: { m_spend: { value: 12 } } },
    { column_key: ["2026-08-02"], values: { m_spend: { value: 8 } } },
  ],
  // The policy is read by the projection: `both` collapses each axis AND
  // produces the corner. `rows` or `columns` alone carry no corner, because a
  // corner needs the two.
  grand_total: {
    policy: "both",
    by_column_key: [
      { column_key: ["2026-08-01"], values: { m_spend: { value: 12 } } },
      { column_key: ["2026-08-02"], values: { m_spend: { value: 8 } } },
    ],
    by_row_key: [
      { row_key: ["Brand"], values: { m_spend: { value: 20 } } },
      { row_key: ["Generic"], values: { m_spend: { value: null } } },
    ],
    overall: { m_spend: { value: 20 } },
  },
};

describe("server-owned MCP pivot matrix", () => {
  it("decodes only the exact Result identity", () => {
    const envelope = {
      schema_version: "analyze-pivot-render.v1",
      result_id: "qr_pivot",
      content_hash: "sha256_pivot",
      matrix,
    };
    expect(decodePivotMatrix(envelope, "qr_pivot", "sha256_pivot")).toEqual(matrix);
    expect(() => decodePivotMatrix(envelope, "qr_other", "sha256_pivot")).toThrow(
      /another or malformed Result/,
    );
    expect(() => decodePivotMatrix({
      ...envelope,
      matrix: { ...matrix, comparison: { contract_version: "period-comparison.v1" } },
    }, "qr_pivot", "sha256_pivot")).toThrow(/another or malformed Result/);
  });

  it("renders the server cells as an accessible horizontal pivot without recomputing", () => {
    render(<PivotMatrix matrix={matrix} />);
    const region = screen.getByRole("region", { name: "Multi-Datastream pivot matrix" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("Server-owned projection of Result qr_pivot; no browser totals.")).toBeInTheDocument();
    const rows = within(region).getAllByRole("row");
    expect(within(rows[1]!).getByText("Brand")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("12")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("8")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("20")).toBeInTheDocument();
    expect(screen.getByTestId("pivot-grand-total")).toHaveTextContent("m_spend: 20");
    // Both axes total. A matrix that totals one and not the other is a grouped
    // list wearing a pivot's name.
    const columnTotals = screen.getByTestId("pivot-column-totals");
    expect(within(columnTotals).getByText("Column total")).toBeInTheDocument();
    expect(within(columnTotals).getByText("12")).toBeInTheDocument();
    expect(within(columnTotals).getByText("8")).toBeInTheDocument();
  });

  it("prints no corner cell when only one axis was collapsed", () => {
    const { grand_total: _ignored, column_subtotals: _also, ...rowsOnly } = matrix;
    render(<PivotMatrix matrix={{
      ...rowsOnly,
      grand_total: {
        policy: "rows",
        by_column_key: [
          { column_key: ["2026-08-01"], values: { m_spend: { value: 12 } } },
          { column_key: ["2026-08-02"], values: { m_spend: { value: 8 } } },
        ],
      },
    }} />);
    // The column axis still totals — that is what `rows` asked for.
    expect(screen.getByTestId("pivot-column-totals")).toBeInTheDocument();
    // But naming one of its entries "the grand total" would name a part after
    // the whole.
    expect(screen.queryByTestId("pivot-grand-total")).toBeNull();
  });

  it("shows the exact business question and requested Skill carried by the Result", () => {
    render(<PivotMatrix matrix={matrix} context={{
      analysis_context: {
        business_domain: { name: "Growth", version_number: 4 },
        golden_question: { title: "Revenue by campaign", version_number: 3 },
        requested_skills: [{ name: "Paid media investigation", version_number: 7 }],
      },
    }} />);
    expect(screen.getByTestId("pivot-analysis-context")).toHaveTextContent(
      /Growth v4.*Revenue by campaign v3.*Paid media investigation v7/,
    );
  });

  it("renders the server-owned period comparison without recomputing deltas", () => {
    render(<PivotMatrix matrix={{
      ...matrix,
      comparison: {
        contract_version: "period-comparison.v1",
        kind: "previous_period",
        canonical_field_id: "day",
        period_field: "k_comparison_period",
        current: { start: "2026-08-01", end: "2026-08-07" },
        baseline: { start: "2026-07-25", end: "2026-07-31" },
        deltas: [
          {
            row_key: ["Brand"],
            column_key: [],
            value_field: "m_spend",
            current: { value: 125 },
            baseline: { value: 100 },
            absolute_delta: { value: 25 },
            relative_delta: { value: 0.25 },
          },
          {
            row_key: ["Generic"],
            column_key: [],
            value_field: "m_spend",
            current: { value: 10 },
            baseline: { value: 0 },
            absolute_delta: { value: 10 },
            relative_delta: { value: null, absent_reason: "baseline_zero" },
          },
        ],
      },
    }} />);

    expect(screen.getByTestId("period-comparison")).toHaveTextContent(
      /Current 2026-08-01 to 2026-08-07; baseline 2026-07-25 to 2026-07-31/,
    );
    const deltas = screen.getByRole("region", { name: "Period comparison deltas" });
    expect(within(deltas).getByText(/25\s*%/)).toBeInTheDocument();
    expect(within(deltas).getByText("baseline is zero")).toBeInTheDocument();
  });
});
