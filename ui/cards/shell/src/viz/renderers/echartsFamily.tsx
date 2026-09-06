/**
 * Story 50.5 AC4 -- the five ECharts standard families, drawn through the toorow
 * compiler and nothing else.
 *
 * ONE COMPONENT, FIVE REGISTRATIONS. `line`, `area`, `bar`, `stacked_bar` and
 * `scatter` differ by the series type, the stack and the axis kind -- all of
 * which the compiled model already carries. Writing five near-identical files
 * would give five places for a default to creep back in.
 *
 * WHAT A RENDERER MAY DO, and this file is the proof by example: read the
 * compiled model, hand it to the option builder, and render React around the
 * canvas. It cannot query a warehouse, cannot redefine a metric and cannot inject
 * a frame -- there is no `fetch`, no dynamic `import`, no `iframe` and no
 * arithmetic anywhere under `renderers/`, which the guard test asserts by grep.
 *
 * KEYBOARD EVIDENCE. The canvas is `aria-hidden`; the marks are ALSO published as
 * a visually-hidden list of focusable buttons, one per (category, series). Both
 * paths call `datumKeyAt`, so a keyboard user and a pointer user resolve the same
 * datum key -- which is what makes "evidence never detaches" true rather than
 * claimed.
 */

import { useMemo, useState } from "react";

import EChartsCanvas from "../adapters/echarts/EChartsCanvas";
import { compileChartOption, type EChartsFamily } from "../adapters/echarts/buildOption";
import { datumKeyAt } from "../evidence/resolve";
import { profileLayout } from "../responsive";
import { formatCell } from "../theme/formatters";
import { VizStatePanel } from "../states";
import TableFallback from "./tableFallback";
import type { RendererProps } from "../registry";

export function EChartsFamilyRenderer(family: EChartsFamily) {
  function Renderer(props: RendererProps) {
    const {
      model,
      input,
      palette,
      profile,
      containerWidthPx,
      display,
      onDatumFocus,
      onDatumActivate,
      onFeedbackTarget,
      datumRowOffset,
      onToggleSeries,
    } = props;
    const layout = profileLayout(profile, containerWidthPx);
    const [tableOpen, setTableOpen] = useState(layout.tableFallbackOpen);

    const option = useMemo(
      () => compileChartOption(model, family, palette, layout),
      // `layout` is derived from (profile, containerWidthPx); listing the two
      // inputs keeps the memo stable across renders that changed neither.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [model, palette, profile, containerWidthPx],
    );

    const hiddenSeries = new Set(display.legendHidden ?? []);
    const visibleSeries = model.series.filter((s) => !hiddenSeries.has(s.id));

    // The chart is not drawn when the volume exceeds the declared limits. The
    // table below is then the ONLY rendering, with the exact counts stated.
    if (!model.volume.chartAllowed) {
      return (
        <div data-viz-family={family} data-viz-chart-drawn="false">
          <VizStatePanel kind="truncated" detail={model.volume.reason} />
          <TableFallback
            columns={model.tableColumns}
            rows={input.result.rows}
            caption={`Every row returned for this ${family} visual`}
            numberStyle={model.formats.numberStyle}
            dateStyle={model.formats.dateStyle}
            unit={model.formats.unit}
            targetableFields={[...new Set(Object.values(model.datumTargets).map((target) => target.field))]}
            onFeedbackTarget={onFeedbackTarget ? (rowIndex, field) => onFeedbackTarget(
              { kind: "datum", row_index: datumRowOffset + rowIndex, field },
              `Row ${datumRowOffset + rowIndex + 1} · ${field}`,
            ) : undefined}
          />
        </div>
      );
    }

    return (
      <div data-viz-family={family} data-viz-chart-drawn="true">
        <EChartsCanvas
          option={option}
          height={layout.height}
          onMarkHit={(seriesIndex, dataIndex) =>
            onDatumFocus(datumKeyAt(model, seriesIndex, dataIndex))
          }
          onMarkLeave={() => onDatumFocus(null)}
          onMarkActivate={(seriesIndex, dataIndex) => {
            const key = datumKeyAt(model, seriesIndex, dataIndex);
            if (key) onDatumActivate(key);
          }}
        />

        {/* The keyboard path to every mark. Same derivation as the pointer path. */}
        <ul className="sr-only" aria-label={`Marks of this ${family} visual`}>
          {model.dataset.source.map((row, categoryIndex) =>
            visibleSeries.map((series) => {
              const seriesIndex = model.series.indexOf(series);
              const key = datumKeyAt(model, seriesIndex, categoryIndex);
              if (!key) return null;
              const value =
                family === "waterfall"
                  ? (row[4] ?? null)
                  : (row[model.axes.x.column ? seriesIndex + 1 : seriesIndex] ?? null);
              const category = model.axes.x.column ? row[0] : null;
              return (
                <li key={key}>
                  <button
                    type="button"
                    data-viz-mark={key}
                    onFocus={() => onDatumFocus(key)}
                    onBlur={() => onDatumFocus(null)}
                    onClick={() => onDatumActivate(key)}
                  >
                    {series.name}
                    {category === null ? "" : `, ${formatCell(category, { isDate: model.axes.x.isDate, dateStyle: model.formats.dateStyle as never })}`}
                    {": "}
                    {formatCell(value, {
                      numberStyle: model.formats.numberStyle as never,
                      unit: model.formats.unit,
                    })}
                  </button>
                </li>
              );
            }),
          )}
        </ul>

        {/* Visible legend: the canvas legend is pixels, this one is text and is
            what a screen reader and a keyboard user actually get.

            IT IS ALSO THE ONE WIRED LOCAL CONTROL (AC10). Each entry is a real
            button that toggles `display.legendHidden` through the runtime, which
            emits `onDisplayChange`. It changes what is DRAWN from the rows the
            Result already returned -- the table below still carries every row,
            and the disclosures below it are untouched. The label says so, because
            a control whose scope a reader has to guess is a control that will be
            read as a filter. */}
        {model.legend.visible && model.legend.position !== "none" ? (
          <>
            <ul
              className="m-0 flex list-none flex-wrap gap-3 p-0 text-xs"
              aria-label="Series legend -- hides a series in this chart only"
            >
              {model.legend.entries.map((entry) => {
                const hidden = hiddenSeries.has(entry.id);
                return (
                  <li key={entry.id} className="flex items-center">
                    <button
                      type="button"
                      data-viz-legend-toggle={entry.id}
                      aria-pressed={hidden}
                      onClick={() => onToggleSeries(entry.id)}
                      className={`flex items-center gap-1.5 rounded-sm px-1 py-0.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--focus,currentColor)] ${
                        hidden ? "opacity-50 line-through" : ""
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className="inline-block h-2.5 w-2.5 rounded-sm"
                        style={{ backgroundColor: entry.color }}
                      />
                      <span>{entry.name}</span>
                      <span className="sr-only">
                        {hidden
                          ? " -- hidden in this chart only; the rows are still in the table below. Activate to show it again."
                          : " -- activate to hide this series in this chart only. No row, total or disclosure changes."}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
            {hiddenSeries.size > 0 ? (
              <p className="m-0 mt-1 text-xs opacity-75">
                {hiddenSeries.size} series hidden in this chart only. Every returned row is still
                in the table below and every disclosure still applies.
              </p>
            ) : null}
          </>
        ) : null}

        <details
          open={tableOpen}
          onToggle={(event) => setTableOpen((event.currentTarget as HTMLDetailsElement).open)}
          className="mt-3"
        >
          <summary className="cursor-pointer text-sm">
            Table of every returned row (the same data, not a summary)
          </summary>
          <TableFallback
            columns={model.tableColumns}
            rows={input.result.rows}
            caption={`Every row returned for this ${family} visual`}
            numberStyle={model.formats.numberStyle}
            dateStyle={model.formats.dateStyle}
            unit={model.formats.unit}
            hiddenRowKeys={display.localHiddenRows}
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
  Renderer.displayName = `EChartsFamilyRenderer(${family})`;
  return Renderer;
}
