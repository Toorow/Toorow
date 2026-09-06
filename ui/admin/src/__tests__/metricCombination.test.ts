/** The refusal vocabulary the console owes, pinned literal by literal.
 *
 * `analyze-and-test.md:619` recorded the gap as measured: *"no `.tsx` reads
 * `metrics_not_combinable` or `combination_check`. The server states both; the
 * console still draws a refused metric as a gap."* This suite is the other half of
 * closing it — every literal the server emits has a sentence here, and a literal
 * the server does not emit gets `null` rather than an invented reassurance.
 *
 * WHY THE STRINGS ARE REPEATED IN FULL rather than imported into the assertion:
 * the point of the test is that the console's copy MATCHES the server's emitter. An
 * assertion written as `expect(describe(X)).toBe(TABLE[X])` passes for any pair of
 * matching mistakes.
 */

import { describe, expect, it } from "vitest";

import {
  describeAdditivity,
  describeCombinationCheck,
  describeCombinationRefusal,
  describeVisualState,
  isCombinable,
  notCombinableMembers,
} from "../analyze/metricCombination";

describe("the four statuses the rollup authority refuses on (rollup.py:74-81)", () => {
  it("names every one of them, and each one differently", () => {
    const statuses = [
      "UNRULED_OVERLAP",
      "KEEP_SEPARATE",
      "NOT_COMBINABLE",
      "OVERRIDE_NOT_MATERIALIZED",
    ];
    const titles = statuses.map((s) => describeCombinationRefusal(s)?.title);
    expect(titles.every(Boolean)).toBe(true);
    // Four causes, four sentences: one wording for three of them sends the
    // reader to change the wrong thing.
    expect(new Set(titles).size).toBe(4);
  });

  it("an unruled overlap names the rule that repairs it", () => {
    const explained = describeCombinationRefusal("UNRULED_OVERLAP");
    expect(explained?.sentence).toMatch(/double-count/);
    expect(explained?.gesture).toMatch(/Declare a reconciliation rule/);
  });

  it("a metric declared to stay per source offers NO gesture, because none repairs it", () => {
    const explained = describeCombinationRefusal("KEEP_SEPARATE");
    expect(explained?.sentence).toMatch(/by design/);
    expect(explained?.gesture).toBeNull();
  });

  it("a stale override sends the reader to re-run, not to the rule", () => {
    expect(describeCombinationRefusal("OVERRIDE_NOT_MATERIALIZED")?.gesture).toMatch(
      /Re-run the sources/,
    );
  });

  it("a status this console does not know is not explained away", () => {
    expect(describeCombinationRefusal("SOMETHING_NEW")).toBeNull();
    expect(describeCombinationRefusal(null)).toBeNull();
    expect(describeCombinationRefusal(undefined)).toBeNull();
  });
});

describe("how the question was answered (combination_check, rollup.py:518-522)", () => {
  it("covers the five states", () => {
    for (const check of [
      "single_source",
      "not_requested",
      "verified",
      "unavailable",
      "refused",
    ]) {
      expect(describeCombinationCheck(check)).not.toBeNull();
    }
  });

  it("distinguishes a number nobody checked from a number that was checked", () => {
    // The whole reason `combination_check` exists: these two used to be the
    // same output.
    expect(describeCombinationCheck("not_requested")?.sentence).toMatch(
      /nobody verified it/,
    );
    expect(describeCombinationCheck("verified")?.sentence).toMatch(/permits/);
    expect(describeCombinationCheck("not_requested")?.title).not.toBe(
      describeCombinationCheck("verified")?.title,
    );
  });

  it("a gate that could not answer is not read as a pass", () => {
    expect(describeCombinationCheck("unavailable")?.sentence).toMatch(
      /not a verified number/,
    );
  });

  it("does not invent a state", () => {
    expect(describeCombinationCheck("probably_fine")).toBeNull();
  });
});

describe("visual.state (answer_contract.py:39-45)", () => {
  it("names the three states and only those", () => {
    expect(describeVisualState("rendered")).not.toBeNull();
    expect(describeVisualState("not_applicable")).not.toBeNull();
    expect(describeVisualState("unavailable")).not.toBeNull();
    expect(describeVisualState("pending")).toBeNull();
  });

  it("separates 'no chart is owed here' from 'the chart could not be built'", () => {
    expect(describeVisualState("not_applicable")?.sentence).toMatch(/Nothing is missing/);
    expect(describeVisualState("not_applicable")?.gesture).toBeNull();
    expect(describeVisualState("unavailable")?.sentence).toMatch(/has NOT been drawn/);
    expect(describeVisualState("unavailable")?.gesture).toMatch(/Check the sources/);
  });
});

describe("additivity, and what a merge may fold", () => {
  it("agrees with the compiler: only 'additive' may be folded to a merge key", () => {
    // `multi_source_plan._metric_additivity` puts `semi_additive` on the
    // non-summable side. If the console disagreed it would promise a cross the
    // compiler refuses, which is the badge-that-lies defect all over again.
    expect(isCombinable("additive")).toBe(true);
    expect(isCombinable("semi_additive")).toBe(false);
    expect(isCombinable("non_additive")).toBe(false);
    expect(isCombinable(null)).toBe(false);
  });

  it("speaks the reader's words, never the schema's", () => {
    for (const klass of ["additive", "semi_additive", "non_additive"]) {
      const explained = describeAdditivity(klass);
      expect(explained).not.toBeNull();
      expect(explained?.title).not.toContain("_");
      expect(explained?.title.toLowerCase()).not.toContain("additiv");
    }
  });

  it("a non-additive measure carries the gesture that repairs it", () => {
    expect(describeAdditivity("non_additive")?.gesture).toMatch(
      /Ask for the measures it is computed from/,
    );
  });
});

describe("naming the measures a Result does not add up", () => {
  const member = (concept_id: string, additivity_class: string | null) => ({
    concept_id,
    label: concept_id,
    additivity_class,
  });

  it("names the non-additive and the semi-additive, and stays silent on the rest", () => {
    const named = notCombinableMembers([
      member("clicks", "additive"),
      member("roas", "non_additive"),
      member("stock", "semi_additive"),
    ]);
    expect(named.map((entry) => entry.member.concept_id)).toEqual(["roas", "stock"]);
  });

  it("an undeclared additivity is NOT an accusation", () => {
    // The registry refuses to store a metric declaring neither an aggregation
    // nor its non-additivity, so a blank here is a vocabulary this Result could
    // not read — not a verdict against the measure.
    expect(notCombinableMembers([member("mystery", null)])).toEqual([]);
  });

  it("an all-additive Result names nothing, so the one case that matters stays visible", () => {
    expect(notCombinableMembers([member("clicks", "additive")])).toEqual([]);
  });

  it("carries the explanation beside the member, so a caller cannot lose the pairing", () => {
    const [entry] = notCombinableMembers([member("roas", "non_additive")]);
    expect(entry.explained.title).toBe("Cannot be added up");
    expect(entry.member.concept_id).toBe("roas");
  });
});
