/**
 * Story 50.5 AC14 -- the build identity is the identity of the code that draws.
 *
 * THIS FILE IS THE GUARD THE `pretest` HOOK USED TO IMPERSONATE. The generator
 * was wired to `pretest`, so every test run silently rewrote the identity and no
 * assertion could ever observe it being wrong. Regenerating is now an explicit
 * step (`pnpm generate:build-identity`, and `prebuild`); this test recomputes the
 * identity from the runtime sources on disk and fails when the committed file no
 * longer matches them.
 *
 * It also pins the two properties the old git-SHA identity could not have:
 *   * the identity is DERIVED FROM `src/viz/**`, so it cannot name a tree that
 *     does not contain the runtime;
 *   * it is REPRODUCIBLE by anyone, with one command, from a checkout with no
 *     git history at all.
 */

import { describe, expect, it } from "vitest";

import { RUNTIME_BUILD, RUNTIME_PACKAGE, isPlaceholderPin, rendererBuild } from "../buildInfo";
import { RUNTIME_CONTENT_HASH, RUNTIME_SEMVER } from "../buildInfo.generated";
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore -- a build script, deliberately untyped and shared with `prebuild`.
import { runtimeIdentity, runtimeSourceFiles } from "../../../scripts/runtime-identity.mjs";

describe("AC14 -- the runtime build identity", () => {
  it("matches the runtime sources on disk (regenerate if this fails)", () => {
    const fresh = runtimeIdentity() as { semver: string; contentHash: string };
    expect(
      { semver: RUNTIME_SEMVER, contentHash: RUNTIME_CONTENT_HASH },
      "src/viz/** changed without regenerating the build identity: run " +
        "`pnpm --filter @toorow/card-shell generate:build-identity` and commit both " +
        "generated files",
    ).toEqual(fresh);
  });

  it("is derived from the runtime sources, which are more than one file", () => {
    const files = runtimeSourceFiles() as { key: string }[];
    expect(files.length).toBeGreaterThan(10);
    // Tests are not in the bundle, so they are not in the identity.
    expect(files.some((f) => f.key.startsWith("__tests__/"))).toBe(false);
    // Nor is the generated file itself: it carries the hash.
    expect(files.some((f) => f.key === "buildInfo.generated.ts")).toBe(false);
    // The files that draw ARE.
    for (const key of ["Runtime.tsx", "registry.ts", "compile/dataset.ts"]) {
      expect(files.some((f) => f.key === key), `${key} is not in the identity`).toBe(true);
    }
  });

  it("carries package, semver and a hex content hash, and is not a placeholder", () => {
    expect(RUNTIME_BUILD).toMatch(/^@toorow\/card-shell\/viz@\d+\.\d+\.\d+\+[0-9a-f]{7,40}$/);
    expect(RUNTIME_BUILD.startsWith(`${RUNTIME_PACKAGE}@`)).toBe(true);
    expect(isPlaceholderPin(RUNTIME_BUILD)).toBe(false);
  });

  it("the shape satisfies migration 160's runtime_build CHECK", () => {
    // `ck_renderer_runtime_builds_runtime_shape`, mirrored here so a change to
    // the identity shape fails in the runtime's own suite and not only in pg.
    const CHECK = /^@toorow\/card-shell\/viz@[0-9]+\.[0-9]+\.[0-9]+\+[0-9a-f]{7,40}$/;
    expect(CHECK.test(RUNTIME_BUILD)).toBe(true);
    expect(/^[a-z0-9_]+\/[a-z0-9-]+@[0-9]+\.[0-9]+\.[0-9]+$/.test(
      rendererBuild("stacked_bar", "toorow-echarts-stacked-bar", "1.0.0"),
    )).toBe(true);
  });

  it("refuses `unbuilt`, the value a generator that cannot read the sources emits", () => {
    expect(isPlaceholderPin("@toorow/card-shell/viz@0.1.0+unbuilt")).toBe(true);
    expect(isPlaceholderPin("latest")).toBe(true);
    expect(isPlaceholderPin("")).toBe(true);
  });
});
