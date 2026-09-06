import { describe, expect, it } from "vitest";

import { compileChartOption } from "../adapters/echarts/buildOption";
import { compileVisualModel } from "../compile/dataset";
import { serializeForConsole } from "../entries/console";
import { serializeForMcpApp } from "../entries/mcpApp";
import { serializeForShare } from "../entries/share";
import { resolveDatumEvidence } from "../evidence/resolve";
import { installStandardRenderers } from "../renderers";
import { getVizPalette } from "../../vizTheme";
import { profileLayout } from "../responsive";
import { WATERFALL_ROWS, waterfallInput } from "./waterfallFixture";

installStandardRenderers();

const palette = getVizPalette(null);
const limits = { max_rows: 8, max_series: 1, max_cells: 200 };

describe("Story 41.6 -- standard waterfall runtime", () => {
  it("compiles only server-authored values and keeps server datum keys", () => {
    const input = waterfallInput();
    const model = compileVisualModel(input.result, input.spec.document, {
      palette,
      limits,
      unit: "EUR",
    });

    expect(model.family).toBe("waterfall");
    expect(model.datumKeys).toEqual(WATERFALL_ROWS.map((row) => row.datum_key));
    expect(model.datumTargets[WATERFALL_ROWS[0]!.datum_key]).toEqual({
      row_index: 0,
      field: "running_total_micros",
    });
    expect(model.dataset.source).toEqual(
      WATERFALL_ROWS.map((row) => [
        row.label,
        row.start_total_micros,
        row.running_total_micros,
        row.waterfall_role,
        row.value_micros,
        row.is_complete,
      ]),
    );
    const returned = new Set<unknown>(WATERFALL_ROWS.flatMap((row) => Object.values(row)));
    for (const line of model.dataset.source) {
      for (const value of line) expect(returned.has(value)).toBe(true);
    }
  });

  it("builds the shared ECharts custom-series option from the compiled dataset", () => {
    const input = waterfallInput();
    const model = compileVisualModel(input.result, input.spec.document, {
      palette,
      limits,
      unit: "EUR",
    });
    const option = compileChartOption(
      model,
      "waterfall",
      palette,
      profileLayout("console", 900),
    ) as { series?: { type?: string }[]; dataset?: { source?: unknown[] } };
    expect(option.series?.[0]?.type).toBe("custom");
    expect(option.dataset?.source).toEqual(model.dataset.source);
  });

  it("resolves each mark through its exact server evidence key", () => {
    const input = waterfallInput();
    const model = compileVisualModel(input.result, input.spec.document, {
      palette,
      limits,
      unit: "EUR",
    });
    const second = resolveDatumEvidence(
      model.datumKeys[1]!,
      model,
      input.result,
      input.spec.document,
    );
    expect(second.bound).toBe(true);
    if (!second.bound) return;
    expect(second.provenance[0]).toMatchObject({
      member_id: "m2",
      source_field: "platform_fee",
      pull_id: "pull_example",
    });
    expect(second.values.value_micros).toBe(100_000);
  });

  it("serializes byte-equal values, order, gaps and evidence across all entries", () => {
    const input = waterfallInput();
    const consoleModel = serializeForConsole(input);
    expect(serializeForMcpApp(input, "inline")).toEqual(consoleModel);
    expect(serializeForShare(input)).toEqual(consoleModel);
    expect(consoleModel.datumKeys).toEqual(WATERFALL_ROWS.map((row) => row.datum_key));
    expect(consoleModel.rendererBuild).toBe(
      "waterfall/toorow-echarts-waterfall@1.0.0",
    );
  });
});
