/**
 * The `analyze` workspace: its sections, their lenses, their object contracts.
 *
 * AD-42, 2026-08-12 -- declared here rather than inside the 313-line array it
 * shared with five other workspaces. A change to analyze no longer opens the file
 * the others are written in.
 *
 * ORDER IS PART OF THE CONTRACT and it stays in `navigation.ts`: the subnav
 * renders `WORKSPACES` as declared. This file owns the CONTENT of one entry,
 * never its position.
 */

import { EXPLORE_QUERY, section } from "./vocabulary";

export const analyze = {
  key: "analyze",
  slug: "analyze",
  label: "Analyze",
  question: "What do the governed data say?",
  subnav: [
    section("explore", "Explore", [
      {
        type: "result",
        label: "Result",
        tabs: ["view", "data", "definitions", "quality", "provenance", "ai-path"],
        defaultTab: "view",
        // Every lens addresses one datum: /tab/{lens}/evidence/{datum_key}.
        evidenceTabs: ["view", "data", "definitions", "quality", "provenance", "ai-path"],
      },
      {
        type: "query-spec",
        label: "Query Spec",
        tabs: ["query"],
        defaultTab: "query",
        // `query` is the version-bearing tab: the Query Spec's only tab IS the
        // one that pins immutable analytical intent.
        versionTabs: ["query"],
      },
      {
        type: "visualization",
        label: "Visualization",
        tabs: ["build", "versions"],
        defaultTab: "build",
        versionTabs: ["build", "versions"],
      },
    ], [], [], EXPLORE_QUERY),
    // Story 52.1: the Answerable Topic catalog is a LENS of Reports, not a
    // fifth Level 2 screen. `analyze-and-test.md:33` says "Analyze has exactly
    // four stable Level 2 screens", and `:41-46` puts reusable question
    // objects here -- Reports is the destination that "reuses curated or saved
    // analytical questions". A fifth section was written first and removed:
    // the target fixes four, and adding one silently is the drift this repo
    // has a rule against. The FIRST lens is the declared default, so a bare
    // /analyze/reports address still opens Reports.
    section("reports", "Reports", [
      {
        type: "report",
        label: "Report",
        tabs: ["overview", "query", "presentation", "runs", "versions"],
        defaultTab: "overview",
      },
      // Story 72.5: the Chart Template, the LAST ratified object that reached no
      // surface at all (`python scripts/object_coverage_audit.py` answered
      // `ratified objects reaching no surface : 1` until this line existed).
      //
      // The four tabs are `visualization-and-rendering.md`'s own table
      // (amendment 2026-08-31, "The Chart Template workbench"), in its order.
      // The noun is `chart-template` and not `visualization-template`: decision
      // D1 of epic 72 collapsed three spellings into one, and the ratified
      // surface document spells the object type `chart-template` where it places
      // it. A second spelling in the address grammar would reopen exactly what
      // D1 closed.
      //
      // Unlike `visualization`, it is BROWSABLE — hence the `templates` lens
      // below — because a starting point nobody can browse is not a starting
      // point.
      {
        type: "chart-template",
        label: "Chart Template",
        tabs: ["overview", "presentation", "compatibility", "versions"],
        defaultTab: "overview",
        // NO `versionTabs`, and that is a measured decision rather than an
        // omission. `versionTabs` makes `/version/{id}` a resolvable address, and
        // `objectSurfaces.tsx:202` then refuses it out loud for every type whose
        // workbench does not OPEN one. The Versions tab here LISTS the immutable
        // versions; no row opens one yet. Declaring the address and refusing it
        // on arrival would be an address that exists only to say no.
      },
    ], [], [
      { slug: "reports", label: "Reports" },
      { slug: "topics", label: "Topics" },
      // Story 67.26: plan-versus-actual is a LENS of Reports, for the reason
      // Topics is one. `analyze-and-test.md:33` fixes Analyze at exactly four
      // Level 2 screens, and `:134` places pacing in Analyze ("Pacing ... is an
      // Analyze reading. It is not an ingestion surface and not a Governance
      // surface"). A fifth section would have been the drift this repo has a
      // rule against; a lens is the shape the target already ratified for
      // "another reading of the same section".
      //
      // It is NOT a `report` object. An `app.analysis_reports` row pins one
      // Query Spec version, and pacing has no Query Spec -- it is a mart read
      // whose authority is the `mediaplan_pacing` card. Minting a Query Spec to
      // make it fit would be a second definition of a computation that already
      // has one, which `analyze-and-test.md` forbids in the same breath
      // ("Analyze creates another source of metric definitions").
      { slug: "pacing", label: "Pacing" },
      // Story 72.5: the Chart Template catalogue is a LENS of Reports, for the
      // reason Topics and Pacing are. `analyze-and-test.md:44` already places it
      // there — "Chart Templates and presentation settings are lenses inside
      // Reports" — and `analyze-and-test.md:33` fixes Analyze at exactly four
      // Level 2 screens, so a fifth section would have been the drift this repo
      // has a rule against.
      { slug: "templates", label: "Chart Templates" },
    ], {
      // THE RESULT THE LIST IS JUDGED AGAINST IS PART OF THE ADDRESS, or it is a
      // mood. `vocabulary.ts` states the rule this obeys — "a parameter nobody
      // declared is dropped rather than carried into a shareable address that
      // lies" — and the Chart Templates lens is exactly the case it was written
      // for: the same list against two Results is two different screens, and a
      // colleague opening the link must get the one that was shared.
      //
      // `latest` and `current` are refused for the reason they are refused on
      // every other pin here: a shared link that follows a moving target
      // silently changes what it shows, and a compatibility verdict against
      // "whatever ran last" is a verdict about nothing.
      parameters: [{ name: "result_id", forbiddenValues: ["latest", "current"] }],
    }),
    section("notebooks", "Notebooks", [
      {
        type: "notebook",
        label: "Notebook",
        tabs: ["content", "runs", "versions"],
        defaultTab: "content",
      },
    ]),
    section("renders", "Renders", [
      {
        type: "render",
        label: "Render",
        tabs: ["result", "evidence", "sharing"],
        defaultTab: "result",
      },
      // Epic 73/74: a Dossier is a composition OF Renders -- several frozen
      // figures and a narrative, one document with versions -- so it is an
      // object of this section, not a fifth Level 2 screen (story 73-2 placed
      // the composition door under Renders; 74-2 delivers the reading page).
      {
        type: "dossier",
        label: "Dossier",
        tabs: ["document", "versions"],
        defaultTab: "document",
      },
    ]),
  ],
} as const;
