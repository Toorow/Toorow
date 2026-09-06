/**
 * Story 50.5 AC4/AC6 -- family -> renderer is DATA, registered once, here.
 *
 * There is no `switch` in a screen. A screen asks the registry; the registry
 * answers with a renderer or a named refusal. The seven declarations below are
 * the whole standard rendering surface of this build, and every other family
 * Story 50.4's target names is registered as known-and-refused in
 * `registry.ts` (`KNOWN_UNBUILT_FAMILIES`).
 */

import { declare, register, __resetRegistryForTests } from "../registry";
import { EChartsFamilyRenderer } from "./echartsFamily";
import TableRenderer from "./table";
import KpiRenderer from "./kpi";
import { MAX_CELLS, MAX_ROWS, MAX_SERIES } from "../compile/limits";

let installed = false;

/**
 * Idempotent. Every entry (Console, MCP App, share) calls it before rendering,
 * and a test may reset and reinstall.
 */
export function installStandardRenderers(): void {
  if (installed) return;
  installed = true;

  register(
    declare("table", "toorow-table", TableRenderer, {
      requiredWells: ["dimension", "measure"],
      // The table has NO chart-side limit: it is bounded only by the Result
      // slice the server returned (`compile/limits.ts`).
      volume: { max_rows: Number.MAX_SAFE_INTEGER, max_series: Number.MAX_SAFE_INTEGER, max_cells: Number.MAX_SAFE_INTEGER },
      evidenceHit: { pointer: false, keyboard: true, granularity: "datum" },
    }),
  );

  register(
    declare("kpi", "toorow-kpi", KpiRenderer, {
      requiredWells: ["measure"],
      volume: { max_rows: MAX_ROWS, max_series: 1, max_cells: MAX_CELLS },
      evidenceHit: { pointer: true, keyboard: true, granularity: "datum" },
    }),
  );

  register(
    declare("line", "toorow-echarts-line", EChartsFamilyRenderer("line"), {
      requiredWells: ["dimension", "measure"],
      volume: { max_rows: MAX_ROWS, max_series: MAX_SERIES, max_cells: MAX_CELLS },
    }),
  );

  register(
    declare("area", "toorow-echarts-area", EChartsFamilyRenderer("area"), {
      requiredWells: ["dimension", "measure"],
      volume: { max_rows: MAX_ROWS, max_series: MAX_SERIES, max_cells: MAX_CELLS },
    }),
  );

  register(
    declare("bar", "toorow-echarts-bar", EChartsFamilyRenderer("bar"), {
      requiredWells: ["dimension", "measure"],
      volume: { max_rows: MAX_ROWS, max_series: MAX_SERIES, max_cells: MAX_CELLS },
      evidenceHit: { pointer: true, keyboard: true, granularity: "datum" },
    }),
  );

  register(
    declare("stacked_bar", "toorow-echarts-stacked-bar", EChartsFamilyRenderer("stacked_bar"), {
      requiredWells: ["dimension", "breakdown", "measure"],
      volume: { max_rows: MAX_ROWS, max_series: MAX_SERIES, max_cells: MAX_CELLS },
      evidenceHit: { pointer: true, keyboard: true, granularity: "datum" },
    }),
  );

  register(
    declare("scatter", "toorow-echarts-scatter", EChartsFamilyRenderer("scatter"), {
      requiredWells: ["dimension", "measure"],
      volume: { max_rows: MAX_ROWS, max_series: MAX_SERIES, max_cells: MAX_CELLS },
      evidenceHit: { pointer: true, keyboard: true, granularity: "datum" },
    }),
  );

  register(
    declare("waterfall", "toorow-echarts-waterfall", EChartsFamilyRenderer("waterfall"), {
      requiredWells: ["dimension", "measure", "detail"],
      volume: { max_rows: 8, max_series: 1, max_cells: 200 },
      evidenceHit: { pointer: true, keyboard: true, granularity: "datum" },
    }),
  );
}

/** Test-only: reset the registry AND this module's install latch together. */
export function __resetRenderersForTests(): void {
  __resetRegistryForTests();
  installed = false;
}
