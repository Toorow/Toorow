/**
 * Story 50.5 AC7 -- the volume thresholds, as THREE named constants in ONE module.
 *
 * Downstream decision 4. `visualization-and-rendering.md:373-379` leaves
 * "row/byte thresholds" open. First release:
 *
 *     MAX_ROWS   = 5000     MAX_SERIES = 24     MAX_CELLS = 100000
 *
 * for every ECharts family. The table fallback has no chart-side limit and is
 * bounded only by the Result slice the server returned
 * (`server/core/query_execution.py:57`, MAX_INLINE_ROWS = 1000 today -- so these
 * ceilings sit above what the platform can currently return, deliberately: a
 * ceiling below the server's own bound would turn every large answer into a table
 * for a reason that is ours, not the data's).
 *
 * REJECTED: deriving the limit from measured frame time at runtime. Two surfaces
 * would then disagree about whether the same Render is a chart or a table, which
 * breaks AC8 outright.
 *
 * WHEN A LIMIT IS EXCEEDED the runtime renders the TABLE FALLBACK with the exact
 * measured count and the exact declared limit. It never renders an empty canvas,
 * never samples silently, and never draws the first N rows as if they were the
 * whole answer.
 */

import { formatValue } from "../theme/formatters";

export const MAX_ROWS = 5000;
export const MAX_SERIES = 24;
export const MAX_CELLS = 100_000;

export interface VolumeConstraints {
  max_rows: number;
  max_series: number;
  max_cells: number;
}

export const DEFAULT_VOLUME_CONSTRAINTS: VolumeConstraints = {
  max_rows: MAX_ROWS,
  max_series: MAX_SERIES,
  max_cells: MAX_CELLS,
};

export interface VolumeMeasurement {
  rows: number;
  series: number;
  cells: number;
}

export interface VolumeVerdict {
  /** True when the chart may be drawn. False means: render the table fallback. */
  chartAllowed: boolean;
  /** English, naming the measured count and the declared limit. Null when allowed. */
  reason: string | null;
  measured: VolumeMeasurement;
  limits: VolumeConstraints;
}

export function evaluateVolume(
  measured: VolumeMeasurement,
  limits: VolumeConstraints,
): VolumeVerdict {
  const exceeded: string[] = [];
  if (measured.rows > limits.max_rows) {
    exceeded.push(
      `${formatValue(measured.rows)} rows exceeds this renderer's declared limit of ` +
        `${formatValue(limits.max_rows)}`,
    );
  }
  if (measured.series > limits.max_series) {
    exceeded.push(
      `${measured.series} series exceeds this renderer's declared limit of ${limits.max_series}`,
    );
  }
  if (measured.cells > limits.max_cells) {
    exceeded.push(
      `${formatValue(measured.cells)} cells exceeds this renderer's declared limit of ` +
        `${formatValue(limits.max_cells)}`,
    );
  }
  if (exceeded.length === 0) {
    return { chartAllowed: true, reason: null, measured, limits };
  }
  return {
    chartAllowed: false,
    reason:
      `The chart is not drawn because ${exceeded.join(", and ")}. ` +
      `Every returned row is shown in the table below -- this is the same data, ` +
      `not a sample of it.`,
    measured,
    limits,
  };
}
