/**
 * Emit the build ledger's PROJECTION SOURCE from the shipped renderer registry.
 *
 * WHY THIS EXISTS, measured 2026-08-13. `app.renderer_runtime_builds` is what a
 * Render's `renderer_build` and `runtime_build` pins point at: without a row the
 * pin names a build the deployment cannot claim it shipped. The ledger held ONE
 * row for EIGHT declared families, and that row had been inserted by hand. Seven
 * families were un-pinnable -- including `table`, the simplest thing the product
 * draws. Nothing was wrong with the renderers; nothing carried them across.
 *
 * IT READS THE REGISTRY, IT DOES NOT RESTATE IT. The declarations are loaded from
 * `renderers/index.ts` itself, through Vite's SSR loader, so a family added there
 * appears here without an edit. A second hand-written list is exactly the drift
 * this file removes -- see `visualization-and-rendering.md`, "The build ledger is
 * a PROJECTION of the shipped registry".
 *
 * THE OUTPUT IS COMMITTED, like `buildInfo.generated.ts` and for the same reason:
 * the deploy-time projection (`scripts/register_renderer_builds.py`) must run on
 * a checkout with no node toolchain. A stale manifest is a RED TEST
 * (`__tests__/rendererBuilds.test.ts`), never a silent lie.
 *
 * IT IS THE SECOND HALF OF ONE GESTURE, AND IT RUNS AFTER THE FIRST. The manifest
 * stamps `RUNTIME_BUILD` on every build it emits, so it must be written after
 * `generate-build-info.mjs` has settled that value. The output is EXCLUDED from
 * the identity hash (`runtime-identity.mjs` HASH_EXCLUDES): it was inside it
 * until 2026-08-24, which gave the pair no fixed point -- emitting at
 * `aff93cd8d3c6` moved the sources to `c0cefc614674` -- and that is why three
 * identities in this repository were two generations apart.
 *
 * Run: pnpm --filter @toorow/card-shell generate:build-identity
 *      (this script alone: node scripts/emit-renderer-builds.mjs)
 */
import { writeFileSync } from "node:fs";
import { join } from "node:path";

import { createServer } from "vite";

import { PACKAGE_ROOT } from "./runtime-identity.mjs";

const server = await createServer({
  configFile: false,
  root: PACKAGE_ROOT,
  appType: "custom",
  logLevel: "error",
  server: { middlewareMode: true },
});

try {
  const registry = await server.ssrLoadModule("/src/viz/registry.ts");
  const renderers = await server.ssrLoadModule("/src/viz/renderers/index.ts");
  const buildInfo = await server.ssrLoadModule("/src/viz/buildInfo.ts");

  renderers.installStandardRenderers();
  const declared = registry.registered();
  if (!declared.length) {
    throw new Error("the registry declared nothing: refusing to emit an empty ledger source");
  }

  const runtimeBuild = buildInfo.RUNTIME_BUILD;
  const builds = declared
    .flatMap((declaration) =>
      declaration.families.map((family) => ({
        // `<family>/<renderer_id>@<semver>` -- the id migration 160 keys on.
        id: declaration.build,
        family,
        renderer_id: declaration.renderer_id,
        runtime_build: runtimeBuild,
        theme_version: buildInfo.THEME_VERSION,
        formatter_version: buildInfo.FORMATTER_VERSION,
        responsive_profiles: [...declaration.profiles],
      })),
    )
    .sort((left, right) => left.id.localeCompare(right.id));

  const target = join(PACKAGE_ROOT, "src", "viz", "rendererBuilds.generated.json");
  writeFileSync(
    target,
    `${JSON.stringify(
      {
        _generated_by: "ui/cards/shell/scripts/emit-renderer-builds.mjs -- do not edit",
        runtime_build: runtimeBuild,
        builds,
      },
      null,
      2,
    )}\n`,
    "utf8",
  );
  process.stdout.write(`${builds.length} renderer build(s) -> ${target}\n`);
} finally {
  await server.close();
}
