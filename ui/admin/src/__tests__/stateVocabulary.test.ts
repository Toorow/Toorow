/**
 * ONE VOCABULARY, ONE ANSWER PER WORD, AND NO PRIVATE MAP LEFT TO PASS.
 *
 * Six screens held their own state map before `ui/stateVocabulary` existed —
 * `data/DataCollectionLayout` (a tone map and a label map),
 * `governance/GovernanceCollection`, `analyze-artifacts/Shared`,
 * `analyze-artifacts/RenderSharing` and `cache/CacheHealthCard`. Two of the six
 * genuinely disagreed, about `blocked` and about `unavailable`, and until story
 * 76-2 both survived as declared overrides that `stateTone(value, overrides)`
 * existed to carry.
 *
 * `console-presentation.md` §3 closed both, and the previous version of this file
 * — which asserted that the two overrides still disagreed — is what it replaces:
 *
 *   `blocked`      warning everywhere; Governance's `error` is withdrawn.
 *   `unavailable`  neutral everywhere; Analyze's `info` is withdrawn, and the
 *                  fact it wanted to colour gets `not_offered`.
 *
 * WHAT IS ASSERTED NOW, and why each part matters:
 *
 *   1. THE ARITY. `stateTone` and `stateLabel` take one argument. A test on
 *      `.length` is not pedantry: an `overrides` parameter that grows back is how
 *      a private vocabulary returns one entry at a time, under a name that reads
 *      as an exception, and a type-level check would not survive a `any` at a
 *      call site.
 *   2. THE MATRIX. Every declared word, its tone and its sentence, in one table
 *      a reader can compare against the screen.
 *   3. THE DEFAULT IS A WARNING. `README.md` invariant 8 — unknown is never
 *      healthy, and never the grey of "nothing to see" either.
 *   4. NO SCREEN STILL EXPORTS AN OVERRIDE MAP. The two names the old file
 *      imported (`OUTCOME_TONE`, `GOVERNANCE_STATE_TONE`) must be gone from their
 *      modules, not merely unused — an exported map with no reader is the next
 *      caller's invitation.
 */
import { describe, expect, it } from "vitest";

import * as Shared from "../analyze-artifacts/Shared";
import * as GovernanceCollection from "../governance/GovernanceCollection";
import {
  STATE_LABEL,
  STATE_TONE,
  UNKNOWN_STATE_LABEL,
  UNKNOWN_STATE_TONE,
  stateLabel,
  stateTone,
} from "../ui/stateVocabulary";
import type { Tone } from "../ui/tone";

describe("the vocabulary answers with one argument, and there is nothing to override", () => {
  it("neither function takes a second parameter", () => {
    expect(stateTone).toHaveLength(1);
    expect(stateLabel).toHaveLength(1);
  });

  it("no module still exports a state override map", () => {
    // Named rather than grepped, because these two are THE two: the file that
    // used to import them is this one.
    expect(Object.keys(Shared)).not.toContain("OUTCOME_TONE");
    expect(Object.keys(GovernanceCollection)).not.toContain("GOVERNANCE_STATE_TONE");
  });
});

describe("the two collisions are closed, and the third word exists", () => {
  it("blocked is a warning everywhere — Governance's error is withdrawn", () => {
    // Repairable, which is what warning means in this scale. The word that means
    // "cannot be used at all" is `archived`.
    expect(stateTone("blocked")).toBe("warning");
    expect(stateTone("archived")).toBe("error");
  });

  it("unavailable is neutral everywhere — Analyze's info is withdrawn", () => {
    expect(stateTone("unavailable")).toBe("neutral");
  });

  it("not_offered is the statement unavailable was being asked to carry", () => {
    // `unavailable` on a run outcome is written by query_execution.py for a
    // warehouse it could not reach: a silence. `not_offered` is read from
    // `ContractState.available`: the deployment does not carry the contract.
    expect(stateTone("not_offered")).toBe("info");
    expect(stateLabel("not_offered")).toBe("Not offered here");
    expect(stateTone("not_offered")).not.toBe(stateTone("unavailable"));
  });
});

describe("the matrix: every declared word, its tone and its sentence", () => {
  /**
   * The words whose reading a screen depends on, pinned one by one. Not every
   * key of `STATE_TONE` — the closure test below covers those — but every word
   * whose tone was ever argued about, plus one of each tone so the scale itself
   * cannot rotate unnoticed.
   */
  const MATRIX: ReadonlyArray<readonly [string, Tone, string]> = [
    ["active", "success", "Active"],
    ["available", "success", "Available"],
    ["fresh", "success", "Fresh"],
    ["succeeded", "success", "Succeeded"],
    ["matched", "success", "Matched"],
    ["accepted", "success", "Accepted"],
    ["blocked", "warning", "Blocked"],
    ["stale", "warning", "Stale"],
    ["draft", "warning", "Draft"],
    ["pending_confirmation", "warning", "Awaiting confirmation"],
    ["outcome_unknown", "warning", "Outcome unknown"],
    ["ambiguous", "warning", "Ambiguous"],
    ["archived", "error", "Archived"],
    ["failed", "error", "Failed"],
    ["revoked", "error", "Revoked"],
    ["unavailable", "neutral", "Unavailable"],
    ["expired", "neutral", "Expired"],
    ["no-cache", "neutral", "Absent"],
    ["unmatched", "neutral", "Not matched"],
    ["not_offered", "info", "Not offered here"],
  ];

  it.each(MATRIX)("%s reads %s / %s", (word, tone, label) => {
    expect(stateTone(word)).toBe(tone);
    expect(stateLabel(word)).toBe(label);
  });

  it("is case- and whitespace-insensitive, because the wire is not", () => {
    expect(stateTone(" ACTIVE ")).toBe("success");
    expect(stateLabel(" Stale")).toBe("Stale");
  });

  it("every word the tone map declares can be said out loud", () => {
    // Not "every word has a hand-written label": the fallback capitalizes most
    // of them correctly and repeating those would be a second list to maintain.
    // What must hold is that no DECLARED word reaches a screen as a stored token
    // and that none of them accidentally falls through to `Unknown`.
    for (const word of Object.keys(STATE_TONE)) {
      const label = stateLabel(word);
      expect(label[0], `"${word}" renders as "${label}"`).toBe(label[0].toUpperCase());
      expect(label, `"${word}" renders as "${label}"`).not.toMatch(/[_-]/);
      expect(label, `"${word}" is declared but reads as Unknown`).not.toBe(UNKNOWN_STATE_LABEL);
    }
    expect(Object.keys(STATE_LABEL).length).toBeGreaterThan(0);
  });
});

describe("a word the console has never heard of is a question, not a grey badge", () => {
  it("colours an undeclared word warning, never neutral", () => {
    expect(UNKNOWN_STATE_TONE).toBe("warning");
    expect(stateTone("something_new_from_the_server")).toBe("warning");
    expect(stateTone("half_written")).toBe("warning");
  });

  it("spells it Unknown rather than dressing the token as a product word", () => {
    // "Half written" looked like a word the console owns. It was a column value.
    expect(stateLabel("half_written")).toBe("Unknown");
    expect(stateLabel("brand-new")).toBe("Unknown");
  });

  it("reads an absent state as Unknown, and not as Unavailable", () => {
    // The server did not say "unavailable"; it said nothing. Those are two
    // different facts and `unavailable` is the one the server chooses.
    for (const absent of [undefined, null, "", "   "]) {
      expect(stateLabel(absent)).toBe("Unknown");
      expect(stateTone(absent)).toBe("warning");
    }
  });
});
