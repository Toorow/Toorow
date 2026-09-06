/**
 * The database's word is not the person's word.
 *
 * MEASURED ON THE GOVERNANCE WORKBENCH, 2026-08-16. Its `Definition` tab was
 * `Object.entries(summary)` run through `label()`, and `label()` only de-snakes:
 * `content_hash` became `Content hash`, `additivity_class` became `Additivity
 * class`. That is a TYPOGRAPHIC repair. A person opening a governed object was
 * reading a column list in the storage layer's voice, with 64-character hashes
 * printed whole.
 *
 * What these tests hold is the rule and not the table: a word that reaches a
 * screen carries the product's noun and ONE sentence saying what it decides; a
 * word nobody curated falls back to the de-snaked form, which is the honest
 * default and not a hole; and an opaque value is shortened to be COMPARABLE,
 * never to be hidden.
 */
import { describe, expect, it } from "vitest";
import { displayValue, fieldMeaning, label, shortenOpaque } from "../ui/Evidence";

describe("a curated field speaks the product's language", () => {
  it("renames the storage word and says what the value decides", () => {
    expect(label("content_hash")).toBe("Exact content");
    expect(fieldMeaning("content_hash")).toMatch(/pins a reading/);

    expect(label("additivity_class")).toBe("Can it be summed");
    expect(fieldMeaning("additivity_class")).toMatch(/true total/);

    expect(label("object_kind")).toBe("Qualifies");
    // The absence is named as an absence, not left to be guessed.
    expect(fieldMeaning("object_kind")).toMatch(/Empty means nobody has said/);
  });

  it("keeps the de-snaked word when nobody curated one, and says NOTHING rather than inventing", () => {
    expect(label("some_uncurated_column")).toBe("Some uncurated column");
    // `null`, not an empty string: a caller must be able to render no hint at
    // all rather than an empty line pretending to be one.
    expect(fieldMeaning("some_uncurated_column")).toBeNull();
  });

  it("curates a meaning without renaming, when the column word is already the product's", () => {
    expect(label("aggregation")).toBe("Aggregation");
    expect(fieldMeaning("aggregation")).toMatch(/one figure/);
  });
});

describe("an opaque value is shortened to be compared, never to be hidden", () => {
  it("shortens a content hash", () => {
    const hash = "a".repeat(64);
    expect(shortenOpaque(hash)).toBe(`${"a".repeat(12)}…`);
    expect(displayValue(hash)).toBe(`${"a".repeat(12)}…`);
  });

  it("shortens a prefixed identity but keeps its prefix, which is what names the object", () => {
    expect(shortenOpaque("mckv_01J00000000000000000000000")).toBe("mckv_01J000…");
    expect(shortenOpaque("scv_01J00000000000000000000000")).toBe("scv_01J000…");
  });

  it("leaves anything a person actually reads alone", () => {
    expect(shortenOpaque("Campaign spend")).toBe("Campaign spend");
    expect(shortenOpaque("many_to_one")).toBe("many_to_one");
    // A short hex string is a value, not a fingerprint.
    expect(shortenOpaque("abc123")).toBe("abc123");
  });

  it("never shortens what is not a string, and keeps the honest absences", () => {
    expect(displayValue(null)).toBe("Unavailable");
    expect(displayValue([])).toBe("None");
    expect(displayValue(false)).toBe("No");
    expect(displayValue(42)).toBe("42");
  });
});
