import "@testing-library/jest-dom/vitest";

/**
 * Story 50.5 -- jsdom implements no canvas context, and ECharts needs one to
 * initialise. Without this stub every chart test would exercise the runtime's
 * error path instead of its real path, and a green suite would prove the wrong
 * thing. The stub returns a NO-OP 2D context: ECharts initialises, computes its
 * layout and emits its events; it simply paints nothing, which is exactly what a
 * headless assertion can and cannot check.
 *
 * WHAT THIS MEANS FOR THE PIXELS. An earlier version of this note said they were
 * "proven by the real browser path recorded in the Dev Agent Record". They are
 * not: that record states at its own line 1381 that the browser path was NOT
 * executed. Nothing in this repository has drawn a pixel of this runtime and
 * looked at it. The suite proves structure, semantics, evidence and refusals; a
 * visual check of the canvas remains unproven, and is named as unproven rather
 * than credited to a run that never happened.
 */
if (typeof HTMLCanvasElement !== "undefined") {
  const noop = () => undefined;
  const context: Record<string, unknown> = new Proxy(
    {
      canvas: null,
      measureText: () => ({ width: 0, actualBoundingBoxAscent: 0, actualBoundingBoxDescent: 0 }),
      createLinearGradient: () => ({ addColorStop: noop }),
      createRadialGradient: () => ({ addColorStop: noop }),
      createPattern: () => null,
      getImageData: () => ({ data: new Uint8ClampedArray(4) }),
      putImageData: noop,
      setLineDash: noop,
      getLineDash: () => [],
      isPointInPath: () => false,
      isPointInStroke: () => false,
    },
    {
      get(target, key) {
        if (key in target) return (target as Record<string | symbol, unknown>)[key];
        return noop;
      },
      set() {
        return true;
      },
    },
  );
  HTMLCanvasElement.prototype.getContext = function getContext(
    this: HTMLCanvasElement,
  ): unknown {
    (context as { canvas: unknown }).canvas = this;
    return context;
  } as unknown as typeof HTMLCanvasElement.prototype.getContext;
}
