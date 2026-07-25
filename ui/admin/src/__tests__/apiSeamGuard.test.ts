/**
 * Structural guard for the API access seam — finding F-010.
 *
 * F-010 was not a logic bug, it was an omission: `ScopeProvider` called
 * `fetch("/api/organizations")` with no Authorization header, got 401, and
 * silently substituted a hard-coded organization. Nothing in the codebase made
 * that omission visible.
 *
 * This test is that mechanism. Every `fetch(` in ui/admin must either be
 * `apiFetch(` (src/lib/apiFetch.ts, which always attaches the bearer token) or
 * carry auth headers of its own. A new bare `fetch("/api/...")` fails here, at
 * the seam, instead of failing silently in production.
 *
 * There is no lint config in ui/admin (package.json has build/test/typecheck
 * only), so the invariant is enforced as a test.
 */
/*
 * Sources are read through Vite's `import.meta.glob` rather than node:fs: this
 * workspace declares `types: ["vitest/globals"]` and carries no @types/node, so
 * the fs route would not typecheck — and adding a dependency to type one guard
 * would be the wrong trade. `?raw` gives the file text, typed via vite-env.d.ts.
 */
const SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

/**
 * Files allowed to call bare `fetch` while this finding is being closed.
 * Each entry is a debt to remove — the target is an empty list.
 *   - shell/pages/CreateOrg.tsx: owned by a parallel change on the same finding;
 *     it is being reworked there, not here.
 */
const ALLOWLIST = new Set(["shell/pages/CreateOrg.tsx"]);

/** Signals that a call site builds its own authenticated headers. */
const AUTH_SIGNALS = [
  "Authorization",
  "authHeader",
  "authHeaders(",
  "headers()",
  "jsonHeaders(",
  "headers,",
  "headers }",
  "...headers",
  "headers)",
];

/** Paths under src/, excluding the tests, the generated code and the seam itself. */
function sourceFiles(): string[] {
  return Object.keys(SOURCES)
    .map((key) => key.replace(/^\.\.\//, ""))
    .filter(
      (rel) =>
        !rel.startsWith("__tests__/") &&
        !rel.startsWith("generated/") &&
        rel !== "lib/apiFetch.ts"
    )
    .sort();
}

function sourceOf(relPath: string): string {
  return SOURCES[`../${relPath}`] ?? "";
}

/**
 * The same source with its comments removed.
 *
 * The seed assertions below must judge the CODE, not the prose about it: the
 * header of scope.tsx legitimately names the "Toorow Core / Default project"
 * fallback in order to explain why it was removed, and that explanation is worth
 * keeping. Matching raw file text would forbid documenting the very defect this
 * guard exists to prevent.
 */
function codeOf(relPath: string): string {
  return sourceOf(relPath)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

/** `fetch(` occurrences that are neither `apiFetch(` nor obviously authenticated. */
function bareFetchCalls(relPath: string): string[] {
  const lines = sourceOf(relPath).split("\n");
  const found: string[] = [];
  lines.forEach((line: string, i: number) => {
    if (line.trimStart().startsWith("*")) return; // doc comment
    if (!/(?<![A-Za-z0-9_])fetch\s*\(/.test(line)) return;
    // The call's options usually follow within a few lines; scan a window.
    const window = lines.slice(i, i + 20).join("\n");
    if (AUTH_SIGNALS.some((s) => window.includes(s))) return;
    found.push(`${relPath}:${i + 1} — ${line.trim()}`);
  });
  return found;
}

describe("API access seam", () => {
  it("has no unauthenticated fetch call outside the allowlist", () => {
    const violations = sourceFiles()
      .filter((f) => !ALLOWLIST.has(f))
      .flatMap(bareFetchCalls);
    expect(
      violations,
      `Unauthenticated fetch call(s) found. Route them through apiFetch() ` +
        `from src/lib/apiFetch.ts:\n${violations.join("\n")}`
    ).toEqual([]);
  });

  it("keeps the scope provider free of any invented fallback org", () => {
    const scope = codeOf("shell/scope.tsx");
    // The exact seed that F-010 surfaced to every user.
    expect(scope).not.toContain("SEED_ORGS");
    expect(scope).not.toContain("Toorow Core");
    expect(scope).not.toContain("Default project");
    // …and it must go through the seam.
    expect(sourceOf("shell/scope.tsx")).toContain("apiFetch");
  });
});
