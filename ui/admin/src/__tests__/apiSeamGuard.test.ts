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
/*
 * Vide, et cela doit le rester.
 *
 * CreateOrg.tsx y figurait « le temps qu'un changement parallèle atterrisse ». Cette
 * exception n'a jamais été refermée, et c'est exactement ce qu'elle a coûté : son
 * `fetch` nu partait sans jeton, l'API répondait 401 sous TOOROW_AUTH_MODE=oauth, et
 * l'écran d'accueil du premier login ne pouvait PAS créer d'organisation. La garde
 * passait au vert pendant ce temps — une allowlist est un trou déclaré, pas un sursis.
 * Trouvé par audit le 2026-07-25, refermé le même jour.
 */
const ALLOWLIST = new Set<string>();

/**
 * Signals that a call site gets its credentials from the seam.
 *
 * `"Authorization"` used to head this list, and that was the hole. The whole
 * point of `apiFetch` is that ONE function decides where the token comes from:
 * `apiToken()` reads `window.__TOOROW_API_KEY__` first and falls back to
 * `localStorage`. A call site that writes the header itself picks its own
 * source — and `datamodel/FieldDetailDrawer.tsx` picked `localStorage` alone,
 * across nine calls, so every write on that panel sent no bearer under the
 * deployment that injects the key on `window`. The guard was green throughout:
 * the literal string `Authorization` satisfied it.
 *
 * So spelling the header is no longer an answer. What remains are the two
 * honest ones: the call goes through `apiFetch`/`authHeaders` (the seam), or it
 * forwards headers a CALLER built (`...headers`, `headers,`, `headers }`),
 * which is a wrapper passing the seam's work along rather than inventing its
 * own. `credentials` stays: a cookie-authenticated call carries no bearer at
 * all and has nothing to forget.
 */
const AUTH_SIGNALS = [
  "authHeader",
  "authHeaders(",
  "headers()",
  "jsonHeaders(",
  "headers,",
  "headers }",
  "...headers",
  "headers)",
  "credentials",
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

/** The detector itself, over already-decommented code — so it can be tested. */
function bareFetchesIn(code: string, label: string): string[] {
  const lines = code.split("\n");
  const found: string[] = [];
  lines.forEach((line: string, i: number) => {
    if (line.trimStart().startsWith("*")) return; // doc comment
    if (!/(?<![A-Za-z0-9_])fetch\s*\(/.test(line)) return;
    // The call's options usually follow within a few lines; scan a window.
    const window = lines.slice(i, i + 20).join("\n");
    if (AUTH_SIGNALS.some((s) => window.includes(s))) return;
    found.push(`${label}:${i + 1} — ${line.trim()}`);
  });
  return found;
}

/** `fetch(` occurrences that are neither `apiFetch(` nor obviously authenticated. */
function bareFetchCalls(relPath: string): string[] {
  // codeOf, pas sourceOf : la garde doit juger le CODE, pas la prose. Elle a
  // signalé un commentaire qui EXPLIQUAIT pourquoi un fetch nu était interdit —
  // interdire de documenter le défaut qu'on prévient est absurde.
  return bareFetchesIn(codeOf(relPath), relPath);
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

  /**
   * The guard's own regression test.
   *
   * A guard that passes proves nothing about what it would catch, and this one
   * passed for months over nine hand-rolled `Authorization` headers reading the
   * wrong token source. So the detector is pointed at the exact shape that
   * slipped through, and at the two shapes that must keep going through.
   */
  it("catches a call site that spells its own Authorization header", () => {
    const handRolled = [
      'fetch(`/api/datamodel/fields/${name}`, {',
      '  headers: { Authorization: `Bearer ${localStorage.getItem("api_token") || ""}` },',
      "});",
    ].join("\n");
    expect(bareFetchesIn(handRolled, "synthetic.tsx")).toHaveLength(1);
  });

  it("still lets the seam and a header-forwarding wrapper through", () => {
    const throughTheSeam = 'apiFetch("/api/organizations", { method: "GET" });';
    expect(bareFetchesIn(throughTheSeam, "synthetic.ts")).toEqual([]);

    const forwardsCallerHeaders = [
      "export function withRetry(path: string, headers: Record<string, string>) {",
      "  return fetch(path, { ...init, headers });",
      "}",
    ].join("\n");
    expect(bareFetchesIn(forwardsCallerHeaders, "synthetic.ts")).toEqual([]);

    const cookieAuthenticated = 'fetch("/api/ping", { credentials: "include" });';
    expect(bareFetchesIn(cookieAuthenticated, "synthetic.ts")).toEqual([]);
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
