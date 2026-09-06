/**
 * A type check is not a presence check.
 *
 * `record({})` returns `{}`, which is truthy. Every site that wrote
 * `record(x) ? <EvidenceRows source={x}/> : <Status>not persisted</Status>`
 * therefore took the FIRST branch for an empty object — and `EvidenceRows`
 * returns `null` when it has no rows. The honest sentence never rendered;
 * silent blank space took its place. Two independent review lenses found it on
 * the same file:line on 2026-08-03.
 *
 * On the rollback dialog it was worse than cosmetic: the confirm button was
 * gated on the same truthy check, so an IRREVERSIBLE action stayed enabled with
 * no consequence on screen — directly against the comment above it, "a rollback
 * is irreversible; it is read, not dumped".
 */
import { record, filledRecord } from "../datastreams/workbench/evidence";

it("keeps `record` answering the TYPE question, because {} is a real value", () => {
  // A capability's `impact: {}` means "no impact", which is information. Making
  // `record` reject it would lose that.
  expect(record({})).toEqual({});
  expect(record(null)).toBeNull();
  expect(record([])).toBeNull();
  expect(record("x")).toBeNull();
});

it("answers the PRESENCE question separately, which is what the screens ask", () => {
  expect(filledRecord({})).toBeNull();
  expect(filledRecord(null)).toBeNull();
  expect(filledRecord({ null_rate: "0.00" })).toEqual({ null_rate: "0.00" });
});

it("is the difference between a blank panel and a sentence", () => {
  const emptyEvidence = {};
  // The old test: truthy, so the screen rendered EvidenceRows on nothing.
  expect(Boolean(record(emptyEvidence))).toBe(true);
  // The new one: falsy, so the honest fallback renders.
  expect(Boolean(filledRecord(emptyEvidence))).toBe(false);
});
