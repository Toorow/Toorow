/**
 * Story 50.5 -- the React boundary around one ECharts instance.
 *
 * React owns the DOM node; ECharts owns the pixels inside it and nothing else.
 * The component mounts, sets the compiled option, and disposes on unmount. It
 * takes no children, exposes no imperative handle and never receives an option
 * from a caller -- the option comes from `buildEChartsOption`, which is the only
 * producer (AC3).
 *
 * ACCESSIBILITY. A canvas is not an accessible chart, and this file does not
 * pretend otherwise: the canvas is `aria-hidden`, the accessible name and the
 * data live in the table fallback every visual carries (AC13). Keyboard evidence
 * traversal is provided by the renderer's own focusable mark list, not by the
 * canvas.
 *
 * jsdom. ECharts needs a canvas context that jsdom does not implement. `init` is
 * wrapped so a test environment renders the surrounding React tree -- states,
 * disclosures, legend, table fallback -- instead of throwing. The failure is
 * recorded in a data attribute so a test can assert it happened rather than
 * silently passing.
 */

import { useEffect, useRef } from "react";
import type { EChartsCoreOption } from "echarts/core";

import { echarts } from "./core";

export interface EChartsCanvasProps {
  option: EChartsCoreOption;
  height: number;
  /** Called with the ECharts `dataIndex`/`seriesIndex` of the mark under the pointer. */
  onMarkHit?: (seriesIndex: number, dataIndex: number) => void;
  onMarkLeave?: () => void;
  onMarkActivate?: (seriesIndex: number, dataIndex: number) => void;
}

export default function EChartsCanvas(props: EChartsCanvasProps) {
  const { option, height, onMarkHit, onMarkLeave, onMarkActivate } = props;
  const host = useRef<HTMLDivElement | null>(null);
  const chart = useRef<ReturnType<typeof echarts.init> | null>(null);
  const unavailable = useRef<string | null>(null);

  useEffect(() => {
    const node = host.current;
    if (!node) return undefined;
    try {
      chart.current = echarts.init(node, undefined, { renderer: "canvas" });
    } catch (error) {
      unavailable.current = error instanceof Error ? error.message : String(error);
      node.setAttribute("data-echarts-unavailable", "true");
      return undefined;
    }
    const instance = chart.current;
    instance.on("mouseover", (params: { seriesIndex?: number; dataIndex?: number }) => {
      if (onMarkHit && typeof params.seriesIndex === "number" && typeof params.dataIndex === "number") {
        onMarkHit(params.seriesIndex, params.dataIndex);
      }
    });
    instance.on("mouseout", () => onMarkLeave?.());
    instance.on("click", (params: { seriesIndex?: number; dataIndex?: number }) => {
      if (onMarkActivate && typeof params.seriesIndex === "number" && typeof params.dataIndex === "number") {
        onMarkActivate(params.seriesIndex, params.dataIndex);
      }
    });

    const observer =
      typeof ResizeObserver === "function"
        ? new ResizeObserver(() => instance.resize())
        : null;
    if (observer) observer.observe(node);

    return () => {
      observer?.disconnect();
      instance.dispose();
      chart.current = null;
    };
    // The handlers are stable for the life of one mount; re-subscribing on every
    // render would leak listeners into the same instance.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // `notMerge` on purpose: a Render is replaced, never merged into. A merged
    // option would leave a series from the previous spec alive on the canvas.
    chart.current?.setOption(option, { notMerge: true });
  }, [option]);

  useEffect(() => {
    chart.current?.resize();
  }, [height]);

  return (
    <div
      ref={host}
      aria-hidden="true"
      data-viz-canvas="true"
      style={{ width: "100%", height: `${height}px` }}
    />
  );
}
