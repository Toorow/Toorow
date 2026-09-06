/**
 * Story 50.5 -- the ONE formatter set every renderer and the table fallback share.
 *
 * WHY SHARED, AND WHY VERSIONED. AC8 requires the Console, the MCP App and the
 * share page to produce the same resolved number and date formats from the same
 * input. If each renderer formatted its own axis labels, "1 234,5" on one surface
 * and "1,234.5" on another would be the same Render disagreeing with itself. So
 * there is one module, it is pinned as `formatter_version`, and a Render replays
 * through the version it pinned.
 *
 * WHAT IT MAY NOT DO. It never rounds a value before it is displayed as a value:
 * `formatNumber` is a display transform on a number the server already computed.
 * There is no `sum`, no `average`, no bucketing and no unit conversion here --
 * those change the answer and belong to the Query Spec.
 *
 * LOCALE. One locale for every surface, pinned with the version. A branded org
 * whose chart reads "1,234" in the Console and "1 234" in a shared link would
 * break AC8 for a reason nobody would think to look for.
 */

import type { CellValue, DateStyle, NumberStyle } from "../contracts";

/** Pinned with `FORMATTER_VERSION`. Changing it is a formatter version bump. */
export const FORMATTER_LOCALE = "en-US";

/** Non-breaking space, written as an escape so a diff cannot hide it. */
export const NBSP = "\u00A0";

function toNumber(value: CellValue): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

const COMPACT_STEPS: [number, string][] = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "k"],
];

function compact(value: number): string {
  const abs = Math.abs(value);
  for (const [factor, suffix] of COMPACT_STEPS) {
    if (abs >= factor) {
      const scaled = value / factor;
      const digits = Math.abs(scaled) >= 100 ? 0 : 1;
      return `${scaled.toLocaleString(FORMATTER_LOCALE, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })}${suffix}`;
    }
  }
  return value.toLocaleString(FORMATTER_LOCALE, { maximumFractionDigits: 2 });
}

/**
 * `unit` comes from the governed member definition or from nowhere
 * (`visualization_specs.py` GRAMMAR `formatting.unit_source`). This function
 * never guesses a currency from a column name.
 */
export function formatNumber(
  value: CellValue,
  style: NumberStyle = "auto",
  unit?: string | null,
): string {
  const n = toNumber(value);
  if (n === null) return value === null || value === undefined ? "--" : String(value);

  switch (style) {
    case "integer":
      return n.toLocaleString(FORMATTER_LOCALE, { maximumFractionDigits: 0 });
    case "decimal":
      return n.toLocaleString(FORMATTER_LOCALE, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    case "percent":
      // The server stores a ratio; the percent STYLE is a display choice, and
      // x100 is the display of that same ratio, not a new computation.
      return `${(n * 100).toLocaleString(FORMATTER_LOCALE, {
        maximumFractionDigits: 1,
      })}%`;
    case "currency":
      if (!unit) {
        // No governed unit -> no invented currency symbol. A "$" the semantic
        // view never declared would be a claim about money we cannot make.
        return n.toLocaleString(FORMATTER_LOCALE, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        });
      }
      try {
        return n.toLocaleString(FORMATTER_LOCALE, { style: "currency", currency: unit });
      } catch {
        return `${n.toLocaleString(FORMATTER_LOCALE, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}${NBSP}${unit}`;
      }
    case "compact":
      return compact(n);
    case "auto":
    default:
      if (Number.isInteger(n)) {
        return Math.abs(n) >= 100000
          ? compact(n)
          : n.toLocaleString(FORMATTER_LOCALE, { maximumFractionDigits: 0 });
      }
      return n.toLocaleString(FORMATTER_LOCALE, { maximumFractionDigits: 2 });
  }
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

function parseDate(value: CellValue): Date | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  // A DATE everywhere, never an hour (`date-grain-no-hourly`): a bare
  // `YYYY-MM-DD` is parsed as UTC midnight and rendered from UTC parts, so no
  // timezone shift can move a day.
  const iso = DATE_ONLY.test(trimmed) ? `${trimmed}T00:00:00Z` : trimmed;
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

const MONTHS_LONG = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

export function formatDate(value: CellValue, style: DateStyle = "auto"): string {
  const date = parseDate(value);
  if (!date) return value === null || value === undefined ? "--" : String(value);

  const y = date.getUTCFullYear();
  const m = date.getUTCMonth();
  const d = date.getUTCDate();
  const pad = (n: number) => String(n).padStart(2, "0");

  switch (style) {
    case "iso":
      return `${y}-${pad(m + 1)}-${pad(d)}`;
    case "short":
      return `${MONTHS[m]}${NBSP}${d}`;
    case "long":
      return `${MONTHS_LONG[m]}${NBSP}${d}, ${y}`;
    case "month":
      return `${MONTHS[m]}${NBSP}${y}`;
    case "year":
      return String(y);
    case "auto":
    default:
      return `${MONTHS[m]}${NBSP}${d}, ${y}`;
  }
}

/** Seconds -> a bounded, readable duration. No locale plural machinery. */
export function formatDuration(value: CellValue): string {
  const seconds = toNumber(value);
  if (seconds === null) return value === null || value === undefined ? "--" : String(value);
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

/** True when a column looks like a date column BY ITS VALUES, for display only. */
export function looksLikeDate(value: CellValue): boolean {
  return typeof value === "string" && DATE_ONLY.test(value.trim());
}

/**
 * The one entry every renderer and the table fallback call. `isDate` is decided
 * by the CALLER from the binding, never guessed inside a renderer -- a role
 * inferred from a value shape is the invented role the contracts file forbids.
 */
export function formatCell(
  value: CellValue,
  opts: { numberStyle?: NumberStyle; dateStyle?: DateStyle; isDate?: boolean; unit?: string | null },
): string {
  if (value === null || value === undefined) return "--";
  if (opts.isDate) return formatDate(value, opts.dateStyle ?? "auto");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return formatNumber(value, opts.numberStyle ?? "auto", opts.unit);
  return String(value);
}

// ---------------------------------------------------------------------------
// Story 76-8 — the card and widget surfaces join this module.
//
// WHY THEY WERE OUTSIDE IT, and what it cost. Measured 2026-09-05 at HEAD
// 368f910f: 30 `toLocaleString("fr-FR")` call sites across `ui/cards/**` and
// `ui/widgets/**`, plus `pivotMatrix.tsx` calling `new Intl.NumberFormat(
// undefined, …)` — the READER'S OS locale, so the same Render printed a
// different number on two machines, which is the exact failure AC8 named. The
// visible symptom was smaller and worse: `card-keywords` printed « 3 842 » and
// « 2,4 » (comma) beside « +8.8 % » (point) IN THE SAME CARD, because the
// grouped values went through `fr-FR` while the deltas went through
// `Number.prototype.toFixed`, which is locale-blind by specification.
//
// THE VERSION IS NOT BUMPED BY THIS ADDITION, and the rule is the one story
// 50.5 wrote: `FORMATTER_VERSION` moves when an EXISTING input starts printing
// a different output. Everything below is new API; `formatNumber`, `formatDate`,
// `formatDuration`, `looksLikeDate` and `formatCell` are untouched, so a Render
// frozen on `viz-formatters@1` replays byte-identically. What does move is the
// runtime content hash (`scripts/runtime-identity.mjs` hashes `src/viz/**`),
// and that is the correct signal: the bundle changed, the formatting contract
// did not.
//
// ONE CONVENTION, NOT TWO. `formatPercent` prints `19.6%` with no space, the
// way `formatNumber(v, "percent")` already did, so a card cannot show a percent
// in two shapes depending on which primitive drew it.
// ---------------------------------------------------------------------------

/**
 * The absent value. Same dash the card primitives already printed by hand;
 * `formatNumber` keeps its own `--` because changing it would be a version bump
 * for no reader's benefit.
 */
export const NO_VALUE = "—";

export interface ValueFormatOptions {
  minimumFractionDigits?: number;
  maximumFractionDigits?: number;
  /** Prefix a non-negative value with `+`. Used by movers and deltas. */
  signed?: boolean;
}

/**
 * A plain grouped number — the direct replacement for a bare
 * `value.toLocaleString(…)` in a card primitive.
 *
 * `null`/`undefined`/non-finite is an ABSENCE and prints `NO_VALUE`; it never
 * becomes `0`, because a zero a reader can act on and a measure nobody took are
 * not the same fact (AD-9).
 */
export function formatValue(
  value: number | null | undefined,
  options: ValueFormatOptions = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  const { signed = false, ...digits } = options;
  const body = value.toLocaleString(FORMATTER_LOCALE, {
    maximumFractionDigits: 3,
    ...digits,
  });
  return signed && value >= 0 ? `+${body}` : body;
}

/** `k` / `M` / `B` / `T`. Exported so an axis and a tile abbreviate identically. */
export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  return compact(value);
}

/**
 * A percentage whose input is ALREADY in percent units (`19.6` -> `19.6%`),
 * which is what every card block carries: `delta_pct`, `rate`, `part_pct`. A
 * RATIO (`0.196`) goes through `formatNumber(v, "percent")`; the two are
 * different inputs and this module refuses to guess which one it was handed.
 */
export function formatPercent(
  value: number | null | undefined,
  options: { digits?: number; signed?: boolean } = {},
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  const { digits = 1, signed = false } = options;
  const body = value.toLocaleString(FORMATTER_LOCALE, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${signed && value >= 0 ? "+" : ""}${body}%`;
}

/**
 * The ISO currency codes the RUNTIME knows, read from the platform rather than
 * written down here — a currency catalogue shipped with the product is code the
 * repository would have to maintain against nobody's authority. On a runtime
 * without `Intl.supportedValuesOf` the set is empty and every unit is treated as
 * a plain word, which under-decorates rather than inventing a currency.
 */
const ISO_CURRENCIES: ReadonlySet<string> = new Set(
  typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("currency") : [],
);

/** True when a governed `unit` names money, e.g. `EUR`. `POS` is not a currency. */
export function isCurrencyCode(unit: string | null | undefined): boolean {
  return !!unit && ISO_CURRENCIES.has(unit.toUpperCase());
}

/**
 * An amount, never bare. `30EUR` — measured on `card-conversions` — is the
 * defect this closes: a currency is either the locale's own symbol or the code
 * separated from the digits, and never glued to them.
 *
 * Cents appear only when the amount HAS cents: an integer number of euros is a
 * hero figure, and `€9,600.00` reads as precision nobody claimed.
 */
export function formatCurrency(
  amount: number | null | undefined,
  currency?: string | null,
): string {
  if (amount === null || amount === undefined || !Number.isFinite(amount)) return NO_VALUE;
  const fractionDigits = Number.isInteger(amount) ? 0 : 2;
  if (!currency) {
    // No governed unit -> no invented symbol, the rule `formatNumber` already
    // states for its `currency` style.
    return formatValue(amount, {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
  }
  try {
    return amount.toLocaleString(FORMATTER_LOCALE, {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
  } catch {
    return `${formatValue(amount, {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    })}${NBSP}${currency}`;
  }
}

/**
 * One value plus whatever unit the governed definition attached to it. This is
 * the entry a primitive calls when it holds a `unit` prop and does not know what
 * kind of unit it is: money takes the currency shape, `%` takes the percent
 * shape, and anything else is a word set off from the digits by a non-breaking
 * space — never concatenated.
 */
export function formatMeasure(
  value: number | null | undefined,
  unit?: string | null,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE;
  if (isCurrencyCode(unit)) return formatCurrency(value, unit);
  const trimmed = unit?.trim();
  if (trimmed === "%") return formatPercent(value);
  const body = formatValue(value);
  return trimmed ? `${body}${NBSP}${trimmed}` : body;
}

/**
 * WHETHER A UNIT IS WORTH PRINTING BESIDE A METRIC'S OWN NAME.
 *
 * « CLICS (CLICS) », « CONVERSIONS (CONVERSIONS) », « SESSIONS (SÉANCES) » —
 * three headers measured on 2026-09-05 that ask the same question twice. The
 * rule is derivable rather than a list of forbidden words: a unit earns its
 * parenthesis when it changes HOW THE NUMBER READS (a currency, a percent, a
 * ratio, anything carrying a symbol). A unit that is a plain word only restates
 * what the label already named, and is dropped.
 */
export function unitWorthShowing(unit: string | null | undefined): boolean {
  const trimmed = unit?.trim();
  if (!trimmed) return false;
  if (isCurrencyCode(trimmed)) return true;
  // A bare word (letters, spaces, hyphens, apostrophes) restates the metric.
  return !/^[\p{L}\p{M}\s'’-]+$/u.test(trimmed);
}
