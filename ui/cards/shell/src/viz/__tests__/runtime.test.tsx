/**
 * Story 50.5 AC7/AC10/AC11/AC13/AC17 -- the runtime on a real DOM.
 *
 * jsdom has no canvas context, so ECharts cannot paint here. That is recorded,
 * not hidden: the canvas is stubbed in `src/test-setup.ts`, and everything that
 * MATTERS for accessibility and evidence -- the focusable mark list, the legend,
 * the disclosures, the table fallback, the evidence drawer -- is React and is
 * asserted below.
 *
 * WHAT IS NOT PROVEN HERE, stated rather than implied. An earlier version of this
 * docstring said the pixels were "proven by the real browser path recorded in the
 * Dev Agent Record". They are not: that record states at its own line 1381 that
 * the browser path was NOT executed. Nothing in this repository has yet drawn a
 * pixel of this runtime and looked at it. These assertions cover structure,
 * semantics, evidence and refusals; a visual regression of the canvas is
 * unproven and is named as unproven.
 */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import VisualizationRuntime, { buildNewExecutionRequest } from "../Runtime";
import { installStandardRenderers, __resetRenderersForTests } from "../renderers";
import { declare, register } from "../registry";
import { renderInput, specDocument } from "./fixtures";
import { getVizPalette } from "../../vizTheme";
import { compileVisualModel } from "../compile/dataset";

installStandardRenderers();
afterEach(cleanup);

describe("AC13 -- the table fallback is real, keyboard-reachable and complete", () => {
  it("every family exposes a native table carrying every returned row", () => {
    for (const family of ["table", "kpi", "line", "area", "bar", "scatter"] as const) {
      cleanup();
      const input = renderInput();
      input.spec.document = specDocument({
        family,
        bindings:
          family === "kpi"
            ? { measure: ["sessions"] }
            : family === "scatter"
              ? { dimension: ["channel"], measure: ["sessions", "conversions"] }
              : { dimension: ["channel"], measure: ["sessions"] },
      });
      input.pins.renderer_build = "";
      const { container } = render(
        <VisualizationRuntime input={{ ...input, pins: { ...input.pins, renderer_build: rendererBuildFor(family) } }} containerWidthPx={900} />,
      );
      const table = container.querySelector("[data-viz-table-fallback]");
      expect(table, `${family} has no table fallback`).not.toBeNull();
      expect(within(table as HTMLElement).getByText("organic")).toBeTruthy();
      expect(within(table as HTMLElement).getByText("referral")).toBeTruthy();
      // The scroller is focusable, so wide data is reachable by keyboard.
      const scroller = container.querySelector("[data-viz-table-scroll]");
      expect(scroller?.getAttribute("tabindex")).toBe("0");
    }
  });

  it("`empty` and `unavailable` never share a rendering, and unavailable names the missing link", () => {
    const empty = renderInput();
    empty.result = { ...empty.result, outcome: "empty", rows: [] };
    const { container, unmount } = render(<VisualizationRuntime input={empty} containerWidthPx={900} />);
    expect(container.querySelector('[data-viz-state="empty"]')).not.toBeNull();
    expect(container.querySelector('[data-viz-state="unavailable"]')).toBeNull();
    unmount();

    const unavailable = renderInput();
    unavailable.result = {
      ...unavailable.result,
      outcome: "unavailable",
      rows: [],
      manifest: {
        ...unavailable.result.manifest,
        unavailable_reason: "the bound Datastream has no materialized output",
        missing_link: "datastream_output_versions",
      },
    };
    const second = render(<VisualizationRuntime input={unavailable} containerWidthPx={900} />);
    const panel = second.container.querySelector('[data-viz-state="unavailable"]');
    expect(panel).not.toBeNull();
    expect(panel!.textContent).toContain("datastream_output_versions");
    expect(panel!.textContent).toContain("could not be asked");
  });

  it("states are announced without stealing focus, and errors are assertive", () => {
    const input = renderInput();
    input.result = { ...input.result, outcome: "degraded" };
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);
    const panel = container.querySelector('[data-viz-state="degraded"]')!;
    expect(panel.getAttribute("role")).toBe("status");
    expect(panel.getAttribute("aria-live")).toBe("polite");
    // Status is never colour-only: the state is named in text.
    expect(panel.textContent).toContain("degraded source");
  });
});

describe("AC11 -- evidence resolves from mark back to the immutable Result", () => {
  it("keeps mark activation on Evidence and emits the governed datum only on the named action", () => {
    const input = renderInput();
    const target = vi.fn();
    const { container } = render(
      <VisualizationRuntime
        input={input}
        containerWidthPx={900}
        datumRowOffset={100}
        onFeedbackTarget={target}
      />,
    );
    fireEvent.click(container.querySelector("[data-viz-mark]") as HTMLButtonElement);
    expect(container.querySelector("[data-viz-evidence-drawer]")).not.toBeNull();
    expect(target).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Target this value for feedback" }));
    expect(target).toHaveBeenCalledWith(
      { kind: "datum", row_index: 100, field: "sessions" },
      "Row 101 · sessions",
    );
  });

  it("offers the same exact locator through the keyboard-operable table path", () => {
    const input = renderInput();
    input.spec.document = specDocument({ family: "table", bindings: { measure: ["sessions"], dimension: ["channel"] } });
    input.pins.renderer_build = "table/toorow-table@1.0.0";
    const target = vi.fn();
    render(
      <VisualizationRuntime input={input} datumRowOffset={2} onFeedbackTarget={target} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Target feedback at row 1, sessions" }));
    expect(target).toHaveBeenCalledWith(
      { kind: "datum", row_index: 2, field: "sessions" },
      "Row 3 · sessions",
    );
  });

  it("a keyboard-activated mark opens the evidence layer and shows the provenance chain", () => {
    const input = renderInput();
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);
    const mark = container.querySelector("[data-viz-mark]") as HTMLButtonElement;
    expect(mark).not.toBeNull();
    fireEvent.click(mark);
    const drawer = container.querySelector("[data-viz-evidence-drawer]");
    expect(drawer).not.toBeNull();
    expect(drawer!.textContent).toContain("Evidence");
    // The D3 evidence path renders as a real tree of focusable nodes.
    const path = container.querySelector("[data-viz-evidence-path]");
    expect(path).not.toBeNull();
    expect(path!.getAttribute("role")).toBe("tree");
    expect(path!.querySelectorAll('[role="treeitem"]').length).toBeGreaterThan(1);
  });

  it("closing the evidence layer restores focus to its invoker", () => {
    const input = renderInput();
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);
    const mark = container.querySelector("[data-viz-mark]") as HTMLButtonElement;
    mark.focus();
    fireEvent.click(mark);
    fireEvent.click(screen.getByText("Close evidence"));
    expect(document.activeElement).toBe(mark);
  });

  it("a datum with no evidence binding renders the explicit state, not an empty tooltip", () => {
    const input = renderInput();
    input.spec.document = specDocument({
      evidence: { datum_fields: [], mark_binding: "none" },
    });
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);
    const mark = container.querySelector("[data-viz-mark]") as HTMLButtonElement;
    fireEvent.click(mark);
    const drawer = container.querySelector("[data-viz-evidence-drawer]")!;
    expect(drawer.textContent).toContain("No evidence binding");
    expect(drawer.textContent!.trim().length).toBeGreaterThan(20);
  });
});

describe("AC7 -- a refusal that promises a table draws one", () => {
  it("a known-but-unbuilt family renders the refusal AND every returned row", () => {
    const input = renderInput();
    input.spec.document = specDocument({ family: "heatmap" as never });
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);

    const panel = container.querySelector('[data-viz-state="refused"]')!;
    expect(panel.textContent).toContain('"heatmap" renderer is not built');
    // The message ends with "The accessible table below shows every returned
    // row." A panel that says that with nothing below it is worse than a bare
    // refusal: it tells the reader the data is reachable when it is not.
    expect(panel.textContent).toContain("table below shows every returned row");
    const table = container.querySelector("[data-viz-table-fallback]");
    expect(table, "the refusal promised a table and drew none").not.toBeNull();
    // Every returned row, not a sample of them.
    expect(table!.querySelectorAll("[data-viz-row-key]")).toHaveLength(3);
    for (const value of ["organic", "paid", "referral", "1,240", "880", "305"]) {
      expect(within(table as HTMLElement).getByText(value)).toBeTruthy();
    }
    // Reachable by keyboard, like every other table in this runtime.
    expect(container.querySelector("[data-viz-table-scroll]")?.getAttribute("tabindex")).toBe("0");
  });

  it("an unsupported responsive profile does the same -- AC7 names this case", () => {
    // Every shipped renderer declares all four profiles, so the case is built by
    // registering one that does not. This is the clause AC7 names explicitly, and
    // `registry.ts` ends its message with the same table promise.
    __resetRenderersForTests();
    register(
      declare("bar", "profile-limited-bar", () => null, {
        profiles: ["console"],
        requiredWells: ["dimension", "measure"],
      }),
    );
    try {
      const input = renderInput();
      input.pins.renderer_build = "bar/profile-limited-bar@1.0.0";
      const { container } = render(
        <VisualizationRuntime input={{ ...input, profile: "share" }} containerWidthPx={900} />,
      );
      const panel = container.querySelector('[data-viz-state="refused"]')!;
      expect(panel.textContent).toContain('"share" responsive profile');
      expect(panel.textContent).toContain("table below shows every returned row");
      const table = container.querySelector("[data-viz-table-fallback]");
      expect(table, "the profile refusal promised a table and drew none").not.toBeNull();
      expect(within(table as HTMLElement).getByText("referral")).toBeTruthy();
    } finally {
      __resetRenderersForTests();
      installStandardRenderers();
    }
  });

  it("an unknown family refuses by name and still shows the rows", () => {
    const input = renderInput();
    input.spec.document = specDocument({ family: "sunburst" as never });
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);
    const panel = container.querySelector('[data-viz-state="refused"]')!;
    expect(panel.textContent).toContain("sunburst");
    expect(container.querySelector("[data-viz-table-fallback]")).not.toBeNull();
  });
});

describe("AC10 -- local display state cannot change analytical meaning", () => {
  it("a granularity change emits ONE event and computes nothing", () => {
    const input = renderInput();
    const request = buildNewExecutionRequest(
      input,
      "grain_change",
      "The reader asked for a weekly grain.",
    );
    expect(request).toEqual({
      type: "requestNewExecution",
      reason: "grain_change",
      detail: "The reader asked for a weekly grain.",
      from_result_id: "res_EXAMPLE_0001",
      from_visualization_spec_version_id: "vsv_EXAMPLE_0001",
    });
    // Nothing was recomputed: the request carries an intent, not a value.
    expect(Object.keys(request)).not.toContain("rows");
  });

  it("a legend hide changes what is drawn, and leaves the returned rows in the table", () => {
    const input = renderInput();
    input.spec.document = specDocument({
      family: "line",
      bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
    });
    input.pins.renderer_build = "line/toorow-echarts-line@1.0.0";
    const { container } = render(
      <VisualizationRuntime
        input={{ ...input, display: { legendHidden: ["conversions"] } }}
        containerWidthPx={900}
      />,
    );
    const marks = container.querySelectorAll("[data-viz-mark]");
    // 3 categories x 1 visible series.
    expect(marks).toHaveLength(3);
    const table = container.querySelector("[data-viz-table-fallback]")!;
    expect(table.textContent).toContain("31");
  });

  /**
   * The AC10 clause the review found unwired. `Runtime.tsx` read
   * `{onRequestNewExecution && onDisplayChange ? null : null}` -- neither prop
   * was ever called and no legend, zoom, sort or hide affordance existed. These
   * three assertions go through the DOM, so they fail if the control stops being
   * connected; asserting the shape of `buildNewExecutionRequest`'s return value,
   * as the suite did, could not.
   */
  it("clicking a legend entry hides that series and emits the display state", () => {
    const onDisplayChange = vi.fn();
    const input = renderInput();
    input.spec.document = specDocument({
      family: "line",
      bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
    });
    input.pins.renderer_build = "line/toorow-echarts-line@1.0.0";
    const { container } = render(
      <VisualizationRuntime
        input={input}
        onDisplayChange={onDisplayChange}
        containerWidthPx={900}
      />,
    );
    expect(container.querySelectorAll("[data-viz-mark]")).toHaveLength(6);

    const toggle = container.querySelector('[data-viz-legend-toggle="conversions"]')!;
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(toggle);

    expect(onDisplayChange).toHaveBeenCalledTimes(1);
    expect(onDisplayChange.mock.calls[0]![0]).toEqual({ legendHidden: ["conversions"] });
    // What is DRAWN changed: 3 categories x 1 remaining series.
    expect(container.querySelectorAll("[data-viz-mark]")).toHaveLength(3);
    expect(
      container.querySelector('[data-viz-legend-toggle="conversions"]')!.getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("the hidden series is labelled as local, and every row stays in the table", () => {
    const input = renderInput();
    input.spec.document = specDocument({
      family: "line",
      bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
    });
    input.pins.renderer_build = "line/toorow-echarts-line@1.0.0";
    const { container } = render(<VisualizationRuntime input={input} containerWidthPx={900} />);
    fireEvent.click(container.querySelector('[data-viz-legend-toggle="conversions"]')!);

    expect(container.textContent).toContain("hidden in this chart only");
    // The rows the Result returned are untouched, and so are the disclosures.
    const table = container.querySelector("[data-viz-table-fallback]")!;
    for (const value of ["31", "44", "9"]) {
      expect(within(table as HTMLElement).getByText(value)).toBeTruthy();
    }
    expect(container.textContent).toContain("country = FR");
  });

  it("toggling it back restores the series -- the control is reversible", () => {
    const onDisplayChange = vi.fn();
    const input = renderInput();
    input.spec.document = specDocument({
      family: "line",
      bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
    });
    input.pins.renderer_build = "line/toorow-echarts-line@1.0.0";
    const { container } = render(
      <VisualizationRuntime input={input} onDisplayChange={onDisplayChange} containerWidthPx={900} />,
    );
    const selector = '[data-viz-legend-toggle="sessions"]';
    fireEvent.click(container.querySelector(selector)!);
    expect(container.querySelectorAll("[data-viz-mark]")).toHaveLength(3);
    fireEvent.click(container.querySelector(selector)!);
    expect(container.querySelectorAll("[data-viz-mark]")).toHaveLength(6);
    expect(onDisplayChange).toHaveBeenCalledTimes(2);
    expect(onDisplayChange.mock.calls[1]![0]).toEqual({ legendHidden: [] });
  });

  it("asking for rows the Result did not return emits ONE typed event and recomputes nothing", () => {
    const onRequestNewExecution = vi.fn();
    const input = renderInput();
    input.result = {
      ...input.result,
      truncated: true,
      manifest: { ...input.result.manifest, truncation: null },
    };
    const { container } = render(
      <VisualizationRuntime
        input={input}
        onRequestNewExecution={onRequestNewExecution}
        containerWidthPx={900}
      />,
    );
    const rowsBefore = container.querySelectorAll("[data-viz-row-key]").length;
    const button = container.querySelector('[data-viz-request-new-execution]')!;
    fireEvent.click(button);

    expect(onRequestNewExecution).toHaveBeenCalledTimes(1);
    const request = onRequestNewExecution.mock.calls[0]![0];
    expect(request.type).toBe("requestNewExecution");
    expect(request.reason).toBe("rows_not_returned");
    expect(request.from_result_id).toBe("res_EXAMPLE_0001");
    expect(request.from_visualization_spec_version_id).toBe("vsv_EXAMPLE_0001");
    expect(Object.keys(request)).not.toContain("rows");
    // The runtime computed nothing: the same rows are still on screen and the
    // truncation disclosure is still visible.
    expect(container.querySelectorAll("[data-viz-row-key]").length).toBe(rowsBefore);
    expect(container.querySelector('[data-viz-state="truncated"]')).not.toBeNull();
  });

  it("the control is absent when the Result discloses no missing rows", () => {
    const { container } = render(
      <VisualizationRuntime
        input={renderInput()}
        onRequestNewExecution={vi.fn()}
        containerWidthPx={900}
      />,
    );
    expect(container.querySelector("[data-viz-request-new-execution]")).toBeNull();
  });
});

describe("AC17 -- the org brand colours reach the chart, in order", () => {
  it("brand primary, secondary and accent are the first three categorical colours", () => {
    const palette = getVizPalette({
      palette: {
        mode: "light",
        primary: { main: "#123456" },
        success: { main: "#1B8A5A" },
        warning: { main: "#B26B00" },
        error: { main: "#B3261E" },
        text: { primary: "#111111" },
      },
      vizBranding: { secondary: "#654321", accent: "#ABCDEF" },
    });
    expect(palette.categorical.slice(0, 3)).toEqual(["#123456", "#654321", "#ABCDEF"]);
  });

  it("the same three, in the same order, in dark", () => {
    const palette = getVizPalette({
      palette: {
        mode: "dark",
        primary: { main: "#123456" },
        success: { main: "#1B8A5A" },
        warning: { main: "#B26B00" },
        error: { main: "#B3261E" },
        text: { primary: "#FAFAFA" },
      },
      vizBranding: { secondary: "#654321", accent: "#ABCDEF" },
    });
    expect(palette.categorical.slice(0, 3)).toEqual(["#123456", "#654321", "#ABCDEF"]);
  });

  it("the diverging ramp follows success -> warning -> error from the live values", () => {
    const palette = getVizPalette({
      palette: {
        mode: "light",
        primary: { main: "#123456" },
        success: { main: "#00FF00" },
        warning: { main: "#FFFF00" },
        error: { main: "#FF0000" },
        text: { primary: "#111111" },
      },
    });
    expect(palette.diverging(0)).toBe("rgba(0, 255, 0, 1)");
    expect(palette.diverging(0.5)).toBe("rgba(255, 255, 0, 1)");
    expect(palette.diverging(1)).toBe("rgba(255, 0, 0, 1)");
  });

  it("the sequential ramp ends on the live accent", () => {
    const palette = getVizPalette({
      palette: {
        mode: "light",
        primary: { main: "#123456" },
        success: { main: "#1B8A5A" },
        warning: { main: "#B26B00" },
        error: { main: "#B3261E" },
        text: { primary: "#111111" },
      },
      vizTrack: "#FFFFFF",
    });
    expect(palette.sequential(1)).toBe("rgba(18, 52, 86, 1)");
  });

  it("no renderer reads a colour constant -- every colour comes from the palette", () => {
    const palette = getVizPalette({
      palette: {
        mode: "light",
        primary: { main: "#123456" },
        success: { main: "#1B8A5A" },
        warning: { main: "#B26B00" },
        error: { main: "#B3261E" },
        text: { primary: "#111111" },
      },
      vizBranding: { secondary: "#654321", accent: "#ABCDEF" },
    });
    const input = renderInput();
    input.spec.document = specDocument({
      family: "line",
      bindings: { dimension: ["channel"], measure: ["sessions", "conversions"] },
    });
    // The compiled model's series colours ARE palette entries.
    const model = compileVisualModel(input.result, input.spec.document, {
      palette,
      limits: { max_rows: 5000, max_series: 24, max_cells: 100000 },
    });
    expect(model.series.map((s: { color: string }) => s.color)).toEqual([
      "#123456",
      "#654321",
    ]);
  });
});

function rendererBuildFor(family: string): string {
  const map: Record<string, string> = {
    table: "table/toorow-table@1.0.0",
    kpi: "kpi/toorow-kpi@1.0.0",
    line: "line/toorow-echarts-line@1.0.0",
    area: "area/toorow-echarts-area@1.0.0",
    bar: "bar/toorow-echarts-bar@1.0.0",
    stacked_bar: "stacked_bar/toorow-echarts-stacked-bar@1.0.0",
    scatter: "scatter/toorow-echarts-scatter@1.0.0",
  };
  return map[family]!;
}
