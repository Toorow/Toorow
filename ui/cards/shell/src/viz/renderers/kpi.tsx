/**
 * Story 50.5 -- the `kpi` family: one governed measure as a single figure.
 *
 * The figure is READ from the first returned row, at the measure the spec bound.
 * It is not summed, not averaged and not "the latest of" anything: a KPI whose
 * value the runtime computed would be a second metric definition
 * (`analyze-and-test.md` -- "Analyze creates another source of metric
 * definitions" is a failure clause, not a feature). A Result that returns more
 * than one row for a KPI shows the first row's value AND says so, because
 * silently picking one of several would hide a question the query did not settle.
 *
 * The table fallback is present here as everywhere: the figure is a reading of a
 * row, and the row stays visible.
 */

import { formatCell } from "../theme/formatters";
import TableFallback from "./tableFallback";
import { VizStatePanel } from "../states";
import type { RendererProps } from "../registry";

export default function KpiRenderer(props: RendererProps) {
  const {
    model,
    input,
    palette,
    onDatumFocus,
    onDatumActivate,
    onFeedbackTarget,
    datumRowOffset,
  } = props;
  const measure = model.series[0];
  const row = input.result.rows[0];
  const value = measure && row ? (row[measure.measureId] ?? null) : null;
  const datumKey = model.datumKeys[0] ?? null;
  const ambiguous = input.result.rows.length > 1;

  return (
    <div data-viz-family="kpi" data-viz-chart-drawn="true">
      {ambiguous ? (
        <VizStatePanel
          kind="partial"
          detail={
            `This query returned ${input.result.rows.length} rows and the KPI family shows one ` +
            `figure. The first returned row is shown; nothing has been combined. Every row is ` +
            `in the table below.`
          }
        />
      ) : null}
      <button
        type="button"
        data-viz-mark={datumKey ?? undefined}
        onFocus={() => datumKey && onDatumFocus(datumKey)}
        onBlur={() => onDatumFocus(null)}
        onClick={() => datumKey && onDatumActivate(datumKey)}
        className="flex w-full flex-col items-start gap-1 rounded-md px-3 py-4 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus,currentColor)]"
      >
        <span className="text-xs uppercase tracking-wide opacity-70">
          {measure?.name ?? "No measure bound"}
        </span>
        <span
          className="text-4xl font-semibold tabular-nums"
          style={{ color: palette.accent }}
        >
          {formatCell(value, {
            numberStyle: model.formats.numberStyle as never,
            unit: model.formats.unit,
          })}
        </span>
      </button>
      <details className="mt-3">
        <summary className="cursor-pointer text-sm">
          Table of every returned row (the same data, not a summary)
        </summary>
        <TableFallback
          columns={model.tableColumns}
          rows={input.result.rows}
          caption="Every row returned for this KPI"
          numberStyle={model.formats.numberStyle}
          dateStyle={model.formats.dateStyle}
          unit={model.formats.unit}
          targetableFields={[...new Set(Object.values(model.datumTargets).map((target) => target.field))]}
          onFeedbackTarget={onFeedbackTarget ? (rowIndex, field) => onFeedbackTarget(
            { kind: "datum", row_index: datumRowOffset + rowIndex, field },
            `Row ${datumRowOffset + rowIndex + 1} · ${field}`,
          ) : undefined}
        />
      </details>
    </div>
  );
}
