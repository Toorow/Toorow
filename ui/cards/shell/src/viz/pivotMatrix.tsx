import type { ReactElement } from "react";
import { formatGovernedValue } from "./theme/canonicalAmount";
import { formatNumber, formatValue } from "./theme/formatters";

type CellValue = string | number | boolean | null;

interface PivotValue {
  name: string;
  canonical_field_id?: string | null;
  datastream_id?: string | null;
  /** What the plan froze about this column. Money is canonical micros, and the
   *  App printed the stored integer until the projection carried these two. */
  value_type?: string | null;
  unit?: string | null;
}

interface PivotCell {
  row_key: CellValue[];
  column_key: CellValue[];
  values: Record<string, { value: CellValue; absent_reason?: string }>;
  contributing_rows: number;
}

interface PivotComparisonValue {
  value: number | null;
  absent_reason?: string;
}

interface PivotComparison {
  contract_version: "period-comparison.v1";
  kind: "previous_period" | "previous_year";
  canonical_field_id: string;
  period_field: "k_comparison_period";
  current: { start: string; end: string };
  baseline: { start: string; end: string };
  unavailable_reason?: string;
  deltas: {
    row_key: CellValue[];
    column_key: CellValue[];
    value_field: string;
    current: PivotComparisonValue;
    baseline: PivotComparisonValue;
    absolute_delta: PivotComparisonValue;
    relative_delta: PivotComparisonValue;
  }[];
}

export interface PivotMatrixProjection {
  result_id: string;
  content_hash: string;
  row_fields: string[];
  column_fields: string[];
  value_fields: PivotValue[];
  row_keys: CellValue[][];
  column_keys: CellValue[][];
  cells: PivotCell[];
  bounds: {
    response_bytes: number;
    rows_truncated: boolean;
    columns_truncated: boolean;
    next_row_offset?: number | null;
    next_column_offset?: number | null;
    total_row_keys?: number;
    total_column_keys?: number;
    /** The totals span the filtered set, never the page. See the labels below. */
    totals_cover_unserved_rows?: boolean;
    totals_cover_unserved_columns?: boolean;
  };
  grand_total?: {
    policy?: string;
    by_column_key?: {
      column_key: CellValue[];
      values: Record<string, { value: CellValue; absent_reason?: string }>;
    }[];
    by_row_key?: {
      row_key: CellValue[];
      values: Record<string, { value: CellValue; absent_reason?: string }>;
    }[];
    overall?: Record<string, { value: CellValue; absent_reason?: string }>;
  };
  row_subtotals?: {
    row_key: CellValue[];
    values: Record<string, { value: CellValue; absent_reason?: string }>;
  }[];
  column_subtotals?: {
    column_key: CellValue[];
    values: Record<string, { value: CellValue; absent_reason?: string }>;
  }[];
  comparison?: PivotComparison;
}

export interface PivotContext {
  sources?: { datastream_id?: string; name?: string }[];
  common_keys?: {
    version_id?: string;
    components?: string[];
    relationship?: string;
    cardinality?: string;
    execution_safety?: string;
  }[];
  analysis_context?: {
    business_domain?: { name?: string; version_number?: number } | null;
    golden_question?: { title?: string; version_number?: number } | null;
    requested_skills?: { name?: string; version_number?: number }[];
  } | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function validComparisonValue(value: unknown): boolean {
  const entry = record(value);
  return entry !== null
    && (entry.value === null || (typeof entry.value === "number" && Number.isFinite(entry.value)))
    && (entry.absent_reason === undefined || typeof entry.absent_reason === "string");
}

function validComparison(value: unknown): boolean {
  const comparison = record(value);
  const current = record(comparison?.current);
  const baseline = record(comparison?.baseline);
  if (
    comparison?.contract_version !== "period-comparison.v1"
    || !["previous_period", "previous_year"].includes(String(comparison.kind))
    || typeof comparison.canonical_field_id !== "string"
    || comparison.period_field !== "k_comparison_period"
    || typeof current?.start !== "string"
    || typeof current.end !== "string"
    || typeof baseline?.start !== "string"
    || typeof baseline.end !== "string"
    || !Array.isArray(comparison.deltas)
    || (comparison.unavailable_reason !== undefined
      && typeof comparison.unavailable_reason !== "string")
  ) return false;
  return comparison.deltas.every((raw) => {
    const delta = record(raw);
    return delta !== null
      && Array.isArray(delta.row_key)
      && Array.isArray(delta.column_key)
      && typeof delta.value_field === "string"
      && validComparisonValue(delta.current)
      && validComparisonValue(delta.baseline)
      && validComparisonValue(delta.absolute_delta)
      && validComparisonValue(delta.relative_delta);
  });
}

export function decodePivotMatrix(
  value: unknown,
  resultId: string,
  contentHash: string,
): PivotMatrixProjection | null {
  if (value === undefined) return null;
  const envelope = record(value);
  const matrix = record(envelope?.matrix);
  if (
    envelope?.schema_version !== "analyze-pivot-render.v1" ||
    envelope.result_id !== resultId ||
    envelope.content_hash !== contentHash ||
    matrix?.result_id !== resultId ||
    matrix.content_hash !== contentHash ||
    !Array.isArray(matrix.row_fields) ||
    !Array.isArray(matrix.column_fields) ||
    !Array.isArray(matrix.value_fields) ||
    !Array.isArray(matrix.row_keys) ||
    !Array.isArray(matrix.column_keys) ||
    !Array.isArray(matrix.cells) ||
    !record(matrix.bounds) ||
    (matrix.comparison !== undefined && !validComparison(matrix.comparison))
  ) {
    throw new Error("The host delivered a pivot for another or malformed Result.");
  }
  return matrix as unknown as PivotMatrixProjection;
}

function label(parts: CellValue[], fallback: string): string {
  return parts.length > 0 ? parts.map((part) => part === null ? "(not set)" : String(part)).join(" / ") : fallback;
}

function scalar(value: CellValue, meaning?: PivotValue): string {
  if (value === null) return "—";
  // The SAME read-time division the Console does, from the same module: an
  // amount that reads 124.00 EUR in one door and 124000000 in the other is one
  // Result contradicting itself.
  const governed = formatGovernedValue(value, meaning);
  if (governed !== null) return governed;
  return String(value);
}

function cellKey(row: CellValue[], column: CellValue[]): string {
  return JSON.stringify([row, column]);
}

function comparisonScalar(entry: PivotComparisonValue, relative = false): string {
  if (entry.value === null) {
    if (entry.absent_reason === "baseline_zero") return "baseline is zero";
    if (entry.absent_reason === "comparison_value_missing") return "comparison unavailable";
    return entry.absent_reason ?? "no data";
  }
  // This read the READER'S OS locale (a number formatter built with an
  // `undefined` locale argument), so the same Result printed `12,5 %` on one
  // machine and `12.5%` on another -- the exact disagreement `formatters.ts` is
  // pinned to prevent (story 76-8, AC8).
  return relative
    ? formatNumber(entry.value, "percent")
    : formatValue(entry.value, { maximumFractionDigits: 2 });
}

export default function PivotMatrix({ matrix, context }: { matrix: PivotMatrixProjection; context?: PivotContext }): ReactElement {
  const cells = new Map(
    matrix.cells.map((cell) => [cellKey(cell.row_key, cell.column_key), cell]),
  );
  const columns = matrix.column_keys.flatMap((column) =>
    matrix.value_fields.map((value) => ({ column, value })),
  );
  const subtotals = new Map(
    (matrix.row_subtotals ?? []).map((entry) => [JSON.stringify(entry.row_key), entry]),
  );
  // `column_subtotals` and the grand total's `by_column_key` are the same numbers
  // under two contracts. Either one draws the row; asking for both is not an
  // error, and preferring the subtotal keeps the page consistent with its
  // right-hand column.
  // Same rule as the console: a total spans the filtered set, so a truncated
  // page has to say its totals count rows it is not showing.
  const totalScope = matrix.bounds.totals_cover_unserved_rows === true
    ? ` · all ${matrix.bounds.total_row_keys ?? "?"} rows, not only this page`
    : "";
  // Meme regle sur l'autre axe : un sous-total de ligne court sur les colonnes.
  const rowTotalScope = matrix.bounds.totals_cover_unserved_columns === true
    ? ` · all ${matrix.bounds.total_column_keys ?? "?"} columns`
    : "";
  const columnTotals = new Map(
    (matrix.column_subtotals ?? matrix.grand_total?.by_column_key ?? []).map((entry) => [
      JSON.stringify(entry.column_key),
      entry,
    ]),
  );
  return (
    <section className="my-3 rounded-lg border border-[color:var(--border)] bg-[color:var(--surface)] p-3">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="m-0 text-base font-semibold">Pivot table</h2>
          <p className="m-0 text-xs opacity-75">
            Server-owned projection of Result {matrix.result_id}; no browser totals.
          </p>
          {context?.sources?.length ? (
            <p className="m-0 text-xs opacity-75">
              Sources: {context.sources.map((source) => source.name ?? source.datastream_id).join(" + ")}.
              {context.common_keys?.length
                ? ` Matching: ${context.common_keys.map((key) =>
                    `${key.components?.join(" + ") ?? key.version_id}`
                    + `${key.relationship ? ` via ${key.relationship}` : ""}`
                    + `${key.cardinality ? ` (${key.cardinality})` : ""}`
                    + `${key.execution_safety ? ` — ${key.execution_safety}` : ""}`
                  ).join("; ")}.`
                : ""}
            </p>
          ) : null}
          {context?.analysis_context ? (
            <p className="m-0 text-xs opacity-75" data-testid="pivot-analysis-context">
              {context.analysis_context.business_domain
                ? `Business Domain: ${context.analysis_context.business_domain.name} v${context.analysis_context.business_domain.version_number}. `
                : "Free business exploration. "}
              {context.analysis_context.golden_question
                ? `Golden Question: ${context.analysis_context.golden_question.title} v${context.analysis_context.golden_question.version_number}. `
                : "No Golden Question. "}
              {context.analysis_context.requested_skills?.length
                ? `Requested Skills: ${context.analysis_context.requested_skills.map((skill) => `${skill.name} v${skill.version_number}`).join(", ")}.`
                : "No requested Skill."}
            </p>
          ) : null}
        </div>
        <span className="text-xs tabular-nums opacity-70">
          {matrix.row_keys.length} rows × {columns.length} value columns
        </span>
      </div>
      <div
        className="max-w-full overflow-x-auto rounded-md border border-[color:var(--border)]"
        role="region"
        aria-label="Multi-Datastream pivot matrix"
        tabIndex={0}
      >
        <table className="w-full min-w-max border-collapse text-sm">
          <thead>
            <tr>
              <th className="border-b border-r px-3 py-2 text-left">Rows</th>
              {columns.map(({ column, value }) => (
                <th
                  key={cellKey(column, [value.name])}
                  className="border-b px-3 py-2 text-right"
                >
                  <span className="block font-semibold">{label(column, "All")}</span>
                  <span className="block text-xs font-normal opacity-70">{value.name}</span>
                </th>
              ))}
              {matrix.row_subtotals?.length
                ? matrix.value_fields.map((value) => (
                    <th key={`subtotal:${value.name}`} className="border-b px-3 py-2 text-right">
                      <span className="block font-semibold">Row total{rowTotalScope}</span>
                      <span className="block text-xs font-normal opacity-70">{value.name}</span>
                    </th>
                  ))
                : null}
            </tr>
          </thead>
          <tbody>
            {matrix.row_keys.map((row) => (
              <tr key={JSON.stringify(row)}>
                <th className="border-r border-t px-3 py-2 text-left font-medium">
                  {label(row, "All")}
                </th>
                {columns.map(({ column, value }) => {
                  const cell = cells.get(cellKey(row, column));
                  const projected = cell?.values[value.name];
                  return (
                    <td
                      key={cellKey(column, [value.name])}
                      className="border-t px-3 py-2 text-right tabular-nums"
                      title={projected?.absent_reason ?? `${cell?.contributing_rows ?? 0} contributing rows`}
                    >
                      {scalar(projected?.value ?? null, value)}
                    </td>
                  );
                })}
                {matrix.row_subtotals?.length
                  ? matrix.value_fields.map((value) => (
                      <td key={`subtotal:${value.name}`} className="border-t px-3 py-2 text-right font-semibold tabular-nums">
                        {scalar(subtotals.get(JSON.stringify(row))?.values[value.name]?.value ?? null, value)}
                      </td>
                    ))
                  : null}
              </tr>
            ))}
            {/* The column axis totals too. A matrix that totals one axis and not
                the other is a grouped list wearing a pivot's name. */}
            {columnTotals.size ? (
              <tr data-testid="pivot-column-totals">
                <th className="border-r border-t px-3 py-2 text-left font-semibold">
                  Column total{totalScope}
                </th>
                {columns.map(({ column, value }) => (
                  <td
                    key={cellKey(column, [value.name])}
                    className="border-t px-3 py-2 text-right font-semibold tabular-nums"
                  >
                    {scalar(
                      columnTotals.get(JSON.stringify(column))?.values[value.name]?.value ?? null,
                      value,
                    )}
                  </td>
                ))}
                {matrix.row_subtotals?.length
                  ? matrix.value_fields.map((value) => (
                      <td
                        key={`corner:${value.name}`}
                        className="border-t px-3 py-2 text-right font-semibold tabular-nums"
                      >
                        {scalar(matrix.grand_total?.overall?.[value.name]?.value ?? null, value)}
                      </td>
                    ))
                  : null}
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {/* The corner cell exists only under `both`: a grand total of one axis is a
          list of totals, not a single number, and printing one of its entries as
          "the" grand total would name a part after the whole. */}
      {matrix.grand_total?.overall ? (
        <p className="mb-0 mt-2 text-sm font-semibold" data-testid="pivot-grand-total">
          Grand total{totalScope} · {matrix.value_fields.map((value) =>
            `${value.name}: ${scalar(matrix.grand_total?.overall?.[value.name]?.value ?? null, value)}`).join(" · ")}
        </p>
      ) : null}
      {matrix.comparison ? (
        <div className="mt-3 space-y-2" data-testid="period-comparison">
          <p className="m-0 text-sm font-semibold">
            {matrix.comparison.kind === "previous_year"
              ? "Compared with the previous year"
              : "Compared with the previous period"}
          </p>
          <p className="m-0 text-xs opacity-75">
            Current {matrix.comparison.current.start} to {matrix.comparison.current.end}; baseline{" "}
            {matrix.comparison.baseline.start} to {matrix.comparison.baseline.end}. Both windows
            belong to Result {matrix.result_id}.
          </p>
          <div
            className="max-w-full overflow-x-auto rounded-md border border-[color:var(--border)]"
            role="region"
            aria-label="Period comparison deltas"
            tabIndex={0}
          >
            <table className="w-full min-w-max border-collapse text-sm">
              <thead>
                <tr>
                  <th className="border-b px-3 py-2 text-left">Rows</th>
                  <th className="border-b px-3 py-2 text-left">Columns</th>
                  <th className="border-b px-3 py-2 text-left">Value</th>
                  <th className="border-b px-3 py-2 text-right">Current</th>
                  <th className="border-b px-3 py-2 text-right">Baseline</th>
                  <th className="border-b px-3 py-2 text-right">Absolute change</th>
                  <th className="border-b px-3 py-2 text-right">Relative change</th>
                </tr>
              </thead>
              <tbody>
                {matrix.comparison.deltas.map((delta) => (
                  <tr key={`${JSON.stringify(delta.row_key)}:${JSON.stringify(delta.column_key)}:${delta.value_field}`}>
                    <td className="border-t px-3 py-2">{label(delta.row_key, "All")}</td>
                    <td className="border-t px-3 py-2">{label(delta.column_key, "All")}</td>
                    <td className="border-t px-3 py-2">{delta.value_field}</td>
                    <td className="border-t px-3 py-2 text-right tabular-nums">{comparisonScalar(delta.current)}</td>
                    <td className="border-t px-3 py-2 text-right tabular-nums">{comparisonScalar(delta.baseline)}</td>
                    <td className="border-t px-3 py-2 text-right tabular-nums">{comparisonScalar(delta.absolute_delta)}</td>
                    <td className="border-t px-3 py-2 text-right tabular-nums">{comparisonScalar(delta.relative_delta, true)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {matrix.comparison.unavailable_reason ? (
            <p role="status" className="m-0 text-xs opacity-75">
              Comparison unavailable: {matrix.comparison.unavailable_reason}.
            </p>
          ) : null}
        </div>
      ) : null}
      {matrix.bounds.rows_truncated || matrix.bounds.columns_truncated ? (
        <p role="status" className="mb-0 mt-2 text-xs opacity-75">
          This is a bounded matrix page. Open the Result to narrow or continue the exploration.
        </p>
      ) : null}
    </section>
  );
}
