/**
 * Story 50.5 AC11 -- a mark resolves to ITS OWN row, whatever the row contains.
 *
 * THE DEFECT THIS FILE EXISTS FOR, reproduced before it was repaired.
 * `resolve.ts` used to derive the row by splitting the datum key on `|`, while
 * the compiler built the key by joining with `|` and never escaped the category.
 * Measured against the real modules:
 *
 *     datumKey "h|Brand|Generic|sessions|"  ->  resolved {"campaign":"Brand","sessions":11}
 *     the mark's real value is 999
 *
 * Clicking the mark for the campaign `Brand|Generic` opened the evidence drawer
 * on ANOTHER row, reported as `bound: true`. With a space -- `Brand | Generic`,
 * an ordinary campaign naming convention -- the parse matched no row and `values`
 * came back `{}`, an evidence panel with no values and no statement that it had
 * failed.
 *
 * `resolve.ts` no longer parses anything: the compiler publishes
 * `datumRowIndex`, so the row is reached POSITIONALLY, and the key itself is
 * joined with U+0001 so two datums cannot produce one key. Both halves are
 * asserted here, and the mount test at the bottom proves it on a real DOM rather
 * than only on the pure functions.
 */

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import VisualizationRuntime from "../Runtime";
import { compileVisualModel, DATUM_KEY_SEP } from "../compile/dataset";
import { datumKeyAt, resolveDatumEvidence } from "../evidence/resolve";
import { installStandardRenderers } from "../renderers";
import { getVizPalette } from "../../vizTheme";
import { renderInput, specDocument } from "./fixtures";
import type { RenderInput, VizSpecDocument } from "../contracts";

installStandardRenderers();
afterEach(cleanup);

const LIMITS = { max_rows: 5000, max_series: 24, max_cells: 100_000 };

/** Three campaigns whose names differ only by what surrounds a pipe. */
const PIPE_ROWS = [
  { campaign: "Brand", sessions: 11 },
  { campaign: "Brand|Generic", sessions: 999 },
  { campaign: "Brand | Generic", sessions: 777 },
];

function pipeInput(): RenderInput {
  const input = renderInput();
  input.spec.document = specDocument({
    family: "bar",
    bindings: { dimension: ["campaign"], measure: ["sessions"] },
    evidence: { datum_fields: ["campaign"], mark_binding: "datum" },
  });
  input.result = {
    ...input.result,
    schema: { fields: [{ name: "campaign" }, { name: "sessions" }] },
    rows: PIPE_ROWS,
    manifest: {
      ...input.result.manifest,
      provenance: {
        source_system: "example_source",
        mapping_version_id: "mv_EXAMPLE",
        publication_log_id: "pub_EXAMPLE",
        values: [
          {
            member_id: "campaign",
            source_system: "example_source",
            source_field: "utm_campaign",
            pull_id: "pull_EXAMPLE",
          },
          {
            member_id: "sessions",
            source_system: "example_source",
            source_field: "session_count",
            pull_id: "pull_EXAMPLE",
          },
        ],
      },
    } as never,
  };
  return input;
}

function compile(input: RenderInput) {
  return compileVisualModel(input.result, input.spec.document as VizSpecDocument, {
    palette: getVizPalette(null),
    limits: LIMITS,
  });
}

describe("AC11 -- a `|` in a dimension value cannot move the evidence", () => {
  it("each mark resolves to the value the mark itself draws", () => {
    const input = pipeInput();
    const model = compile(input);
    expect(model.datumKeys).toHaveLength(PIPE_ROWS.length);

    const resolved = model.datumKeys.map((key) =>
      resolveDatumEvidence(key, model, input.result, input.spec.document),
    );
    expect(resolved.map((r) => (r.bound ? r.values : r.message))).toEqual([
      { campaign: "Brand", sessions: 11 },
      { campaign: "Brand|Generic", sessions: 999 },
      { campaign: "Brand | Generic", sessions: 777 },
    ]);
  });

  it("no two datums share a key, whatever the category contains", () => {
    const input = pipeInput();
    const model = compile(input);
    expect(new Set(model.datumKeys).size).toBe(model.datumKeys.length);
    // The separator is a C0 control character precisely so a category cannot
    // contain it. `"A" + "B|C"` and `"A|B" + "C"` must not meet.
    for (const key of model.datumKeys) {
      expect(key.split(DATUM_KEY_SEP)).toHaveLength(4);
    }
    for (const row of PIPE_ROWS) {
      expect(String(row.campaign)).not.toContain(DATUM_KEY_SEP);
    }
  });

  it("the drawer shows the clicked campaign, on a real DOM", () => {
    const input = pipeInput();
    const { container } = render(
      <VisualizationRuntime input={input} containerWidthPx={900} />,
    );
    const marks = container.querySelectorAll("[data-viz-mark]");
    expect(marks).toHaveLength(3);
    fireEvent.click(marks[1]!);
    const drawer = container.querySelector("[data-viz-evidence-drawer]")!;
    expect(drawer.textContent).toContain("Brand|Generic");
    expect(drawer.textContent).toContain("999");
    expect(drawer.textContent).not.toContain("777");
  });

  it("a category containing a space around the pipe resolves too, not to `{}`", () => {
    const input = pipeInput();
    const { container } = render(
      <VisualizationRuntime input={input} containerWidthPx={900} />,
    );
    fireEvent.click(container.querySelectorAll("[data-viz-mark]")[2]!);
    const drawer = container.querySelector("[data-viz-evidence-drawer]")!;
    expect(drawer.textContent).toContain("Brand | Generic");
    expect(drawer.textContent).toContain("777");
  });
});

describe("AC11 -- pointer and keyboard derive the SAME datum key", () => {
  it("`datumKeyAt` is the one derivation, and it is positional", () => {
    const input = renderInput();
    input.spec.document = specDocument({
      family: "line",
      bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
    });
    const model = compile(input);
    // 3 categories x 2 series, laid out category-major.
    expect(model.datumKeys).toHaveLength(6);
    expect(datumKeyAt(model, 0, 0)).toBe(model.datumKeys[0]);
    expect(datumKeyAt(model, 1, 0)).toBe(model.datumKeys[1]);
    expect(datumKeyAt(model, 0, 2)).toBe(model.datumKeys[4]);
    expect(datumKeyAt(model, 2, 0)).toBeNull();
    expect(datumKeyAt(model, 0, -1)).toBeNull();
  });

  it("every datum key points at the row index it was built from", () => {
    const input = pipeInput();
    const model = compile(input);
    model.datumKeys.forEach((key, index) => {
      expect(model.datumRowIndex[key]).toBe(index);
    });
  });
});

describe("AC11 -- an absence is named, never an empty drawer", () => {
  it("a (category, split) the server returned no row for says so", () => {
    const input = renderInput();
    input.spec.document = specDocument({
      family: "stacked_bar",
      bindings: { dimension: ["channel"], breakdown: ["device"], measure: ["sessions"] },
      evidence: { datum_fields: ["channel"], mark_binding: "datum" },
    });
    input.result = {
      ...input.result,
      schema: { fields: [{ name: "channel" }, { name: "device" }, { name: "sessions" }] },
      // `paid` has no `mobile` row: the compiler draws a null cell for it.
      rows: [
        { channel: "organic", device: "mobile", sessions: 10 },
        { channel: "organic", device: "desktop", sessions: 20 },
        { channel: "paid", device: "desktop", sessions: 30 },
      ],
    };
    const model = compile(input);
    const missing = model.datumKeys.filter((k) => model.datumRowIndex[k] === -1);
    expect(missing).toHaveLength(1);
    const resolution = resolveDatumEvidence(
      missing[0]!,
      model,
      input.result,
      input.spec.document,
    );
    expect(resolution.bound).toBe(false);
    expect(resolution.bound ? "" : resolution.message).toContain("No returned row backs this mark");
  });

  it("`mark_binding: \"none\"` still says why, and is never an empty tooltip", () => {
    const input = pipeInput();
    input.spec.document = specDocument({
      family: "bar",
      bindings: { dimension: ["campaign"], measure: ["sessions"] },
      evidence: { datum_fields: [], mark_binding: "none" },
    });
    const model = compile(input);
    const resolution = resolveDatumEvidence(
      model.datumKeys[0]!,
      model,
      input.result,
      input.spec.document,
    );
    expect(resolution.bound).toBe(false);
    expect(resolution.bound ? "" : resolution.message).toContain("No evidence binding");
  });
});
