/**
 * Story 50.5 AC14 -- what a runtime build identity is MADE OF.
 *
 * IT IS NOT A GIT SHA, AND THE REASON IS ARITHMETIC RATHER THAN TASTE.
 * The first implementation read `git rev-parse HEAD` into a TRACKED file wired to
 * `pretest`/`prebuild`. A generator that runs BEFORE a commit can only ever
 * record the commit before the one that will contain the code: the landed value
 * pinned `4924e0a`, a parallel session's commit whose tree contains no `viz/` at
 * all, while the refusal message told the reader to "check out the pinned build
 * to replay it". It also rewrote the file on every test run, so it was
 * permanently dirty in a repository with four active sessions.
 *
 * A CONTENT HASH OF THE RUNTIME SOURCES HAS NEITHER PROBLEM. It is derived from
 * the very bytes that draw the chart, so it cannot name the wrong tree; and it
 * changes only when `src/viz/**` changes, so an unrelated commit in another
 * session leaves it alone and the generated file stops being noise.
 *
 * WHAT IS HASHED, stated so a reader can reproduce it:
 *   every file under `src/viz/`, sorted by POSIX-normalized relative path,
 *   EXCLUDING `__tests__/` (tests are not in the bundle) and the two generated
 *   files that CARRY the identity -- `buildInfo.generated.ts` and
 *   `rendererBuilds.generated.json` -- because a file that carries the hash
 *   cannot feed it.
 *   Each file contributes its path, a NUL, and its contents with CRLF normalized
 *   to LF -- a Windows checkout and a Linux checkout must produce one identity.
 *
 * WHY THE MANIFEST IS EXCLUDED, and it is a REPAIR rather than a preference,
 * measured 2026-08-24. `rendererBuilds.generated.json` was inside the hash while
 * stamping that same hash on all eight of its builds, so the pair had NO FIXED
 * POINT: emitting the manifest at identity `aff93cd8d3c6` moved the sources to
 * `c0cefc614674`, which the next emission would have moved again. No ordering of
 * the two generators converges, and that is why the repository carried three
 * identities two generations apart. The exclusion is the rule this file already
 * stated for `buildInfo.generated.ts`, applied to the whole class of files that
 * carry the identity instead of only the first one found.
 *
 * Twelve hex characters, which satisfies the `[0-9a-f]{7,40}` shape the
 * `runtime_build` CHECK in migration 160 requires.
 */
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
export const PACKAGE_ROOT = resolve(here, "..");
export const VIZ_ROOT = resolve(PACKAGE_ROOT, "src", "viz");

/** Named so a reader knows what the identity does and does not cover. */
export const HASH_EXCLUDES = [
  "__tests__",
  "buildInfo.generated.ts",
  "rendererBuilds.generated.json",
];

function collect(dir, out) {
  for (const entry of readdirSync(dir).sort()) {
    if (HASH_EXCLUDES.includes(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) collect(full, out);
    else out.push(full);
  }
  return out;
}

export function runtimeSourceFiles() {
  return collect(VIZ_ROOT, [])
    .map((file) => ({ file, key: relative(VIZ_ROOT, file).split(sep).join("/") }))
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
}

export function runtimeIdentity() {
  const pkg = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));
  const hash = createHash("sha256");
  for (const { file, key } of runtimeSourceFiles()) {
    hash.update(key);
    hash.update("\0");
    hash.update(readFileSync(file, "utf8").replace(/\r\n/g, "\n"));
    hash.update("\0");
  }
  return { semver: pkg.version, contentHash: hash.digest("hex").slice(0, 12) };
}
