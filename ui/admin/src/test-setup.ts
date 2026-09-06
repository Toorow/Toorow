/**
 * Vitest test setup for admin console (Story 2.4, AC8).
 * Imports jest-dom matchers for DOM assertions.
 */
import "@testing-library/jest-dom";
import { configure } from "@testing-library/react";

/**
 * Testing Library waits 1s by default for `findBy*` / `waitFor`. That budget is
 * not meaningful in this suite: `shell/ContentRouter.tsx` mounts its screens
 * behind 36 `React.lazy()` boundaries, and the chunk behind the route under
 * test is transformed on demand, inside the worker, while up to one fork per
 * core is doing the same thing. When the box is loaded the fallback
 * (`<div role="status">Loading workspace…</div>`) is still on screen at 1s, and
 * the assertion fails against it — a red that says nothing about the product.
 *
 * Measured on this tree: three consecutive full runs, same commit, gave 0, 0
 * and 5 such failures across SemanticModel, EvidenceIndex and GovernanceScreens
 * — all nine ContentRouter-mounting suites are exposed. A suite whose verdict
 * depends on machine load is as unusable as one that never finishes.
 *
 * This raises the FLOOR only. It weakens no assertion: the same text must still
 * appear, and a screen that never renders still fails — 4s later. The four
 * KnowledgeGraphPage suites already do exactly this per file (elk + React Flow
 * settle over several frames); the value belongs in the shared setup so the
 * next suite does not have to rediscover it. Files needing more still call
 * `configure()` themselves — a local call wins over this one.
 */
configure({ asyncUtilTimeout: 5000 });

/**
 * jsdom has no ResizeObserver, and several screens (anything on React Flow, the
 * scroll areas) construct one on mount.
 *
 * NOT `vi.stubGlobal`: stubbing records the ORIGINAL value — here `undefined` —
 * and any file whose `afterEach` calls `vi.unstubAllGlobals()` then restores
 * that `undefined`, deleting whatever shim the file installed for itself. The
 * four KnowledgeGraphPage suites install a richer observer by direct assignment
 * for exactly this reason (see the comment in KnowledgeGraphPage.test.tsx), and
 * a stubbed global here silently wiped it between their tests. A plain
 * assignment is invisible to the stub registry and survives.
 */
class TestResizeObserver implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!("ResizeObserver" in globalThis)) {
  (globalThis as { ResizeObserver?: typeof ResizeObserver }).ResizeObserver =
    TestResizeObserver as unknown as typeof ResizeObserver;
}

/**
 * jsdom implements no canvas context, and ECharts needs one to initialise.
 *
 * THIS LIVED ONLY IN `ui/cards/shell/src/test-setup.ts` until 2026-08-04, when
 * the console started mounting the shared Visualization runtime in page
 * (`analyze-artifacts/Renders.tsx` → `analyze/VisualizationMount.tsx`). Without
 * it, an admin test that draws a chart does not fail an assertion — zrender
 * throws `Cannot read properties of null (reading 'clearRect')` from inside a
 * React effect, which vitest reports as an UNCAUGHT EXCEPTION attributed to
 * whichever test happened to be running. It belongs in the shared setup rather
 * than in the one suite that first hit it: every future console screen that
 * mounts the runtime needs it, and rediscovering it costs an afternoon.
 *
 * The stub is a NO-OP 2D context, so ECharts initialises, computes its layout
 * and emits its events; it paints nothing. WHAT THAT DOES NOT PROVE: no pixel of
 * this runtime has been drawn and looked at in this repository. These suites
 * prove structure, semantics, evidence and refusals — never appearance.
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
