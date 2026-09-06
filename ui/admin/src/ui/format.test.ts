/**
 * `ui/format` — the table of `console-presentation.md` §2, case by case.
 *
 * WHAT IS ASSERTED is the rule, not the implementation: every row of the §2
 * table has its example here as a literal, because the examples ARE the
 * ratified decision and a change to any of them has to be a change to the
 * document first.
 *
 * The edge cases are the ones that silently corrupt a screen: an absence
 * printed as `0`, a ratio printed as a percentage (or the reverse — a factor
 * of a hundred), a currency code the browser has no symbol for, `-0` from a
 * subtraction, and the boundary of every step in the byte and duration
 * ladders. `Intl` is NOT used to derive the expectations: a test that computes
 * its expectation the way the code does asserts nothing.
 */
import { describe, expect, it } from "vitest";

import {
  CONSOLE_LOCALE,
  NO_VALUE,
  THIN_SPACE,
  formatBytes,
  formatCompact,
  formatCount,
  formatCurrency,
  formatDuration,
  formatNumber,
  formatPercent,
} from "./format";

describe("the two constants the whole console reads", () => {
  it("pins one locale, and it is the one `Timestamp` already pinned", () => {
    expect(CONSOLE_LOCALE).toBe("en-GB");
  });

  it("spells the absent value as the em dash, never as a blank or a zero", () => {
    expect(NO_VALUE).toBe("—");
  });
});

describe("formatNumber", () => {
  it("groups, and keeps the caller's digits — §2: `12,480` · `0.42`", () => {
    expect(formatNumber(12480)).toBe("12,480");
    expect(formatNumber(0.42)).toBe("0.42");
  });

  it("adds no rounding of its own", () => {
    expect(formatNumber(1234.5)).toBe("1,234.5");
    expect(formatNumber(0.125, { maximumFractionDigits: 2 })).toBe("0.13");
    expect(formatNumber(3, { minimumFractionDigits: 2 })).toBe("3.00");
  });

  it("renders every absence as the dash, and never `NaN` or `Infinity`", () => {
    for (const value of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(formatNumber(value)).toBe(NO_VALUE);
    }
  });

  it("renders a measured zero as `0` — an absence is the dash, a zero is a measurement", () => {
    expect(formatNumber(0)).toBe("0");
    // `-0` comes out of a subtraction, never out of a measurement.
    expect(formatNumber(-0)).toBe("0");
  });

  it("keeps a negative amount negative, and groups it", () => {
    expect(formatNumber(-12480)).toBe("-12,480");
  });

  it("groups a number large enough to have stopped being read", () => {
    expect(formatNumber(1e12)).toBe("1,000,000,000,000");
  });
});

describe("formatCompact", () => {
  it("is the §2 examples — `1.2k` · `34M`", () => {
    expect(formatCompact(1200)).toBe("1.2k");
    expect(formatCompact(34_000_000)).toBe("34M");
  });

  it("spells the thousand step in lower case, which `Intl` does not", () => {
    expect(formatCompact(1500)).toBe("1.5k");
    expect(formatCompact(1500)).not.toContain("K");
  });

  it("takes one decimal below 100 and none above, on the COMPACT figure", () => {
    expect(formatCompact(123_400)).toBe("123k");
    expect(formatCompact(1_500_000_000)).toBe("1.5B");
    expect(formatCompact(1e12)).toBe("1T");
  });

  it("does not compact what is already short, and loses no digit doing it", () => {
    expect(formatCompact(999)).toBe("999");
    expect(formatCompact(0.42)).toBe("0.42");
    expect(formatCompact(0)).toBe("0");
  });

  it("answers the dash for an absence", () => {
    expect(formatCompact(null)).toBe(NO_VALUE);
    expect(formatCompact(Number.NaN)).toBe(NO_VALUE);
  });
});

describe("formatPercent", () => {
  it("takes a RATIO and is the §2 example — `42.0 %`", () => {
    expect(formatPercent(0.42)).toBe(`42.0${THIN_SPACE}%`);
  });

  it("puts a thin no-break space before the sign, so the two never split", () => {
    expect(THIN_SPACE).toBe(" ");
    expect(formatPercent(0.42)).toContain(" %");
  });

  it("renders a measured zero as `0.0 %`, never blank — §2 says so in words", () => {
    expect(formatPercent(0)).toBe(`0.0${THIN_SPACE}%`);
    expect(formatPercent(-0)).toBe(`0.0${THIN_SPACE}%`);
  });

  it("holds the digits fixed, so a column lines up on its decimal point", () => {
    expect(formatPercent(0.5, { digits: 2 })).toBe(`50.00${THIN_SPACE}%`);
    expect(formatPercent(0.5, { digits: 0 })).toBe(`50${THIN_SPACE}%`);
  });

  it("takes an already-multiplied figure only when the caller says so", () => {
    // The one mistake this function exists to prevent: 42 read as a ratio is
    // 4,200 %. Nothing guesses — `scaled` is written at the call site.
    expect(formatPercent(42)).toBe(`4,200.0${THIN_SPACE}%`);
    expect(formatPercent(42, { scaled: true })).toBe(`42.0${THIN_SPACE}%`);
  });

  it("answers the dash for an absence, and never a zero", () => {
    expect(formatPercent(null)).toBe(NO_VALUE);
    expect(formatPercent(undefined)).toBe(NO_VALUE);
    expect(formatPercent(Number.NaN)).toBe(NO_VALUE);
    expect(formatPercent(Number.POSITIVE_INFINITY)).toBe(NO_VALUE);
  });

  it("keeps a negative ratio negative", () => {
    expect(formatPercent(-0.013)).toBe(`-1.3${THIN_SPACE}%`);
  });
});

describe("formatCurrency", () => {
  it("is the §2 examples — `€30` · `£1,240.50`", () => {
    expect(formatCurrency(30, "EUR")).toBe("€30.00");
    expect(formatCurrency(1240.5, "GBP")).toBe("£1,240.50");
  });

  it("renders the number then the code when the browser has no symbol — §2: `1,240 XXX`", () => {
    // `XXX` is the ISO code for "no currency". `Intl` accepts it and answers
    // the generic sign `¤`, which tells a reader nothing.
    expect(formatCurrency(1240, "XXX")).toBe("1,240 XXX");
    expect(formatCurrency(1240, "XXX")).not.toContain("¤");
  });

  it("renders the number then the code when the code is not a currency at all", () => {
    // `Intl` throws a RangeError here rather than answering.
    expect(formatCurrency(1240, "NOPE")).toBe("1,240 NOPE");
  });

  it("never prints a bare number when a code was given, and never throws", () => {
    for (const code of ["XXX", "NOPE", "€", "12"]) {
      expect(formatCurrency(1240, code)).toContain(code.toUpperCase());
    }
  });

  it("prints the figure alone when the server named no currency at all", () => {
    // The caller owns the sentence that says why — `evidence.ts#moneyText`
    // refuses to print the figure at all, and that refusal stays its own.
    expect(formatCurrency(1240, null)).toBe("1,240");
    expect(formatCurrency(1240, "")).toBe("1,240");
  });

  it("keeps a negative amount, a zero and an absence distinct", () => {
    expect(formatCurrency(-30, "EUR")).toBe("-€30.00");
    expect(formatCurrency(0, "EUR")).toBe("€0.00");
    expect(formatCurrency(null, "EUR")).toBe(NO_VALUE);
    expect(formatCurrency(Number.NaN, "EUR")).toBe(NO_VALUE);
  });

  it("divides nothing — the amount arrives already converted", () => {
    // §2: "`formatCurrency` takes a number the server already converted; it
    // never divides micros."
    expect(formatCurrency(30_000_000, "EUR")).toBe("€30,000,000.00");
  });
});

describe("formatBytes", () => {
  it("is the §2 example — `4.2 MB`", () => {
    expect(formatBytes(4.2 * 1024 * 1024)).toBe("4.2 MB");
  });

  it("steps on the binary boundary, at every step", () => {
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 * 1024 - 1)).toBe("1024 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytes(1024 ** 3)).toBe("1.0 GB");
    expect(formatBytes(1024 ** 4)).toBe("1.0 TB");
    expect(formatBytes(1024 ** 5)).toBe("1.0 PB");
  });

  it("takes one decimal below 10 and none above", () => {
    expect(formatBytes(9.5 * 1024)).toBe("9.5 KB");
    expect(formatBytes(42 * 1024)).toBe("42 KB");
  });

  it("renders a zero and an absence differently", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(null)).toBe(NO_VALUE);
    expect(formatBytes(Number.NaN)).toBe(NO_VALUE);
  });
});

describe("formatDuration", () => {
  it("is every form of the §2 ladder", () => {
    expect(formatDuration(400)).toBe("< 1 s");
    expect(formatDuration(12_000)).toBe("12 s");
    expect(formatDuration(245_000)).toBe("4 min 05 s");
    expect(formatDuration(8_040_000)).toBe("2 h 14 min");
    expect(formatDuration(273_600_000)).toBe("3 d 4 h");
  });

  it("changes form exactly on each boundary", () => {
    expect(formatDuration(999)).toBe("< 1 s");
    expect(formatDuration(1000)).toBe("1 s");
    expect(formatDuration(59_999)).toBe("59 s");
    expect(formatDuration(60_000)).toBe("1 min 00 s");
    expect(formatDuration(3_599_999)).toBe("59 min 59 s");
    expect(formatDuration(3_600_000)).toBe("1 h 0 min");
    expect(formatDuration(86_399_999)).toBe("23 h 59 min");
    expect(formatDuration(86_400_000)).toBe("1 d 0 h");
  });

  it("never prints a raw millisecond count", () => {
    expect(formatDuration(245_000)).not.toContain("245000");
    expect(formatDuration(245_000)).not.toContain("ms");
  });

  it("keeps the measurement under `precise`, where `< 1 s` would erase it", () => {
    // The nightly sequence times its steps to the tenth: 4 ms and 900 ms are
    // the same reading without this, and telling them apart is the point.
    expect(formatDuration(4, { precise: true })).toBe("4 ms");
    expect(formatDuration(900, { precise: true })).toBe("900 ms");
    expect(formatDuration(4500, { precise: true })).toBe("4.5 s");
    expect(formatDuration(59_940, { precise: true })).toBe("59.9 s");
  });

  it("rejoins the plain ladder above a minute, precise or not", () => {
    // Nobody times a two-minute step to the tenth.
    expect(formatDuration(90_000, { precise: true })).toBe("1 min 30 s");
    expect(formatDuration(8_040_000, { precise: true })).toBe("2 h 14 min");
  });

  it("renders a zero and an absence differently", () => {
    expect(formatDuration(0)).toBe("< 1 s");
    expect(formatDuration(0, { precise: true })).toBe("0 ms");
    expect(formatDuration(null)).toBe(NO_VALUE);
    expect(formatDuration(Number.NaN)).toBe(NO_VALUE);
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe(NO_VALUE);
  });
});

describe("formatCount", () => {
  it("is the §2 examples — `1 run` · `12 runs`", () => {
    expect(formatCount(1, "run")).toBe("1 run");
    expect(formatCount(12, "run")).toBe("12 runs");
  });

  it("takes the plural rather than appending an `s` to a word that has none", () => {
    expect(formatCount(3, "anomaly", "anomalies")).toBe("3 anomalies");
    expect(formatCount(1, "anomaly", "anomalies")).toBe("1 anomaly");
  });

  it("groups the count, and agrees the noun with a zero", () => {
    expect(formatCount(12_480, "query", "queries")).toBe("12,480 queries");
    expect(formatCount(0, "run")).toBe("0 runs");
  });

  it("answers the dash and NO noun for a count nobody measured", () => {
    // "0 runs" and "no measurement" are two different statements; this one is
    // the second, and the caller owns the sentence that says which.
    expect(formatCount(null, "run")).toBe(NO_VALUE);
    expect(formatCount(Number.NaN, "run")).toBe(NO_VALUE);
  });
});
