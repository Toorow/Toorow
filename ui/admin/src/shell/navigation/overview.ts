/**
 * The `overview` workspace: its sections, their lenses, their object contracts.
 *
 * AD-42, 2026-08-12 -- declared here rather than inside the 313-line array it
 * shared with five other workspaces. A change to overview no longer opens the file
 * the others are written in.
 *
 * ORDER IS PART OF THE CONTRACT and it stays in `navigation.ts`: the subnav
 * renders `WORKSPACES` as declared. This file owns the CONTENT of one entry,
 * never its position.
 */

import { section } from "./vocabulary";

export const overview = {
  key: "overview",
  slug: "overview",
  label: "Overview",
  question: "What changed and what needs my attention?",
  subnav: [section("project-overview", "Project Overview")],
} as const;
