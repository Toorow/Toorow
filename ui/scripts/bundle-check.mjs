#!/usr/bin/env node
/**
 * AD-11 / artifact budget gate.
 *
 * A file target keeps the historical single-file widget contract: its bytes
 * are scanned for external references and its own size is measured. A directory
 * target is walked recursively: every regular file contributes to the raw size
 * and every emitted text-like file is scanned. Symlinks and unreadable or
 * unsupported entries fail closed.
 *
 * Usage: node scripts/bundle-check.mjs <file-or-directory> [--max-bytes <N>]
 *
 * --max-bytes <N>  Optional raw, uncompressed byte ceiling. Widgets use
 *                  1500000. The complete admin directory uses 6000000.
 */
import {
  closeSync,
  constants,
  existsSync,
  lstatSync,
  openSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { extname, relative, resolve } from "node:path";
import { TextDecoder } from "node:util";

const INERT_PATTERNS = [
  /^http:\/\/www\.w3\.org\/(2000\/svg|1998\/Math\/MathML|1999\/(xlink|xhtml)|XML\/1998\/namespace)\/?$/,
  /^https:\/\/react\.dev\/errors(\/|$)/,
  /^https:\/\/mui\.com\/production-error(\/|\?|$)/,
  /^https?:\/\/json-schema\.org\/draft[-/]/,
  /^https:\/\/rolldown\.rs\/in-depth\/bundling-cjs/,
  /^http:\/\/\[\$\{[^}]+\}\]$/,
  /^https:\/\/\*\.example\.com$/,
  // Identifiers and diagnostic links embedded by ELK / React Flow. They are
  // schema names or error/help text, not browser navigation or fetch targets.
  /^http:\/\/www\.w3\.org\/2001\/XMLSchema#[A-Za-z]+$/,
  /^http:\/\/www\.w3\.org\/Graphics\/SVG\/1\.1\/DTD\/svg11\.dtd$/,
  /^http:\/\/www\.w3\.org\/2000\/xmlns\/?$/,
  /^http:\/\/www\.eclipse\.org\/(emf\/(2002\/Ecore|2003\/XMLType)|elk\/ElkGraph)$/,
  /^http:\/\/\/org\/eclipse\/emf\/ecore\/util\/ExtendedMetaData$/,
  /^https:\/\/reactflow\.dev\/?$/,
  /^https:\/\/\$\{[^}]+\}flow\.dev\/error#001$/,
  /^https:\/\/…$/,
];

// The admin console is a network application, unlike a single-file widget.
// These exact browser destinations are product dependencies and may therefore
// occur in its emitted directory. Everything else, including an indirect URL
// stored in a variable before fetch/window.open, fails closed.
const ADMIN_RUNTIME_PATTERNS = [
  /^https:\/\/accounts\.google\.com\/gsi\/client$/,
  /^https:\/\/api\.nango\.dev$/,
  /^https:\/\/reactflow\.dev\?utm_source=attribution$/,
];

const LOAD_BEARING_CONTEXT =
  /(?:src|href|srcset|action|data|poster)\s*=\s*["'`]?https?:\/\/|content\s*=\s*["'`][^"'`]*url\s*=\s*https?:\/\/|url\(\s*["']?https?:\/\/|@import\s+["'`]https?:\/\/|(?:fetch|import|Worker|SharedWorker|EventSource|WebSocket)\(\s*["'`]https?:\/\//gi;
const URL_PATTERN = /(?:https?|wss?):\/\/[^\s"'`)<>\\]+/gi;
const TEXT_EXTENSIONS = new Set([
  ".css",
  ".cjs",
  ".htm",
  ".html",
  ".js",
  ".json",
  ".map",
  ".mjs",
  ".svg",
  ".text",
  ".txt",
  ".webmanifest",
  ".xml",
]);
const BINARY_EXTENSIONS = new Set([
  ".avif",
  ".gif",
  ".ico",
  ".jpeg",
  ".jpg",
  ".png",
  ".webp",
  ".woff",
  ".woff2",
]);

const args = process.argv.slice(2);
const target = args[0];
let maxBytes = null;
for (let i = 1; i < args.length; i++) {
  if (args[i] === "--max-bytes") {
    const value = args[i + 1];
    if (!value || !/^[1-9]\d*$/.test(value)) {
      console.error(
        `bundle-check: --max-bytes must be a positive integer, got: ${value ?? "missing"}`,
      );
      process.exit(2);
    }
    maxBytes = Number(value);
    if (!Number.isSafeInteger(maxBytes)) {
      console.error(`bundle-check: --max-bytes exceeds the safe integer range: ${value}`);
      process.exit(2);
    }
    i += 1;
  } else {
    console.error(`bundle-check: unknown argument: ${args[i]}`);
    process.exit(2);
  }
}

if (!target) {
  console.error("bundle-check: missing argument -- path to built file or directory");
  process.exit(2);
}
if (!existsSync(target)) {
  console.error(`bundle-check: target not found: ${target}`);
  console.error("Did the UI artifact build run first?");
  process.exit(2);
}

function failClosed(message) {
  console.error(`bundle-check FAILED -- ${message}`);
  process.exit(1);
}

function checkedStat(path) {
  try {
    const stat = lstatSync(path);
    if (stat.isSymbolicLink()) {
      failClosed(`symbolic links are not allowed: ${path}`);
    }
    return stat;
  } catch (error) {
    failClosed(`cannot inspect ${path}: ${error instanceof Error ? error.message : error}`);
  }
}

function proveReadable(path) {
  try {
    const descriptor = openSync(path, constants.O_RDONLY);
    closeSync(descriptor);
  } catch (error) {
    failClosed(`cannot read ${path}: ${error instanceof Error ? error.message : error}`);
  }
}

function collectDirectory(root) {
  const files = [];
  function visit(directory) {
    let entries;
    try {
      entries = readdirSync(directory, { withFileTypes: true }).sort((left, right) =>
        left.name < right.name ? -1 : left.name > right.name ? 1 : 0,
      );
    } catch (error) {
      failClosed(
        `cannot enumerate ${directory}: ${error instanceof Error ? error.message : error}`,
      );
    }
    for (const entry of entries) {
      const path = resolve(directory, entry.name);
      const stat = checkedStat(path);
      if (stat.isDirectory()) {
        visit(path);
      } else if (stat.isFile()) {
        proveReadable(path);
        files.push({ path, size: stat.size });
      } else {
        failClosed(`unsupported artifact entry: ${path}`);
      }
    }
  }
  visit(root);
  return files;
}

const absoluteTarget = resolve(target);
const targetStat = checkedStat(absoluteTarget);
let files;
let directoryMode = false;
if (targetStat.isFile()) {
  proveReadable(absoluteTarget);
  files = [{ path: absoluteTarget, size: targetStat.size }];
} else if (targetStat.isDirectory()) {
  directoryMode = true;
  files = collectDirectory(absoluteTarget);
} else {
  failClosed(`target is not a regular file or directory: ${target}`);
}
if (directoryMode && files.length === 0) {
  failClosed(`artifact directory is empty: ${target}`);
}
if (
  directoryMode &&
  !files.some((file) => relative(absoluteTarget, file.path).replaceAll("\\", "/") === "index.html")
) {
  failClosed(`artifact directory has no root index.html: ${target}`);
}

const violations = [];
function matchesAny(url, patterns) {
  return patterns.some((pattern) => pattern.test(url));
}

for (const file of files) {
  const extension = extname(file.path).toLowerCase();
  if (directoryMode && BINARY_EXTENSIONS.has(extension)) continue;
  if (directoryMode && !TEXT_EXTENSIONS.has(extension)) {
    failClosed(`unsupported artifact file type: ${file.path}`);
  }
  let text;
  try {
    const bytes = readFileSync(file.path);
    if (
      (bytes[0] === 0xff && bytes[1] === 0xfe) ||
      (bytes[0] === 0xfe && bytes[1] === 0xff)
    ) {
      failClosed(`text artifact must be UTF-8, not UTF-16: ${file.path}`);
    }
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    failClosed(
      `cannot decode ${file.path} as UTF-8: ${error instanceof Error ? error.message : error}`,
    );
  }
  const loadBearing = [...text.matchAll(LOAD_BEARING_CONTEXT)].map((match) => ({
    fragment: match[0],
    url: text.slice(match.index).match(URL_PATTERN)?.[0] ?? null,
  }));
  const found = [...text.matchAll(URL_PATTERN)].map((match) => match[0]);
  const allowedPatterns = directoryMode
    ? [...INERT_PATTERNS, ...ADMIN_RUNTIME_PATTERNS]
    : INERT_PATTERNS;
  const unexplained = found.filter((url) => !matchesAny(url, allowedPatterns));
  const forbiddenLoads = loadBearing.filter(
    ({ url }) => !directoryMode || !url || !matchesAny(url, ADMIN_RUNTIME_PATTERNS),
  );
  const displayPath = directoryMode
    ? relative(absoluteTarget, file.path).replaceAll("\\", "/")
    : target;
  for (const value of [...forbiddenLoads.map(({ fragment }) => fragment), ...unexplained]) {
    violations.push({ path: displayPath, value });
  }
}

if (violations.length > 0) {
  console.error("bundle-check FAILED -- external http(s) reference(s) found in artifact:");
  for (const violation of new Map(
    violations.map((item) => [`${item.path}\0${item.value}`, item]),
  ).values()) {
    console.error(`   ${violation.path}: ${violation.value}`);
  }
  console.error(
    `${violations.length} occurrence(s). NFR4/AD-11 forbids load-bearing external references.`,
  );
  process.exit(1);
}

const actualBytes = files.reduce((total, file) => total + file.size, 0);
const kind = directoryMode ? `${files.length} regular file(s)` : "single file";
console.log(
  `bundle-check PASSED -- ${target} (${kind}) has no load-bearing external references.`,
);

if (maxBytes !== null) {
  if (actualBytes > maxBytes) {
    console.error(
      `bundle-check FAILED -- size gate: ${actualBytes} bytes > ${maxBytes} bytes (--max-bytes limit)`,
    );
    process.exit(1);
  }
  console.log(`bundle-check size OK -- ${actualBytes} bytes <= ${maxBytes} bytes`);
}
