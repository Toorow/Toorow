/**
 * The `test` workspace: its sections, their lenses, their object contracts.
 *
 * AD-42, 2026-08-12 -- declared here rather than inside the 313-line array it
 * shared with five other workspaces. A change to test no longer opens the file
 * the others are written in.
 *
 * ORDER IS PART OF THE CONTRACT and it stays in `navigation.ts`: the subnav
 * renders `WORKSPACES` as declared. This file owns the CONTENT of one entry,
 * never its position.
 */

import { section } from "./vocabulary";

export const test = {
  key: "test",
  slug: "test",
  label: "Test",
  question: "Can I trust the generated answer and widget?",
  subnav: [
    // The five tabs `analyze-and-test.md:290` contracts, in that order
    // (Story 51.1 AC8). `defaultTab` is declared rather than inferred from
    // `tabs[0]`: a bare object address canonicalizes to Definition because
    // that is the decision, not because it happens to be first.
    section("golden-questions", "Golden Questions", [
      {
        type: "golden-question",
        label: "Golden Question",
        tabs: ["definition", "expected-result", "expected-ai-path", "coverage", "versions"],
        // THE PINNED DEFINITION IS AN ADDRESS, and it was not one.
        //
        // `GoldenQuestionWorkbench` opens an exact immutable version ON the
        // Definition tab -- "Pinned immutable Golden Question version", asserted
        // in `ContentRouter.test.tsx` -- and the server composes exactly that
        // owner link on a Resolution receipt (`create_receipt.owner_links[0]`:
        // tab `definition`, a `version_id`). Without this override the default
        // rule reads `versions` alone from `tabs`, so `buildPath` refused to
        // assemble the address the product already serves, and the receipt's
        // own link could not be built. Declared, because the workbench delivers
        // it -- never the other way round.
        versionTabs: ["definition", "versions"],
        defaultTab: "definition",
      },
    ]),
    // Two object types, two workbenches, one section. The Evaluation Run
    // carries the five tabs of `analyze-and-test.md:291` and the Trace
    // Observation the five lenses of `:293`; they are different objects and
    // neither borrows the other's tabs. `defaultTab` is declared rather than
    // inferred from `tabs[0]` — a bare address canonicalizes to Overview and
    // to Timeline because those are the decisions, not because they are first.
    section("regression-runs", "Regression Runs", [
      {
        type: "evaluation-run",
        label: "Evaluation Run",
        tabs: ["overview", "cases", "comparisons", "environment", "gate-decision"],
        // Resolution receipts focus their immutable Run, Case or Verdict
        // through this one real workbench; the tail is an evidence identity,
        // not a second object type the product does not own.
        versionTabs: ["cases"],
        defaultTab: "overview",
      },
      {
        type: "trace-observation",
        label: "Trace Observation",
        tabs: ["timeline", "context-skills", "tools", "result-render", "linked-feedback"],
        defaultTab: "timeline",
      },
    ]),
    // The five tabs of `analyze-and-test.md:294` (Story 51.5 AC12).
    section("widget-feedback", "Widget Feedback", [
      {
        type: "feedback-review",
        label: "Feedback Review",
        tabs: ["feedback", "result-render", "ai-path", "classification", "resolution"],
        defaultTab: "feedback",
      },
    ]),
  ],
} as const;
