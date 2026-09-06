/**
 * Story 50.5 Task 2 -- the build config `@toorow/card-shell` never had.
 *
 * Two SINGLE-FILE bundles, emitted to `dist/viz/`:
 *   viz/mcp-app.html  -- the MCP App resource `ui://core/visualization-runtime`
 *                        serves (AD-11: one self-contained, token-themed runtime;
 *                        CI fails on a forbidden `http(s):` bundle reference).
 *   viz/share.html    -- the bundle Story 50.7 will mount into the public share
 *                        page's existing `#widget-mount`.
 *
 * ONE ENTRY PER INVOCATION, on purpose. `vite-plugin-singlefile` turns code
 * splitting off so it has a single chunk to inline, and Rollup refuses code
 * splitting off with two inputs. Building them separately is the honest fix; a
 * shared chunk between the two would defeat the point of a self-contained
 * resource anyway. `pnpm build` runs both (see package.json).
 */

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { viteSingleFile } from "vite-plugin-singlefile";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { FORMATTER_VERSION, RUNTIME_BUILD, THEME_VERSION } from "./src/viz/buildInfo";
import { registered } from "./src/viz/registry";
import { installStandardRenderers } from "./src/viz/renderers";

const root = __dirname;

installStandardRenderers();

const ENTRIES = {
  "mcp-app": { html: "viz-mcp-app.html", out: "mcp-app.html" },
  share: { html: "viz-share.html", out: "share.html" },
} as const;

const requested = (process.env.TOOROW_VIZ_ENTRY ?? "mcp-app") as keyof typeof ENTRIES;
const entry = ENTRIES[requested];
if (!entry) {
  throw new Error(
    `TOOROW_VIZ_ENTRY must be one of ${Object.keys(ENTRIES).join(", ")}; received "${requested}".`,
  );
}

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    viteSingleFile({ removeViteModuleLoader: true }),
    {
      // Vite emits `viz-<entry>.html` at the dist root; the resource contract
      // names `dist/viz/<entry>.html`. Renaming here keeps the served path
      // stable no matter what the entry file is called.
      name: "toorow-viz-layout",
      closeBundle() {
        const dist = resolve(root, "dist");
        const target = resolve(dist, "viz");
        mkdirSync(target, { recursive: true });
        const source = resolve(dist, entry.html);
        if (!existsSync(source)) return;

        // AD-11 / `ui/scripts/bundle-check.mjs`: the bundle may carry NO
        // `http(s):` reference. Tailwind's minified output ends with a legal
        // banner that contains its project URL. It is a comment, not a fetch --
        // but the gate cannot tell a comment from a stylesheet link, and a gate
        // that has to be argued with is a gate nobody trusts. So the protocol is
        // stripped INSIDE comment blocks only: the attribution stays legible and
        // complete, and nothing in the file is loadable. No other bytes move.
        const html = readFileSync(source, "utf8").replace(
          /\/\*[\s\S]*?\*\//g,
          (comment) => comment.replace(/https?:\/\//g, ""),
        );
        const output = resolve(target, entry.out);
        writeFileSync(output, html, "utf8");
        if (requested === "mcp-app") {
          const renderers: Record<string, unknown> = {};
          for (const renderer of [...registered()].sort((a, b) =>
            a.families[0]!.localeCompare(b.families[0]!),
          )) {
            for (const family of renderer.families) {
              if (renderers[family]) {
                throw new Error(`Runtime manifest has two renderers for "${family}".`);
              }
              renderers[family] = {
                renderer_build: renderer.build,
                schema_versions: renderer.schemaVersions,
                profiles: renderer.profiles,
              };
            }
          }
          const finalBytes = readFileSync(output);
          const manifest = {
            schema_version: 1,
            bundle_sha256: createHash("sha256").update(finalBytes).digest("hex"),
            runtime_build: RUNTIME_BUILD,
            theme_version: THEME_VERSION,
            formatter_version: FORMATTER_VERSION,
            renderers,
          };
          writeFileSync(
            resolve(target, "runtime-manifest.json"),
            `${JSON.stringify(manifest, null, 2)}\n`,
            "utf8",
          );
        }
        rmSync(source);
      },
    },
  ],
  build: {
    outDir: "dist",
    // The two entries are built in sequence; the second must not erase the first.
    emptyOutDir: false,
    cssCodeSplit: false,
    rollupOptions: { input: resolve(root, entry.html) },
  },
});
