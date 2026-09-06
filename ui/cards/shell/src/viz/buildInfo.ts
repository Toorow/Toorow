/**
 * Story 50.5 AC14 -- build identity, and the refusal that makes replay honest.
 *
 * SHAPE (downstream decision 6, amended -- see below):
 *   runtime_build  = "@toorow/card-shell/viz@<semver>+<content-hash>"
 *   renderer_build = "<family>/<renderer_id>@<semver>"
 *
 * The `/viz` segment matters. The runtime is a SUBTREE of a package whose card
 * primitives version independently, so a pin naming the package alone would not
 * identify the build that drew the chart.
 *
 * THE SUFFIX IS A CONTENT HASH, NOT A GIT SHA, AND THAT IS A CORRECTION.
 * Downstream decision 6 originally chose a git short SHA and explicitly rejected
 * a content hash because a SHA "carries its source commit". It cannot: the value
 * is generated BEFORE the commit that contains the code, so it always names the
 * previous commit -- the landed build pinned a parallel session's commit whose
 * tree held no `viz/` at all, while telling the reader to check that build out.
 * A hash of `src/viz/**` names the bytes that actually drew the chart and is
 * reproducible by `node scripts/generate-build-info.mjs`. The trade the original
 * decision named is real -- a hash does not tell you what to check out -- so the
 * refusal message below says how to find it instead of implying a commit.
 *
 * WHAT THIS BUYS. A retained Render pins both identities. When it is replayed,
 * the runtime compares them against itself and REFUSES -- visibly, with both
 * identities shown -- rather than drawing the frozen Render through a different
 * build. A chart redrawn by a newer renderer is a different claim wearing the old
 * Render's identity, which is precisely what a replay pin exists to prevent.
 */

import { RUNTIME_CONTENT_HASH, RUNTIME_SEMVER } from "./buildInfo.generated";

export const RUNTIME_PACKAGE = "@toorow/card-shell/viz";

/** The identity THIS bundle draws with. */
export const RUNTIME_BUILD = `${RUNTIME_PACKAGE}@${RUNTIME_SEMVER}+${RUNTIME_CONTENT_HASH}`;

/**
 * The theme contract version. Bumped when `vizTheme.ts`'s palette derivation
 * changes in a way that moves a colour, because a Render pins the colour it drew.
 */
export const THEME_VERSION = "viz-theme@1";

/**
 * The formatter contract version. Bumped when a number, date, percentage,
 * currency or duration would render differently for the same input.
 */
export const FORMATTER_VERSION = "viz-formatters@1";

/** Values a pin may never carry. Mirrors `analyze_artifacts._FORBIDDEN_PIN_VALUES`. */
const PLACEHOLDER_PINS = new Set([
  "legacy",
  "current",
  "deferred",
  "latest",
  "unknown",
  "none",
  "",
]);

export function isPlaceholderPin(value: string): boolean {
  // `unbuilt` is what the generator would emit if it could not read the runtime
  // sources. It is refused for the same reason the placeholder words are: a build
  // that cannot name itself must fail loudly rather than pin a value nobody can
  // resolve.
  return PLACEHOLDER_PINS.has(value.trim().toLowerCase()) || value.includes("unbuilt");
}

export function rendererBuild(family: string, rendererId: string, semver: string): string {
  return `${family}/${rendererId}@${semver}`;
}

export interface BuildMismatch {
  pin: "runtime_build" | "renderer_build" | "theme_version" | "formatter_version";
  pinned: string;
  running: string;
  message: string;
}

/**
 * AC14 -- compare a Render's pins against this build. Returns every mismatch, so
 * the refusal can show both identities rather than say "version error".
 */
export function checkBuildIdentity(
  pinned: {
    runtime_build: string;
    renderer_build: string;
    theme_version: string;
    formatter_version: string;
  },
  running: {
    runtime_build: string;
    renderer_build: string;
    theme_version: string;
    formatter_version: string;
  },
): BuildMismatch[] {
  const mismatches: BuildMismatch[] = [];
  if (pinned.runtime_build !== running.runtime_build) {
    mismatches.push({
      pin: "runtime_build",
      pinned: pinned.runtime_build,
      running: running.runtime_build,
      message:
        `This Render was drawn by runtime build "${pinned.runtime_build}". ` +
        `This page is running "${running.runtime_build}". The frozen Render is not ` +
        `redrawn through a different build. The suffix after "+" is a content hash of ` +
        `the runtime sources, so deploy the build whose hash matches to replay it.`,
    });
  }
  if (pinned.renderer_build !== running.renderer_build) {
    mismatches.push({
      pin: "renderer_build",
      pinned: pinned.renderer_build,
      running: running.renderer_build,
      message:
        `This Render was drawn by renderer build "${pinned.renderer_build}". ` +
        `This page is running "${running.renderer_build}". The frozen Render is not ` +
        `redrawn through a different renderer; deploy the renderer build it names to ` +
        `replay it.`,
    });
  }
  for (const pin of ["theme_version", "formatter_version"] as const) {
    if (pinned[pin] === running[pin]) continue;
    const label = pin === "theme_version" ? "theme" : "formatter";
    mismatches.push({
      pin,
      pinned: pinned[pin],
      running: running[pin],
      message:
        `This Render was drawn by ${label} version "${pinned[pin]}". ` +
        `This page is running "${running[pin]}". The frozen Render is not redrawn ` +
        `through a different ${label}; deploy the version it names to replay it.`,
    });
  }
  return mismatches;
}
