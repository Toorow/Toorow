/**
 * Story 50.5 AC7/AC13 -- the accessible table fallback EVERY visual carries.
 *
 * `visualization-and-rendering.md:189` lists "a direct table fallback for every
 * visual" among the runtime's contents. Story 50.4's grammar only declares
 * `accessibility.table_fallback` as a required field; this component is what
 * makes that declaration true on a screen.
 *
 * IT IS THE SAME DATA, NOT A SUMMARY OF IT. Every returned row, every returned
 * column, in the order the server ranked them. No sampling, no "first N rows",
 * no derived total. When a volume limit stops the chart from being drawn, this is
 * what renders, with the exact measured count and the exact declared limit stated
 * above it -- never a blank canvas.
 *
 * NATIVE SEMANTICS. A real `<table>` with a real `<caption>` and real `<th
 * scope>`, inside its own focusable horizontal scroller so wide data scrolls in
 * its container and never the page (WCAG 2.2 reflow, operable from 1280 CSS px
 * and at 400% zoom). The scroller carries `tabindex=0` and a label, because a
 * region that scrolls must be reachable by keyboard.
 */

import type { CellValue, VizResult } from "../contracts";
import type { CompiledTableColumn } from "../compile/dataset";
import { formatCell } from "../theme/formatters";

/**
 * Table columns derived from the Result ALONE, with no compiler and no spec.
 *
 * This exists for the refusal path (AC7). When the runtime refuses a family it
 * does not build, or a profile a renderer does not declare, there IS no compiled
 * model -- the refusal happened before compilation. The refusal message says "the
 * accessible table below shows every returned row", so a table has to be
 * drawable from the Result on its own, or the message is a promise the screen
 * does not keep. `role` is `dimension` for every column because without the
 * spec's bindings nothing here knows which member is a measure, and inventing
 * that would be the compiler's job done badly.
 */
export function columnsFromResult(result: VizResult | undefined | null): CompiledTableColumn[] {
  const declared = result?.schema?.fields;
  const names =
    Array.isArray(declared) && declared.length > 0
      ? declared.map((f) => f.name)
      : result?.rows?.length
        ? Object.keys(result.rows[0]!)
        : [];
  return names.map((name) => ({ key: name, label: name, isDate: false, role: "dimension" }));
}

export interface TableFallbackProps {
  columns: CompiledTableColumn[];
  rows: Record<string, CellValue>[];
  caption: string;
  numberStyle: string;
  dateStyle: string;
  unit: string | null;
  /** Datum keys hidden locally. The rows stay counted; only their display changes. */
  hiddenRowKeys?: string[];
  rowKeyOf?: (rowIndex: number) => string;
  onFeedbackTarget?: (rowIndex: number, field: string) => void;
  targetableFields?: string[];
}

export default function TableFallback(props: TableFallbackProps) {
  const {
    columns,
    rows,
    caption,
    numberStyle,
    dateStyle,
    unit,
    hiddenRowKeys,
    rowKeyOf,
    onFeedbackTarget,
    targetableFields,
  } = props;
  const hidden = new Set(hiddenRowKeys ?? []);
  const targetable = new Set(targetableFields ?? []);

  return (
    <div
      tabIndex={0}
      role="region"
      aria-label={`${caption} -- scrollable table`}
      data-viz-table-scroll="true"
      className="max-w-full overflow-x-auto focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus,currentColor)]"
    >
      <table className="w-full border-collapse text-sm" data-viz-table-fallback="true">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`border-b border-[color:var(--color-divider-base,currentColor)] px-3 py-2 font-semibold ${
                  column.role === "measure" ? "text-right" : "text-left"
                }`}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => {
            const key = rowKeyOf ? rowKeyOf(rowIndex) : String(rowIndex);
            if (hidden.has(key)) return null;
            return (
              <tr key={key} data-viz-row-key={key}>
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={`border-b border-[color:var(--color-divider-base,currentColor)] px-3 py-2 ${
                      column.role === "measure" ? "text-right tabular-nums" : "text-left"
                    }`}
                  >
                    {onFeedbackTarget && targetable.has(column.key) ? (
                      <button
                        type="button"
                        data-feedback-datum={`${rowIndex}:${column.key}`}
                        aria-label={`Target feedback at row ${rowIndex + 1}, ${column.label}`}
                        onClick={() => onFeedbackTarget(rowIndex, column.key)}
                        className="underline underline-offset-2"
                      >
                        {formatCell(row[column.key] ?? null, {
                          numberStyle: column.role === "measure" ? (numberStyle as never) : "auto",
                          dateStyle: dateStyle as never,
                          isDate: column.isDate,
                          unit: column.role === "measure" ? unit : null,
                        })}
                      </button>
                    ) : (
                      formatCell(row[column.key] ?? null, {
                        numberStyle: column.role === "measure" ? (numberStyle as never) : "auto",
                        dateStyle: dateStyle as never,
                        isDate: column.isDate,
                        unit: column.role === "measure" ? unit : null,
                      })
                    )}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
