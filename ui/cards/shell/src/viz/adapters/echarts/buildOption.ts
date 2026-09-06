/**
 * Story 50.5 AC3/AC4 -- the ONLY producer of an ECharts option object.
 *
 * Everything here is a translation of the compiled visual model
 * (`viz/compile/dataset.ts`) into ECharts' dataset/encode vocabulary. It reads
 * the model and writes an option; it never reads a Result, never reads a spec and
 * never receives one from a caller. That is what makes AC3 provable by grep: if
 * the only directory that can name `setOption` or an option type is this one, a
 * `grep` settles whether a raw configuration can enter from anywhere else.
 *
 * EVERY VISUAL DECISION IS ALREADY RESOLVED before it gets here -- colours from
 * the live theme, number and date formats from the shared formatters, axis and
 * legend configuration from the spec. Nothing below reaches for an ECharts
 * default, because a default is a decision nobody made and it would differ
 * between a Console chart and the same Render in a shared link.
 */

import type { EChartsCoreOption } from "echarts/core";

import type { CompiledVisualModel } from "../../compile/dataset";
import { formatCell, formatDate, formatNumber } from "../../theme/formatters";
import type { VizPalette } from "../../../vizTheme";
import type { ProfileLayout } from "../../responsive";

export type EChartsFamily = "line" | "area" | "bar" | "stacked_bar" | "scatter" | "waterfall";

const AXIS_LABEL_ROTATE_THRESHOLD = 8;

function axisType(scale: string): "value" | "log" | "category" {
  if (scale === "log") return "log";
  if (scale === "categorical" || scale === "ordinal") return "category";
  return "value";
}

function tickInterval(density: string, count: number): number | "auto" {
  if (density === "dense") return 0;
  if (density === "sparse") return Math.max(1, Math.ceil(count / 6) - 1);
  return "auto";
}

interface WaterfallRenderApi {
  value: (dimension: number) => unknown;
  coord: (value: [number, number]) => [number, number];
  size: (value: [number, number]) => [number, number];
}

function waterfallOption(
  model: CompiledVisualModel,
  palette: VizPalette,
  layout: ProfileLayout,
): EChartsCoreOption {
  const valueFormatter = (value: unknown): string =>
    formatNumber(
      typeof value === "number" || typeof value === "string" ? value : null,
      model.formats.numberStyle as never,
      model.formats.unit,
    );
  return {
    animation: false,
    backgroundColor: "transparent",
    textStyle: { fontFamily: "inherit" },
    dataset: { dimensions: model.dataset.dimensions, source: model.dataset.source },
    grid: {
      left: 8,
      right: 8,
      top: layout.legendCollapsed ? 8 : 12,
      bottom: 8,
      containLabel: true,
    },
    legend: { show: false },
    tooltip: { trigger: "item", confine: true, valueFormatter },
    xAxis: {
      type: "category",
      axisLine: { lineStyle: { color: palette.hairline } },
      axisLabel: {
        interval: 0,
        rotate: model.dataset.source.length > AXIS_LABEL_ROTATE_THRESHOLD ? 35 : 0,
        hideOverlap: true,
      },
    },
    yAxis: {
      type: "value",
      name: model.axes.y.label ?? undefined,
      min: 0,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: palette.hairline } },
      axisLabel: { formatter: valueFormatter },
    },
    series: [
      {
        id: "waterfall",
        name: "Amount",
        type: "custom",
        encode: { x: "label", y: ["start_total_micros", "running_total_micros"] },
        renderItem: (params: { dataIndex: number }, api: WaterfallRenderApi) => {
          const role = api.value(3);
          const rawStart = api.value(1);
          const rawEnd = api.value(2);
          const rawValue = api.value(4);
          const complete = api.value(5);
          if (typeof rawEnd !== "number") return null;
          const start = role === "delta" && typeof rawStart === "number" ? rawStart : 0;
          const startPoint = api.coord([params.dataIndex, start]);
          const endPoint = api.coord([params.dataIndex, rawEnd]);
          const width = Math.max(2, api.size([1, 0])[0] * 0.58);
          const fill =
            complete !== true
              ? palette.track
              : role === "total" || role === "subtotal"
                ? palette.accent
                : typeof rawValue === "number" && rawValue < 0
                  ? palette.diverging(1)
                  : palette.diverging(0);
          return {
            type: "rect",
            shape: {
              x: startPoint[0] - width / 2,
              y: Math.min(startPoint[1], endPoint[1]),
              width,
              height: Math.abs(endPoint[1] - startPoint[1]),
            },
            style: { fill },
          };
        },
      },
    ],
  } as EChartsCoreOption;
}

/**
 * Build the option. `layout` carries the profile's LAYOUT decisions only -- it
 * cannot change a value, a colour's semantic direction, a format or an evidence
 * key, which is what `__tests__/parity.test.tsx` asserts.
 */
export function compileChartOption(
  model: CompiledVisualModel,
  family: EChartsFamily,
  palette: VizPalette,
  layout: ProfileLayout,
): EChartsCoreOption {
  if (family === "waterfall") {
    return waterfallOption(model, palette, layout);
  }
  const categoryCount = model.dataset.source.length;
  const xIsCategory = family !== "scatter";

  const seriesType = family === "scatter" ? "scatter" : family === "bar" || family === "stacked_bar" ? "bar" : "line";

  const series = model.series.map((s) => ({
    id: s.id,
    name: s.name,
    type: seriesType,
    // ENCODE, never `data`: the numbers come out of the dataset the compiler
    // built from the Result, and no array is assembled on the way.
    encode:
      family === "scatter"
        ? { x: model.dataset.dimensions[1] ?? s.valueColumn, y: s.valueColumn, itemName: model.axes.x.column ?? undefined }
        : { x: model.axes.x.column ?? undefined, y: s.valueColumn },
    itemStyle: { color: s.color },
    lineStyle: family === "line" || family === "area" ? { color: s.color, width: 2 } : undefined,
    areaStyle: family === "area" ? { color: s.color, opacity: 0.18 } : undefined,
    stack: s.stack ?? undefined,
    symbolSize: family === "scatter" ? 9 : 6,
    showSymbol: family === "line" || family === "area" ? categoryCount <= 60 : undefined,
    emphasis: { focus: "series" as const },
  }));

  const valueFormatter = (value: unknown): string =>
    formatNumber(
      typeof value === "number" || typeof value === "string" ? value : null,
      model.formats.numberStyle as never,
      model.formats.unit,
    );

  const categoryFormatter = (value: unknown): string =>
    model.axes.x.isDate
      ? formatDate(typeof value === "string" ? value : null, model.formats.dateStyle as never)
      : formatCell(typeof value === "string" || typeof value === "number" ? value : null, {
          numberStyle: model.formats.numberStyle as never,
        });

  return {
    // No animation: a Render is a frozen claim, and an animated redraw makes two
    // surfaces disagree about what a screenshot of the same Render looks like.
    animation: false,
    color: model.series.map((s) => s.color),
    backgroundColor: "transparent",
    textStyle: { fontFamily: "inherit" },
    dataset: { dimensions: model.dataset.dimensions, source: model.dataset.source },
    grid: {
      left: 8,
      right: 8,
      top: layout.legendCollapsed ? 8 : 28,
      bottom: 8,
      containLabel: true,
    },
    legend: {
      show: model.legend.visible && !layout.legendCollapsed && model.legend.position !== "none",
      type: "scroll",
      top: model.legend.position === "top" ? 0 : undefined,
      bottom: model.legend.position === "bottom" ? 0 : undefined,
      left: model.legend.position === "left" ? 0 : undefined,
      right: model.legend.position === "right" ? 0 : undefined,
      orient: model.legend.position === "left" || model.legend.position === "right" ? "vertical" : "horizontal",
      data: model.legend.entries.map((e) => e.name),
      textStyle: { color: palette.hairline ? undefined : undefined },
    },
    tooltip: {
      trigger: family === "scatter" ? "item" : "axis",
      confine: true,
      valueFormatter,
    },
    xAxis: {
      type: xIsCategory ? "category" : axisType(model.axes.x.scale),
      name: model.axes.x.label ?? undefined,
      nameLocation: "middle",
      nameGap: 26,
      axisLine: { lineStyle: { color: palette.hairline } },
      splitLine: { show: false },
      axisLabel: {
        interval: tickInterval(model.axes.x.tickDensity, categoryCount),
        rotate: xIsCategory && categoryCount > AXIS_LABEL_ROTATE_THRESHOLD ? 35 : 0,
        formatter: xIsCategory ? categoryFormatter : valueFormatter,
        hideOverlap: true,
      },
    },
    yAxis: {
      type: axisType(model.axes.y.scale),
      name: model.axes.y.label ?? undefined,
      scale: !model.axes.y.zeroBaseline,
      min: model.axes.y.zeroBaseline ? 0 : undefined,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: palette.hairline } },
      axisLabel: { formatter: valueFormatter },
    },
    series,
  };
}
