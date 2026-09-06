/**
 * Story 76-8 — the ONE formatter set, and the gate that keeps it one.
 *
 * The file lives under `__tests__/` on purpose: `scripts/runtime-identity.mjs`
 * excludes that directory from the runtime content hash, so a test added here
 * does not move a build identity that no bundle changed.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

import { describe, expect, it } from "vitest";

import {
  FORMATTER_LOCALE,
  NO_VALUE,
  formatCompact,
  formatCurrency,
  formatMeasure,
  formatNumber,
  formatPercent,
  formatValue,
  isCurrencyCode,
  unitWorthShowing,
} from "../theme/formatters";

describe("formatValue", () => {
  it("groups on the pinned locale, never on the reader's", () => {
    expect(FORMATTER_LOCALE).toBe("en-US");
    expect(formatValue(3842)).toBe("3,842");
    expect(formatValue(268981)).toBe("268,981");
  });

  it("says the absence instead of inventing a zero (AD-9)", () => {
    expect(formatValue(null)).toBe(NO_VALUE);
    expect(formatValue(undefined)).toBe(NO_VALUE);
    expect(formatValue(Number.NaN)).toBe(NO_VALUE);
    expect(formatValue(Number.POSITIVE_INFINITY)).toBe(NO_VALUE);
    expect(formatValue(0)).toBe("0");
  });

  it("signs a non-negative value only when the caller asks", () => {
    expect(formatValue(3.2, { signed: true, maximumFractionDigits: 1 })).toBe("+3.2");
    expect(formatValue(-3.2, { signed: true, maximumFractionDigits: 1 })).toBe("-3.2");
    expect(formatValue(3.2, { maximumFractionDigits: 1 })).toBe("3.2");
  });

  it("keeps the decimals the caller asked for", () => {
    expect(formatValue(2.4, { minimumFractionDigits: 1, maximumFractionDigits: 1 })).toBe("2.4");
    expect(formatValue(1234.5678)).toBe("1,234.568");
  });
});

describe("formatPercent", () => {
  it("takes percent units, not a ratio, and agrees with formatNumber's percent style", () => {
    expect(formatPercent(19.6)).toBe("19.6%");
    // The same quantity expressed as a ratio goes through the other entry.
    expect(formatNumber(0.196, "percent")).toBe("19.6%");
  });

  it("signs and rounds on demand, and never blanks a zero", () => {
    expect(formatPercent(8.8, { signed: true })).toBe("+8.8%");
    expect(formatPercent(-1.3, { signed: true })).toBe("-1.3%");
    expect(formatPercent(0)).toBe("0.0%");
    expect(formatPercent(3, { digits: 0 })).toBe("3%");
    expect(formatPercent(null)).toBe(NO_VALUE);
  });
});

describe("formatCurrency", () => {
  it("never glues the code to the digits (the `30EUR` defect)", () => {
    const printed = formatCurrency(30, "EUR");
    expect(printed).not.toBe("30EUR");
    expect(printed).toContain("30");
    expect(printed).toMatch(/[€]|EUR/);
  });

  it("shows cents only when the amount has cents", () => {
    expect(formatCurrency(9600, "EUR")).toBe("€9,600");
    expect(formatCurrency(1240.5, "EUR")).toBe("€1,240.50");
  });

  it("falls back to the code beside the number when the code is unknown", () => {
    expect(formatCurrency(1240, "XYZ")).toMatch(/1,240\s?XYZ|XYZ\s?1,240/);
  });

  it("invents no symbol when no currency is governed", () => {
    expect(formatCurrency(30, null)).toBe("30");
    expect(formatCurrency(null, "EUR")).toBe(NO_VALUE);
  });
});

describe("isCurrencyCode / unitWorthShowing", () => {
  it("reads the currency catalogue from the runtime, not from a table here", () => {
    expect(isCurrencyCode("EUR")).toBe(true);
    expect(isCurrencyCode("usd")).toBe(true);
    expect(isCurrencyCode("POS")).toBe(false);
    expect(isCurrencyCode(null)).toBe(false);
  });

  it("drops a unit that only restates the metric it sits beside", () => {
    // « CLICS (CLICS) », « SESSIONS (SÉANCES) », « CONVERSIONS (CONVERSIONS) ».
    expect(unitWorthShowing("clics")).toBe(false);
    expect(unitWorthShowing("séances")).toBe(false);
    expect(unitWorthShowing("utilisateurs")).toBe(false);
    expect(unitWorthShowing("position")).toBe(false);
    expect(unitWorthShowing(undefined)).toBe(false);
  });

  it("keeps a unit that changes how the number reads", () => {
    expect(unitWorthShowing("EUR")).toBe(true);
    expect(unitWorthShowing("%")).toBe(true);
    expect(unitWorthShowing("x (réclamé / réel)")).toBe(true);
  });
});

describe("formatMeasure", () => {
  it("routes money, percent and words to their own shapes", () => {
    expect(formatMeasure(30, "EUR")).toBe(formatCurrency(30, "EUR"));
    expect(formatMeasure(19.6, "%")).toBe("19.6%");
    // A word unit is set off from the digits, never concatenated.
    expect(formatMeasure(1200, "clics")).toBe("1,200 clics");
    expect(formatMeasure(1200)).toBe("1,200");
    expect(formatMeasure(null, "EUR")).toBe(NO_VALUE);
  });
});

describe("formatCompact", () => {
  it("abbreviates on the pinned locale", () => {
    expect(formatCompact(1234)).toBe("1.2k");
    // 1 decimal below 100, none above — the rule `compact` already pinned.
    expect(formatCompact(34_000_000)).toBe("34.0M");
    expect(formatCompact(340_000_000)).toBe("340M");
    expect(formatCompact(null)).toBe(NO_VALUE);
  });
});

// ---------------------------------------------------------------------------
// The gate. Story 76-8 acceptance: zero `fr-FR`, zero direct locale call in
// `ui/cards/**` and `ui/widgets/**` outside this module and `canonicalAmount.ts`.
//
// It reads the SOURCE TREE rather than the imports, because the defect it
// refuses is a call site somebody adds by hand in a primitive at 2 a.m., not a
// missing dependency.
// ---------------------------------------------------------------------------

const UI_ROOT = resolve(__dirname, "..", "..", "..", "..", "..");

/** The two modules allowed to speak to `Intl` on these surfaces. */
const ALLOWED = new Set([
  "cards/shell/src/viz/theme/formatters.ts",
  "cards/shell/src/viz/theme/canonicalAmount.ts",
]);

const LOCALE_CALL = /\btoLocale(?:String|DateString|TimeString)\s*\(|\bIntl\s*\./;
const FRENCH_LOCALE = /\bfr-FR\b/;

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === "node_modules" || entry === "dist" || entry === "__tests__") continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      sourceFiles(full, out);
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

function surfaceFiles(): { key: string; text: string }[] {
  const roots = [join(UI_ROOT, "cards"), join(UI_ROOT, "widgets")];
  const files: { key: string; text: string }[] = [];
  for (const root of roots) {
    for (const pkg of readdirSync(root)) {
      const src = join(root, pkg, "src");
      let isDir = false;
      try {
        isDir = statSync(src).isDirectory();
      } catch {
        isDir = false;
      }
      if (!isDir) continue;
      for (const file of sourceFiles(src)) {
        files.push({
          key: relative(UI_ROOT, file).split(sep).join("/"),
          text: readFileSync(file, "utf8"),
        });
      }
    }
  }
  return files;
}

describe("one formatter for the consumer renders (76-8 gate)", () => {
  it("finds the surfaces it is supposed to guard", () => {
    const files = surfaceFiles();
    expect(files.length).toBeGreaterThan(40);
    expect(files.map((f) => f.key)).toContain("cards/shell/src/viz/theme/formatters.ts");
    expect(files.map((f) => f.key)).toContain("widgets/google-analytics/src/KpiTile.tsx");
  });

  it("no card or widget source calls a locale API outside the two allowed modules", () => {
    const offenders = surfaceFiles()
      .filter((f) => !ALLOWED.has(f.key) && LOCALE_CALL.test(f.text))
      .map((f) => f.key);
    expect(offenders).toEqual([]);
  });

  it("no card or widget source names `fr-FR`", () => {
    // `formatters.ts` names it in prose, to record the defect it closed.
    const offenders = surfaceFiles()
      .filter((f) => !ALLOWED.has(f.key) && FRENCH_LOCALE.test(f.text))
      .map((f) => f.key);
    expect(offenders).toEqual([]);
  });
});
