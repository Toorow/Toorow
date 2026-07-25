/**
 * DataTable tests (Story 9.2c) — columns, rows, sort, empty state, aria.
 */

import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import DataTable from "../DataTable";
import type { DataTableColumn } from "../types";

const COLUMNS: DataTableColumn[] = [
  { key: "query", label: "Requête", numeric: false, sortable: true },
  { key: "clicks", label: "Clics", numeric: true, sortable: true },
  { key: "impressions", label: "Impressions", numeric: true, sortable: false },
];

const ROWS = [
  { query: "chaussures running", clicks: 420, impressions: 8500 },
  { query: "baskets trail", clicks: 85, impressions: 3200 },
  { query: "semelles orthopédiques", clicks: 310, impressions: 5100 },
];

describe("DataTable — rendu", () => {
  it("renders the table with role=table and aria-label", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} ariaLabel="Test tableau" />);
    expect(screen.getByRole("table", { name: "Test tableau" })).toBeInTheDocument();
  });

  it("renders column headers", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    expect(screen.getByTestId("data-table-header")).toBeInTheDocument();
    expect(screen.getByTestId("data-table-header")).toHaveTextContent("Requête");
    expect(screen.getByTestId("data-table-header")).toHaveTextContent("Clics");
    expect(screen.getByTestId("data-table-header")).toHaveTextContent("Impressions");
  });

  it("renders each row", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    const rows = screen.getAllByTestId("data-table-row");
    expect(rows).toHaveLength(3);
  });

  it("right-aligns numeric cell values (tabular-nums class pattern)", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    // Clicks column: numeric -> first row value 420 formatted as fr-FR
    const clicksCells = screen.getAllByTestId("cell-clicks");
    expect(clicksCells[0]).toHaveTextContent("420");
  });

  it("formats numeric values with fr-FR locale", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} />);
    const cells = screen.getAllByTestId("cell-impressions");
    // 8500 in fr-FR: "8 500"
    expect(cells[0]).toHaveTextContent("8 500");
  });

  it("respects maxRows", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} maxRows={2} />);
    expect(screen.getAllByTestId("data-table-row")).toHaveLength(2);
  });

  it("renders null/undefined cells as em dash", () => {
    const rows = [{ query: null, clicks: undefined, impressions: 100 }];
    render(<DataTable columns={COLUMNS} rows={rows as never} />);
    const queryCells = screen.getAllByTestId("cell-query");
    expect(queryCells[0]).toHaveTextContent("—");
  });
});

describe("DataTable — tri", () => {
  it("sorts ascending on sortable column header click", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} sortable />);
    // Click 'Clics' header to sort ascending
    const header = screen.getByTestId("data-table-header");
    const clicksHeader = Array.from(header.querySelectorAll("[role='columnheader']")).find(
      (el) => el.textContent?.includes("Clics"),
    );
    expect(clicksHeader).toBeDefined();
    fireEvent.click(clicksHeader!);
    const cells = screen.getAllByTestId("cell-clicks");
    // After sort asc: 85, 310, 420
    expect(cells[0]).toHaveTextContent("85");
    expect(cells[1]).toHaveTextContent("310");
    expect(cells[2]).toHaveTextContent("420");
  });

  it("sorts descending on second click", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} sortable />);
    const header = screen.getByTestId("data-table-header");
    const clicksHeader = Array.from(header.querySelectorAll("[role='columnheader']")).find(
      (el) => el.textContent?.includes("Clics"),
    );
    fireEvent.click(clicksHeader!);
    fireEvent.click(clicksHeader!);
    const cells = screen.getAllByTestId("cell-clicks");
    // Desc: 420, 310, 85
    expect(cells[0]).toHaveTextContent("420");
    expect(cells[2]).toHaveTextContent("85");
  });
});

describe("DataTable — état vide", () => {
  it("renders the designed empty state when rows is empty", () => {
    render(<DataTable columns={COLUMNS} rows={[]} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
    expect(screen.getByTestId("data-table-empty")).toHaveTextContent("Aucune donnée disponible");
  });

  it("renders the designed empty state when columns is empty", () => {
    render(<DataTable columns={[]} rows={ROWS} />);
    expect(screen.getByTestId("data-table-empty")).toBeInTheDocument();
  });

  it("has accessible role=status on empty state", () => {
    render(<DataTable columns={COLUMNS} rows={[]} ariaLabel="Tableau vide" />);
    expect(screen.getByRole("status", { name: "Tableau vide" })).toBeInTheDocument();
  });
});
