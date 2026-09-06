/**
 * `ui/Timestamp` — one instant, one rendering.
 *
 * WHAT IS ASSERTED are the three properties the six hand-rolled formatters this
 * component replaces did not share: the same format for the same instant, an
 * explicit zone so two screens can be compared, and an absent value that says
 * what is absent instead of printing `Invalid Date`.
 *
 * The expected text is DERIVED from `Intl` rather than written out. The suite
 * pins no timezone, so a literal `17/08/2026, 11:05:03 CEST` would be a test of
 * the machine the run happened on. What is pinned is the FORMAT — en-GB
 * ordering, a named zone — and that is asserted structurally.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  Timestamp,
  formatClock,
  formatDate,
  formatRelative,
  formatTimestamp,
  NO_TIMESTAMP,
} from "../ui/Timestamp";
import { NO_VALUE } from "../ui/format";

const INSTANT = "2026-08-17T09:05:03Z";

/** The same call the component makes, so the assertion tests the decision. */
const expected = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { timeZoneName: "short" });

describe("Timestamp", () => {
  it("renders a valid instant in the console's one format, with its zone named", () => {
    render(<Timestamp value={INSTANT} data-testid="stamp" />);
    const node = screen.getByTestId("stamp");
    expect(node).toHaveTextContent(expected(INSTANT));
    // en-GB ordering: day first, 24-hour clock, seconds kept — and a zone.
    expect(node.textContent).toMatch(/^17\/08\/2026, \d{2}:\d{2}:\d{2} \S+$/);
  });

  it("carries the exact instant in a machine-readable `datetime`", () => {
    // The visible text is local and therefore lossy across readers; nothing
    // downstream should have to re-derive the instant from it.
    render(<Timestamp value={INSTANT} data-testid="stamp" />);
    expect(screen.getByTestId("stamp").tagName).toBe("TIME");
    expect(screen.getByTestId("stamp")).toHaveAttribute("datetime", "2026-08-17T09:05:03.000Z");
  });

  it("renders every kind of absent value as a dash that says what it means", () => {
    for (const [index, value] of [null, undefined, ""].entries()) {
      const { unmount } = render(<Timestamp value={value} data-testid={`absent-${index}`} />);
      const node = screen.getByTestId(`absent-${index}`);
      expect(node.textContent).toContain(NO_TIMESTAMP);
      // The meaning is readable — by a mouse, via the title, and by a screen
      // reader, via the text the dash itself is hidden from.
      expect(node).toHaveAttribute("title", "No time recorded");
      expect(node).toHaveTextContent("No time recorded");
      unmount();
    }
  });

  it("lets the screen name what is absent, so a column of dashes says which question it answers", () => {
    render(<Timestamp value={null} absentMeaning="Never run" data-testid="stamp" />);
    expect(screen.getByTestId("stamp")).toHaveAttribute("title", "Never run");
    expect(screen.getByTestId("stamp")).toHaveTextContent("Never run");
  });

  it("shows an unreadable value verbatim and never `Invalid Date`", () => {
    render(<Timestamp value="soon" data-testid="stamp" />);
    expect(screen.getByTestId("stamp")).toHaveTextContent("soon");
    expect(screen.getByTestId("stamp").textContent).not.toContain("Invalid Date");
  });
});

describe("formatTimestamp", () => {
  it("is the same decision as the component, for the places that can only hold a string", () => {
    expect(formatTimestamp(INSTANT)).toBe(expected(INSTANT));
  });

  it("accepts the three shapes a screen actually holds", () => {
    const epoch = Date.parse(INSTANT);
    expect(formatTimestamp(new Date(INSTANT))).toBe(expected(INSTANT));
    expect(formatTimestamp(epoch)).toBe(expected(INSTANT));
    expect(formatTimestamp(INSTANT)).toBe(expected(INSTANT));
  });

  it("returns the dash for absent and the raw text for unreadable", () => {
    expect(formatTimestamp(null)).toBe(NO_TIMESTAMP);
    expect(formatTimestamp(undefined)).toBe(NO_TIMESTAMP);
    expect(formatTimestamp("")).toBe(NO_TIMESTAMP);
    expect(formatTimestamp("not a time")).toBe("not a time");
    expect(formatTimestamp("not a time")).not.toContain("Invalid Date");
  });
});

/**
 * `formatRelative` — `console-presentation.md` §2, "Relative time".
 *
 * `now` IS INJECTED IN EVERY CASE. A relative formatter tested against the
 * wall clock is a test that passes at 14:00 and fails at 23:59; the parameter
 * exists for the screens too, which hold a measured `now` rather than reading
 * one per row.
 *
 * The clock half of `yesterday 14:05` is DERIVED, not written out: the suite
 * pins no timezone, so a literal would test the machine the run happened on.
 * What is pinned is the WORD in front of it.
 */
describe("formatRelative", () => {
  const NOW = new Date("2026-08-17T12:00:00Z");
  const at = (ms: number) => new Date(NOW.getTime() + ms);
  const MINUTE = 60_000;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;

  it("says `just now` for the minute either side of now", () => {
    expect(formatRelative(NOW, NOW)).toBe("just now");
    expect(formatRelative(at(-30_000), NOW)).toBe("just now");
    expect(formatRelative(at(30_000), NOW)).toBe("just now");
  });

  it("counts minutes, then hours — §2: `4 min ago` · `2 h ago`", () => {
    expect(formatRelative(at(-4 * MINUTE), NOW)).toBe("4 min ago");
    expect(formatRelative(at(-2 * HOUR), NOW)).toBe("2 h ago");
  });

  it("says which way time runs, and never prints a negative count", () => {
    expect(formatRelative(at(4 * MINUTE), NOW)).toBe("in 4 min");
    expect(formatRelative(at(2 * HOUR), NOW)).toBe("in 2 h");
    expect(formatRelative(at(-4 * MINUTE), NOW)).not.toContain("-");
  });

  it("names yesterday and tomorrow, with the clock — §2: `yesterday 14:05` · `tomorrow 02:00`", () => {
    const yesterday = at(-26 * HOUR);
    const tomorrow = at(26 * HOUR);
    expect(formatRelative(yesterday, NOW)).toBe(`yesterday ${formatClock(yesterday)}`);
    expect(formatRelative(tomorrow, NOW)).toBe(`tomorrow ${formatClock(tomorrow)}`);
  });

  it("counts days between two days out and the seventh", () => {
    expect(formatRelative(at(-3 * DAY), NOW)).toBe("3 d ago");
    expect(formatRelative(at(6 * DAY), NOW)).toBe("in 6 d");
  });

  it("gives up the relative form beyond seven days and says the instant", () => {
    const far = at(-8 * DAY);
    expect(formatRelative(far, NOW)).toBe(formatTimestamp(far));
    const ahead = at(8 * DAY);
    expect(formatRelative(ahead, NOW)).toBe(formatTimestamp(ahead));
  });

  it("keeps the absent and unreadable answers of the absolute form", () => {
    expect(formatRelative(null, NOW)).toBe(NO_TIMESTAMP);
    expect(formatRelative("", NOW)).toBe(NO_TIMESTAMP);
    expect(formatRelative("soon", NOW)).toBe("soon");
    expect(formatRelative("soon", NOW)).not.toContain("Invalid Date");
  });

  it("reads the wall clock when no `now` is given", () => {
    expect(formatRelative(new Date())).toBe("just now");
  });
});

describe("<Timestamp relative>", () => {
  const NOW = new Date("2026-08-17T12:00:00Z");
  const RECENT = new Date(NOW.getTime() - 4 * 60_000);

  it("shows the relative wording and still carries the exact instant", () => {
    render(<Timestamp value={RECENT} relative now={NOW} data-testid="stamp" />);
    const node = screen.getByTestId("stamp");
    expect(node).toHaveTextContent("4 min ago");
    expect(node.tagName).toBe("TIME");
    expect(node).toHaveAttribute("datetime", RECENT.toISOString());
  });

  it("keeps the absolute reading reachable, on the title", () => {
    // A relative wording is a convenience, not a replacement: the instant a
    // person needs in order to compare two screens is one hover away.
    render(<Timestamp value={RECENT} relative now={NOW} data-testid="stamp" />);
    expect(screen.getByTestId("stamp")).toHaveAttribute("title", formatTimestamp(RECENT));
  });

  it("leaves the absent meaning to the caller, exactly as the absolute form does", () => {
    render(<Timestamp value={null} relative now={NOW} absentMeaning="Never run" data-testid="stamp" />);
    const node = screen.getByTestId("stamp");
    expect(node).toHaveAttribute("title", "Never run");
    expect(node).toHaveTextContent("Never run");
    expect(node.textContent).toContain(NO_TIMESTAMP);
  });
});

/**
 * The two grains below the instant. §2 pins ONE dash and ONE locale; these are
 * the same decision applied to a date with no time (« Last verified 14 Aug
 * 2026 ») and to a clock with no date (« measured at 11:05 »), the two shapes
 * the console had spread over eight hand-rolled `toLocaleDateString` /
 * `toLocaleTimeString` calls before 76-1.
 */
describe("formatDate and formatClock", () => {
  it("renders one date grain, day before month, month as a word", () => {
    expect(formatDate(INSTANT)).toMatch(/^17 Aug 2026$/);
  });

  it("drops the year only when the caller says the surrounding window implies it", () => {
    expect(formatDate(INSTANT, { year: false })).toBe("17 Aug");
  });

  it("renders a 24-hour clock, and the seconds only on request", () => {
    expect(formatClock(INSTANT)).toMatch(/^\d{2}:\d{2}$/);
    expect(formatClock(INSTANT, { seconds: true })).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("answers the same dash as every other absence in the console", () => {
    expect(formatDate(null)).toBe(NO_VALUE);
    expect(formatClock(undefined)).toBe(NO_VALUE);
    expect(NO_TIMESTAMP).toBe(NO_VALUE);
  });

  it("shows an unreadable value verbatim rather than `Invalid Date`", () => {
    expect(formatDate("soon")).toBe("soon");
    expect(formatClock("soon")).toBe("soon");
  });
});
