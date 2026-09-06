/**
 * The READ-TIME division, once, for every surface that prints a Result number.
 *
 * The platform's one internal money representation is canonical micros with the
 * currency attached (`server/core/money.py`, E39-AD1). The `/1e6` back to
 * display units happens EXACTLY ONCE, at read — and a Result table, a pivot
 * matrix and the MCP App are all reads. Before this module they were not: they
 * printed the stored integer, so `124 EUR` reached the screen as `124000000`,
 * the same amount stated wrong by six orders of magnitude.
 *
 * WHY NOT IN `formatters.ts`. That module states, in its own header, that it
 * performs no unit conversion — number and date STYLES only. Micros are not a
 * style: what a column means is governed data, frozen by the plan and carried by
 * the Result schema. Putting it there would contradict the rule the file is
 * written around, so the read-time division lives beside it under its own name.
 *
 * IT NEVER GUESSES. No currency is inferred from a column name, and a value
 * whose governed type the Result did not carry is printed as the plain number it
 * is. An older Result, frozen before the plan froze units, therefore keeps
 * reading exactly as it did rather than acquiring a currency after the fact.
 *
 * THE ARITHMETIC IS EXACT. Micros are integers and the shift is done on the
 * digits, not by dividing a float: `0.000001` of a currency unit survives, and
 * no amount picks up a rounding error on its way to a screen.
 */

/** What the governed vocabulary says one Result column means. */
export interface GovernedValueMeaning {
  /** `money`, `ratio`, `integer`, `decimal`, … — never inferred from the name. */
  value_type?: string | null;
  /** ISO currency for money; the declared unit otherwise. */
  unit?: string | null;
}

/** Same locale as `formatters.ts`: one console, one grouping. */
const LOCALE = "en-US";
const NBSP = " ";
const MICRO_DIGITS = 6;
/** Two digits is the smallest amount of money anybody writes. */
const MIN_FRACTION_DIGITS = 2;

/** `124000000` -> `"124.00"`, `1` -> `"0.000001"`. Digits only, no float. */
export function microsToUnits(micros: number | bigint): string {
  const negative = micros < 0;
  const digits = (negative ? -micros : micros).toString();
  const padded = digits.padStart(MICRO_DIGITS + 1, "0");
  const whole = padded.slice(0, padded.length - MICRO_DIGITS);
  let fraction = padded.slice(padded.length - MICRO_DIGITS);
  while (fraction.length > MIN_FRACTION_DIGITS && fraction.endsWith("0")) {
    fraction = fraction.slice(0, -1);
  }
  const grouped = Number(whole).toLocaleString(LOCALE, { maximumFractionDigits: 0 });
  return `${negative ? "-" : ""}${grouped}.${fraction}`;
}

/**
 * One Result value, printed as what it means.
 *
 * `null` is an absence and gets no zero: the caller decides the words for it,
 * so this returns `null` and never `"0"` or `"--"` of its own accord.
 */
export function formatGovernedValue(
  value: unknown,
  meaning: GovernedValueMeaning | null | undefined,
): string | null {
  if (value === null || value === undefined) return null;
  const type = String(meaning?.value_type ?? "");
  const unit = meaning?.unit ? String(meaning.unit) : "";

  if (type === "money" && typeof value === "number" && Number.isInteger(value)) {
    const amount = microsToUnits(value);
    return unit ? `${amount}${NBSP}${unit}` : amount;
  }
  if (typeof value === "number") {
    const printed = value.toLocaleString(LOCALE, { maximumFractionDigits: 4 });
    // A percentage IS its unit; anything else declared keeps its unit visible,
    // because a number whose unit is dropped is a number nobody can act on.
    return unit && type !== "money" ? `${printed}${NBSP}${unit}` : printed;
  }
  return String(value);
}
