/**
 * Story 50.5 AC12 Task -- the AC3, AC5, AC6, AC12, AC16 and AC18 greps, as
 * EXECUTABLE assertions.
 *
 * A grep in a story is a claim a reviewer has to re-run. A grep in a test is a
 * claim a future edit breaks. Everything the acceptance criteria state as
 * "must be empty" is asserted here over the real files on disk.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const VIZ = join(__dirname, "..");
const RENDERERS = join(VIZ, "renderers");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else out.push(full);
  }
  return out;
}

// This file QUOTES every forbidden pattern, so it would match its own greps.
// Excluding it is not a loophole: it contains no runtime code, is never bundled,
// and every other test file stays in scope.
const SELF = "guards.test.ts";
const allFiles = walk(VIZ).filter((f) => /\.(ts|tsx)$/.test(f) && !f.endsWith(SELF));
const sourceFiles = allFiles.filter((f) => !f.includes("__tests__"));
const rendererFiles = walk(RENDERERS).filter((f) => /\.(ts|tsx)$/.test(f));

function hits(files: string[], pattern: RegExp): string[] {
  const found: string[] = [];
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    text.split("\n").forEach((line, i) => {
      if (pattern.test(line)) found.push(`${file}:${i + 1}: ${line.trim()}`);
    });
  }
  return found;
}

describe("AC3 -- the compiler is the only producer of an ECharts option", () => {
  it("no file outside adapters/echarts names setOption or an ECharts option type", () => {
    const outside = sourceFiles.filter((f) => !f.includes(join("adapters", "echarts")));
    expect(hits(outside, /setOption|EChartsOption|EChartsCoreOption/)).toEqual([]);
  });

  it("contracts.ts declares no arbitrary bag", () => {
    const text = readFileSync(join(VIZ, "contracts.ts"), "utf8");
    // `Record<string, CellValue>` is a bounded map of PRIMITIVES and is fine;
    // `Record<string, unknown>` is the bag AC3 forbids.
    expect(text).not.toContain("Record<string, unknown>");
  });
});

describe("AC5 -- D3 calculates, React renders", () => {
  it("D3 never touches the document", () => {
    expect(hits(allFiles, /d3-selection|d3\.select|\.append\(|\.attr\(/)).toEqual([]);
  });

  it("only the layout/geometry modules are imported", () => {
    const imports = hits(sourceFiles, /from "d3-/).map((h) => h.split('from "')[1]!.split('"')[0]!);
    expect([...new Set(imports)].sort()).toEqual(["d3-hierarchy", "d3-shape"]);
  });
});

describe("AC6 -- a renderer cannot query, re-define or inject a frame", () => {
  it("no renderer can reach the network or load code", () => {
    expect(hits(rendererFiles, /fetch\(|XMLHttpRequest|WebSocket|apiFetch|[^.\w]import\(/)).toEqual(
      [],
    );
  });

  it("no renderer aggregates", () => {
    expect(hits(rendererFiles, /aggregate|groupBy|\.reduce\(/)).toEqual([]);
  });

  it("no frame is created anywhere in the runtime", () => {
    expect(hits(allFiles, /<iframe|createElement\("iframe"|srcdoc/)).toEqual([]);
  });
});

describe("AC12 -- ECharts is imported in the tree-shaking form", () => {
  it("the barrel import never appears", () => {
    expect(hits(sourceFiles, /from "echarts"/)).toEqual([]);
  });

  it("echarts/core is imported, and only from the adapter", () => {
    const coreHits = hits(sourceFiles, /from "echarts\/core"/);
    expect(coreHits.length).toBeGreaterThan(0);
    for (const hit of coreHits) {
      expect(hit).toContain(join("adapters", "echarts"));
    }
  });
});

describe("AC18 -- no MUI, no new stylesheet, no new class prefix", () => {
  it("no file under viz imports MUI or emotion", () => {
    expect(hits(allFiles, /@mui\/|@emotion\//)).toEqual([]);
  });

  it("viz contains no stylesheet and imports none", () => {
    expect(walk(VIZ).filter((f) => f.endsWith(".css"))).toEqual([]);
    expect(hits(allFiles, /^import .*\.css/)).toEqual([]);
  });

  it("no hardcoded hex colour in a renderer -- colours come from the live theme", () => {
    expect(hits(rendererFiles, /#[0-9A-Fa-f]{6}\b/)).toEqual([]);
  });
});

describe("AC17 -- there is exactly ONE palette reader", () => {
  it("no second getComputedStyle reader exists in card-shell", () => {
    const cardShellSrc = walk(join(VIZ, "..")).filter(
      (f) => /\.(ts|tsx)$/.test(f) && !f.includes("__tests__"),
    );
    const readers = hits(cardShellSrc, /getComputedStyle/).filter(
      (h) => !h.includes("vizTheme.ts"),
    );
    expect(readers).toEqual([]);
  });
});
