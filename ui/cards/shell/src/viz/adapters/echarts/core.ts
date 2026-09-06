/**
 * Story 50.5 AC12 -- the ONLY file that imports from `echarts/core`.
 *
 * Tree-shaking form, never the package barrel: importing the top-level module
 * pulls every chart type, every component and both renderers into the bundle, and the
 * MCP App resource has a declared size ceiling (AD-11,
 * `ARCHITECTURE-SPINE.md:105`). Registering exactly what the seven first-release
 * families use keeps the ceiling reachable and keeps the bundle honest about what
 * it can draw.
 *
 * CANVAS, not SVG. ECharts' own guidance
 * (<https://echarts.apache.org/handbook/en/best-practices/canvas-vs-svg/>)
 * prefers canvas for many marks with few DOM nodes, which is this runtime's
 * shape. The accessibility floor does not depend on it: every visual has a real
 * `<table>` fallback with native semantics (AC13), so nothing meaningful is
 * reachable only through the canvas.
 */

import * as echarts from "echarts/core";
import { BarChart, CustomChart, LineChart, ScatterChart } from "echarts/charts";
import {
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  CustomChart,
  LineChart,
  ScatterChart,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
]);

/** The exact module list, exported so `__tests__/bundle.test.ts` can assert it. */
export const REGISTERED_ECHARTS_MODULES = [
  "BarChart",
  "LineChart",
  "CustomChart",
  "ScatterChart",
  "DatasetComponent",
  "GridComponent",
  "LegendComponent",
  "MarkLineComponent",
  "TooltipComponent",
  "CanvasRenderer",
] as const;

export { echarts };
