#!/usr/bin/env node
/**
 * Standalone build-path smoke check for the google-analytics widget (Story 1.6).
 *
 * No test runner required (Vitest is Story 1.7). Verifies that the built
 * single-file bundle:
 *   1. exists at dist/index.html,
 *   2. contains the #root mount point and the widget's React runtime,
 *   3. contains NO Material Symbols @font-face (AC7 subsetting worked),
 *   4. is under the 1.5 MB uncompressed AC7 budget,
 *   5. reports its gzip size for the Dev Agent Record.
 *
 * Usage: node scripts/smoke.mjs [path-to-dist/index.html]
 */
import { readFileSync, existsSync, statSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { fileURLToPath } from "node:url";

const target =
  process.argv[2] ||
  fileURLToPath(new URL("../dist/index.html", import.meta.url));

if (!existsSync(target)) {
  console.error(`smoke: bundle not found: ${target} — run the widget build first`);
  process.exit(2);
}

const html = readFileSync(target, "utf8");
const bytes = statSync(target).size;
const gzip = gzipSync(html).length;

const checks = [];
function check(name, cond) {
  checks.push({ name, ok: !!cond });
}

check('#root mount point present', html.includes('id="root"'));
check("React runtime inlined", /react/i.test(html));
check(
  "no Material Symbols @font-face (AC7 subsetting)",
  !/Material Symbols Outlined/.test(html),
);
check("Roboto Flex present (text face kept)", /Roboto Flex/i.test(html));
check("under 1.5 MB uncompressed (AC7)", bytes < 1572864);

let failed = false;
for (const c of checks) {
  console.log(`${c.ok ? "✅" : "❌"} ${c.name}`);
  if (!c.ok) failed = true;
}

console.log(
  `\nBundle: ${bytes} bytes uncompressed (${(bytes / 1024 / 1024).toFixed(2)} MB), ` +
    `${gzip} bytes gzip (${(gzip / 1024).toFixed(0)} KB).`,
);

process.exit(failed ? 1 : 0);
