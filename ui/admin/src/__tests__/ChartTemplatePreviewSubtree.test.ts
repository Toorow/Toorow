/**
 * AC20 — the Chart Template subtree draws through the shared runtime, or it fails here.
 *
 * `visualization-and-rendering.md`, amendment 2026-08-31: "The preview goes
 * through the shared runtime and through nothing else … A preview that drew any
 * other way would be a second engine, and that is the one non-negotiable
 * prohibition of this surface." And in the Incomplete-if list: "a preview, a
 * thumbnail or a template list draws through anything other than the single
 * public entry of the shared runtime".
 *
 * WHY THIS IS STRUCTURAL AND NOT BEHAVIOURAL. A second drawing path is not a bug
 * a render test would notice — the screen would look fine, and that is the whole
 * problem. It is a FILE that exists and an import that resolves. So the check is
 * over the source of the subtree, the way `apiSeamGuard.test.ts` checks the API
 * seam, and it fails on the import rather than on a pixel.
 *
 * WHAT IS ALLOWED. Exactly one drawing import in the whole subtree:
 * `VisualizationMount`, the console's single adapter for the runtime's single
 * public entry (`VisualizationMount.tsx:104`, the same one the Render workbench
 * mounts). Not a renderer, not `@toorow/card-shell/viz` directly, not a chart
 * library, not a canvas.
 */
const SOURCES = import.meta.glob("../analyze/templates/**/*.{ts,tsx}", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

/** Anything that puts a mark on a screen without going through the runtime. */
const FORBIDDEN_IMPORTS: Array<[RegExp, string]> = [
  [/from\s+["'][^"']*\/renderers?\//, "a renderer module"],
  [/from\s+["']echarts/, "the ECharts library"],
  [/from\s+["']d3(-|["'])/, "the D3 library"],
  [/from\s+["']recharts["']/, "the Recharts library"],
  [/from\s+["']chart\.js/, "Chart.js"],
  [/from\s+["']plotly/, "Plotly"],
  [/from\s+["']victory/, "Victory"],
  [/from\s+["']@nivo\//, "Nivo"],
  // The runtime's package is reached through the console's ONE adapter. A file
  // here importing it directly would be a second adapter, which is how two
  // surfaces start pinning different renderer builds for the same family.
  [/from\s+["']@toorow\/card-shell/, "the runtime package directly instead of VisualizationMount"],
];

/** A canvas or a hand-drawn SVG chart is a second engine with no import at all. */
const FORBIDDEN_DRAWING: Array<[RegExp, string]> = [
  [/getContext\(\s*["']2d["']\s*\)/, "a 2D canvas context"],
  [/<canvas[\s>]/, "a <canvas> element"],
  [/<svg[\s>][\s\S]*<(path|circle|rect|line|polyline)[\s>]/, "a hand-drawn SVG chart"],
];

it("has files to check, so an empty glob can never pass this guard", () => {
  expect(Object.keys(SOURCES).length).toBeGreaterThan(0);
});

it("imports no renderer, no chart library and no runtime package in the whole subtree", () => {
  const offenders: string[] = [];
  for (const [path, source] of Object.entries(SOURCES)) {
    for (const [pattern, what] of FORBIDDEN_IMPORTS) {
      if (pattern.test(source)) offenders.push(`${path} imports ${what}`);
    }
  }
  expect(offenders).toEqual([]);
});

it("draws nothing itself — no canvas, no hand-built SVG chart", () => {
  const offenders: string[] = [];
  for (const [path, source] of Object.entries(SOURCES)) {
    for (const [pattern, what] of FORBIDDEN_DRAWING) {
      if (pattern.test(source)) offenders.push(`${path} contains ${what}`);
    }
  }
  expect(offenders).toEqual([]);
});

it("mounts the ONE public entry, from exactly one file", () => {
  const mounting = Object.entries(SOURCES).filter(([, source]) =>
    /from\s+["'][^"']*VisualizationMount["']/.test(source),
  );
  // One file, and it is the preview. A second mounting site in this subtree
  // would be a second answer to "how does a template become pixels".
  expect(mounting.map(([path]) => path.split("/").pop())).toEqual(["TemplatePreview.tsx"]);
});
