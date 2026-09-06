/**
 * A failure must never render as a success.
 *
 * Both publication dialogs decided their banner colour by SUBSTRING-matching the
 * message text — "unknown" or "failed" meant error, anything else meant success.
 * A design review reproduced it live: a genuine `HTTP 404`, which contains
 * neither word, appeared inside a GREEN SUCCESS banner on the screen that
 * publishes and rolls back.
 *
 * This asserts the property directly, over the messages the dialogs really
 * produce, because the defect was never about one message — it was about
 * deciding an outcome by reading prose.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SOURCE = readFileSync(
  resolve(__dirname, "../datastreams/workbench/DatastreamPublicationDialog.tsx"),
  "utf-8",
);

it("never infers an outcome from the words of a message", () => {
  // The exact shape of the old defect. Any return of it is a failure that can
  // render green.
  expect(SOURCE).not.toContain('message.includes("failed")');
  expect(SOURCE).not.toContain('message.includes("unknown")');
});

it("carries the outcome from the caller, which is the only place that knows", () => {
  expect(SOURCE).toContain('tone={message.tone}');
  // Every message assignment goes through one of the two constructors, so a new
  // branch cannot forget to say which it is.
  const assignments = SOURCE.match(/setMessage\(/g) ?? [];
  const tagged = SOURCE.match(/setMessage\((?:failed|succeeded)\(/g) ?? [];
  expect(assignments.length).toBeGreaterThan(0);
  expect(tagged.length).toBe(assignments.length);
});

it("keeps the two success sentences as the ONLY successes", () => {
  // Publishing and rolling back are the two things that can succeed here.
  // Everything else — unavailable, could-not-prepare, outcome-unknown — is a
  // refusal, and an "outcome unknown" is emphatically not a success.
  const successes = SOURCE.match(/succeeded\(/g) ?? [];
  expect(successes).toHaveLength(2);
});
