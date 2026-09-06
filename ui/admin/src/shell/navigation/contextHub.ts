/**
 * The `context-hub` workspace: its sections, their lenses, their object contracts.
 *
 * AD-42, 2026-08-12 -- declared here rather than inside the 313-line array it
 * shared with five other workspaces. A change to context-hub no longer opens the file
 * the others are written in.
 *
 * ORDER IS PART OF THE CONTRACT and it stays in `navigation.ts`: the subnav
 * renders `WORKSPACES` as declared. This file owns the CONTENT of one entry,
 * never its position.
 */

import { section } from "./vocabulary";

export const contextHub = {
  key: "context-hub",
  slug: "context-hub",
  label: "Context Hub",
  question: "What governed business knowledge should analysis use?",
  subnav: [
    section("knowledge-graph", "Knowledge Graph", [{ type: "ai-path", label: "AI Path" }]),
    section("knowledge-library", "Knowledge Library", [
      { type: "context-topic", label: "Knowledge", tabs: ["content", "usage", "versions"] },
    ]),
    // ONE object, TWO spellings. `skill` is declared here beside
    // `context-procedure` for the same thing — the canonical noun
    // (`glossary.md`, which reserves `Procedure` as a delivered-token-only
    // word) and the delivered token that the wire and the DB still carry
    // until the `context_procedure` → Skill rename of
    // `alignment-register.md` item 4 lands with its backfill.
    //
    // Nothing routed `skill` for a while, and its three tabs were then the
    // only three addresses in the registry whose click broke
    // (`python scripts/screens.py pages` → 3 UNANSWERED, all of them here).
    // The declaration is NOT the defect: an AI Path step records
    // `owner_object_type: "skill"` (migration 150), so deleting it would have
    // traded a dead screen for a dead link. `ContentRouter.tsx` now serves
    // `skill` from the same ContextObjectPage as `context-procedure`, which
    // is what `element-control-loop.md` §6 asks for — reach state 7, or
    // leave. This one reaches it.
    section("skills-registry", "Skills Registry", [
      { type: "context-procedure", label: "Skill", tabs: ["content", "usage", "versions"] },
      { type: "skill", label: "Skill", tabs: ["content", "usage", "versions"] },
    ]),
  ],
} as const;
