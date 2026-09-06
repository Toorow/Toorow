/**
 * THE GUARD `emit-renderer-builds.mjs:20` HAS CLAIMED SINCE 2026-08-13, written
 * 2026-08-24 because it did not exist.
 *
 * `ui/cards/shell/src/viz/rendererBuilds.generated.json` is the PROJECTION SOURCE
 * of `app.renderer_runtime_builds`: `scripts/register_renderer_builds.py` reads it
 * on a checkout with no node toolchain and inserts a ledger row per build. A
 * Render's `runtime_build` and `renderer_build` pins point at those rows, so a
 * stale manifest does not merely misinform -- it fills the ledger with builds no
 * deployment ships, and the Render drawn by the RUNNING build still finds no row.
 *
 * The emitter's docstring said "a stale manifest is a RED TEST
 * (`__tests__/rendererBuilds.test.ts`)" and that file was absent, while
 * `package.json` had no `emit:renderer-builds` script either. Measured
 * 2026-08-24, the committed manifest named `@toorow/card-shell/viz@0.1.0+c2969870d2d3`
 * on all eight builds while `buildInfo.generated.ts` shipped `daecc5430bb5` and
 * the sources hashed to `aff93cd8d3c6` -- three identities, two generations
 * apart, and nothing red.
 *
 * WHAT THIS FILE ASSERTS, and why each half is needed:
 *   1. the manifest carries the identity the RUNNING runtime carries -- the half
 *      that turns red when only `buildInfo.generated.ts` is regenerated;
 *   2. the manifest is exactly the projection of the registry this build
 *      installs -- the half that turns red when a family is added, removed or
 *      re-declared and the manifest is not re-emitted;
 *   3. the manifest is EXCLUDED from the identity hash -- the half that keeps the
 *      other two satisfiable at the same time. It was inside the hash, which gave
 *      the pair no fixed point: stamping identity H on the manifest changed the
 *      bytes the identity is computed over, so H was wrong the instant it was
 *      written. Without this assertion the repair silently regresses and the
 *      drift comes back.
 *
 * Repair, when any of them is red: `pnpm --filter @toorow/card-shell
 * generate:build-identity`, which runs both generators in the one order that
 * works, then commit BOTH generated files.
 */

import { describe, expect, it } from "vitest";

import { FORMATTER_VERSION, RUNTIME_BUILD, THEME_VERSION, isPlaceholderPin } from "../buildInfo";
import { registered } from "../registry";
import { installStandardRenderers } from "../renderers";
import manifest from "../rendererBuilds.generated.json";
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore -- a build script, deliberately untyped and shared with `prebuild`.
import { HASH_EXCLUDES, runtimeSourceFiles } from "../../../scripts/runtime-identity.mjs";

installStandardRenderers();

interface ManifestBuild {
  id: string;
  family: string;
  renderer_id: string;
  runtime_build: string;
  theme_version: string;
  formatter_version: string;
  responsive_profiles: string[];
}

const builds = manifest.builds as ManifestBuild[];

/** The projection the emitter performs, restated here as the EXPECTATION. */
function projectionOfTheRegistry(): ManifestBuild[] {
  return registered()
    .flatMap((declaration) =>
      declaration.families.map((family) => ({
        id: declaration.build,
        family,
        renderer_id: declaration.renderer_id,
        runtime_build: RUNTIME_BUILD,
        theme_version: THEME_VERSION,
        formatter_version: FORMATTER_VERSION,
        responsive_profiles: [...declaration.profiles],
      })),
    )
    .sort((left, right) => left.id.localeCompare(right.id));
}

describe("the committed renderer-build manifest", () => {
  it("names the identity this build runs (re-emit if this fails)", () => {
    expect(
      manifest.runtime_build,
      "rendererBuilds.generated.json is stale: run " +
        "`pnpm --filter @toorow/card-shell generate:build-identity` and commit both " +
        "generated files",
    ).toBe(RUNTIME_BUILD);
    for (const build of builds) {
      expect(build.runtime_build, `${build.id} pins a runtime this build does not run`).toBe(
        RUNTIME_BUILD,
      );
    }
    expect(isPlaceholderPin(manifest.runtime_build)).toBe(false);
  });

  it("is the projection of the shipped registry, family for family", () => {
    expect(
      builds,
      "the registry moved without re-emitting the manifest: run " +
        "`pnpm --filter @toorow/card-shell generate:build-identity`",
    ).toEqual(projectionOfTheRegistry());
  });

  it("declares every family the registry declares, and no other", () => {
    const declaredFamilies = registered()
      .flatMap((declaration) => declaration.families)
      .sort();
    expect(builds.map((build) => build.family).sort()).toEqual(declaredFamilies);
    // A build id is what migration 160 keys on: two rows for one id would make
    // the ledger ambiguous about which renderer a pin names.
    expect(new Set(builds.map((build) => build.id)).size).toBe(builds.length);
    expect(builds.length).toBeGreaterThan(0);
  });

  it("carries the theme and formatter versions a Render pins beside them", () => {
    for (const build of builds) {
      expect(build.theme_version).toBe(THEME_VERSION);
      expect(build.formatter_version).toBe(FORMATTER_VERSION);
    }
  });

  it("is NOT part of the identity it stamps, so the two generators converge", () => {
    // The fixed-point repair of 2026-08-24. A file that carries the hash cannot
    // feed it -- the rule `buildInfo.generated.ts` already had, applied to the
    // whole class.
    expect(HASH_EXCLUDES).toContain("rendererBuilds.generated.json");
    const keys = (runtimeSourceFiles() as { key: string }[]).map((file) => file.key);
    expect(keys).not.toContain("rendererBuilds.generated.json");
    expect(keys).not.toContain("buildInfo.generated.ts");
    // And the files that draw are still in it, so the exclusion did not empty it.
    expect(keys).toContain("Runtime.tsx");
    expect(keys).toContain("registry.ts");
  });
});
