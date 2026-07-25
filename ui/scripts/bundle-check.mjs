#!/usr/bin/env node
/**
 * AD-11 / NFR4 bundle gate.
 *
 * Scans a built single-file widget (dist/index.html) for external `http(s)://`
 * references and FAILS (exit 1) if a LOAD-BEARING one is found. The intent of
 * NFR4 is "no network fetch at render time": no external scripts, styles,
 * fonts, images, iframes, or fetch targets.
 *
 * Two classes of http(s) occurrences are inert by construction and allowed:
 *   1. XML namespace identifiers (xmlns) — spec-mandated identifiers inside
 *      inlined SVG/MathML, never dereferenced by the browser.
 *   2. Documentation URLs inside minified error messages (react.dev/errors,
 *      mui.com/production-error) — string literals shown in console output,
 *      never fetched.
 * Anything else http(s) fails the gate. Additions to INERT_PATTERNS require a
 * written justification in the PR.
 *
 * Usage:  node scripts/bundle-check.mjs <path-to-index.html> [--max-bytes <N>]
 *
 * --max-bytes <N>  Optional. Fail (exit 1) if the built file exceeds N bytes
 *                  uncompressed. When omitted, no size check is performed.
 *                  Widget CI: 1500000 (1.5 MB). Admin console CI: 3000000 (3 MB).
 */
import { readFileSync, existsSync, statSync } from "node:fs";

const INERT_PATTERNS = [
  // W3C namespace identifiers (xmlns / xmlnsXlink attrs in inlined SVG & co).
  /^http:\/\/www\.w3\.org\/(2000\/svg|1998\/Math\/MathML|1999\/(xlink|xhtml)|XML\/1998\/namespace)\/?$/,
  // Framework error-documentation links embedded in minified error strings.
  /^https:\/\/react\.dev\/errors(\/|$)/,
  /^https:\/\/mui\.com\/production-error(\/|\?|$)/,
  // Story 9.10 — bundled @modelcontextprotocol/ext-apps SDK (AC4: SDK is bundled,
  // no CDN). Its deps (@modelcontextprotocol/sdk, zod) ship inert string
  // literals; none is dereferenced at render time:
  // JSON Schema $schema spec identifiers (zod/sdk schema constants).
  /^https?:\/\/json-schema\.org\/draft[-/]/,
  // Rolldown (vite 8) CJS-interop warning message embedded by the bundler.
  /^https:\/\/rolldown\.rs\/in-depth\/bundling-cjs/,
  // Zod's IPv6-format validator (bundled by @modelcontextprotocol/ext-apps@1.7.4
  // via zod@4.3.5, dist/src/app-with-deps.js): the check runs
  //   new URL(`http://[${v.value}]`)
  // to validate a candidate IPv6 address by feeding it to the URL parser. The
  // `${…}` is a template placeholder — after minification the inner identifier is
  // an arbitrary short name (e.g. `v.value`, `e`, `n`), hence `[^}]+`. The `]$`
  // anchor keeps this to the bare-host form only (the CIDR sibling
  // `http://[${n}]…` carries a suffix and does not reach the extracted-URL set).
  // This is a validation template literal, never dereferenced at render time —
  // no network fetch, so it is inert w.r.t. NFR4.
  /^http:\/\/\[\$\{[^}]+\}\]$/,
  // ext-apps CSP documentation example (wildcard host — not a fetchable URL).
  /^https:\/\/\*\.example\.com$/,
];

// Attribute/construct contexts that ALWAYS fail regardless of URL: an external
// URL in a load-bearing position is a network fetch by definition.
const LOAD_BEARING_CONTEXT =
  /(?:src|href|srcset|action|data|poster)\s*=\s*["']https?:\/\/|url\(\s*["']?https?:\/\/|@import\s+["']https?:\/\/|(?:fetch|import)\(\s*["']https?:\/\//gi;

// ---------------------------------------------------------------------------
// CLI argument parsing
// argv[2] = path to index.html
// argv[3..] may include --max-bytes <N>
// ---------------------------------------------------------------------------
const args = process.argv.slice(2);
const target = args[0];

let maxBytes = null;
for (let i = 1; i < args.length; i++) {
  if (args[i] === "--max-bytes" && i + 1 < args.length) {
    maxBytes = parseInt(args[i + 1], 10);
    if (isNaN(maxBytes) || maxBytes <= 0) {
      console.error(`bundle-check: --max-bytes must be a positive integer, got: ${args[i + 1]}`);
      process.exit(2);
    }
    i++; // skip the value
  }
}

if (!target) {
  console.error("bundle-check: missing argument -- path to built index.html");
  process.exit(2);
}
if (!existsSync(target)) {
  console.error(`bundle-check: file not found: ${target}`);
  console.error("Did the widget build run first? (pnpm --filter @toorow/widget-sample build)");
  process.exit(2);
}

const html = readFileSync(target, "utf8");

// 1) Hard fail: any external URL in a load-bearing context.
const loadBearing = [...html.matchAll(LOAD_BEARING_CONTEXT)].map((m) => m[0]);

// 2) All other http(s) occurrences must match a known-inert pattern.
const re = /https?:\/\/[^\s"'`)<>\\]+/gi;
const found = [...html.matchAll(re)].map((m) => m[0]);
const unexplained = found.filter((url) => !INERT_PATTERNS.some((p) => p.test(url)));

const violations = [...loadBearing, ...unexplained];

if (violations.length > 0) {
  console.error("❌ bundle-check FAILED — external http(s) reference(s) found in bundle:");
  for (const v of [...new Set(violations)]) {
    console.error(`   ${v}`);
  }
  console.error(
    `\n${violations.length} occurrence(s). NFR4/AD-11: widgets must be fully self-contained.`
  );
  process.exit(1);
}

console.log(`bundle-check PASSED -- ${target} is self-contained (no load-bearing external references).`);

// ---------------------------------------------------------------------------
// Optional size gate (AI-04 / Story 2.4 T1): --max-bytes <N>
// Applies to any bundle: widgets use 1500000, admin console uses 3000000.
// ---------------------------------------------------------------------------
if (maxBytes !== null) {
  const actualBytes = statSync(target).size;
  if (actualBytes > maxBytes) {
    console.error(
      `bundle-check FAILED -- size gate: ${actualBytes} bytes > ${maxBytes} bytes (--max-bytes limit)`
    );
    process.exit(1);
  }
  console.log(`bundle-check size OK -- ${actualBytes} bytes <= ${maxBytes} bytes`);
}

process.exit(0);
