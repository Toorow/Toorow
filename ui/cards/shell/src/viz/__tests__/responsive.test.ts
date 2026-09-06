/**
 * Story 50.5 AC9 -- the responsive-profile enum has ONE definition, on the server.
 *
 * `responsive.ts` and `contracts.ts` both claimed a test that asserted the
 * TypeScript tuple against the server list. It did not exist: the only assertion
 * on the vocabulary hardcoded the same four strings a second time
 * (`parity.test.tsx`), so a server-side change to
 * `server/core/visualization_specs.py` -- the definition site the database CHECK
 * in migration 156 mirrors -- would have left this package green and the two
 * vocabularies silently different.
 *
 * This file reads the server's tuple from the Python source and compares it. A
 * drift is now a red test rather than a surprise in a browser, which is exactly
 * what the docstrings promised.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { KNOWN_UNSUPPORTED_PROFILES, RESPONSIVE_PROFILES } from "../responsive";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../../..");
const SERVER_SOURCE = resolve(REPO_ROOT, "server/core/visualization_specs.py");

/** The single definition site: `RESPONSIVE_PROFILES: tuple[str, ...] = (...)`. */
function serverProfiles(): string[] {
  const source = readFileSync(SERVER_SOURCE, "utf8");
  const match = source.match(/^RESPONSIVE_PROFILES:\s*tuple\[str, \.\.\.\]\s*=\s*\(([^)]*)\)/m);
  if (!match) {
    throw new Error(
      `RESPONSIVE_PROFILES was not found in ${SERVER_SOURCE}. If it moved, this test must " +
       "follow it -- deleting the test would restore the hole it exists to close.`,
    );
  }
  return [...match[1]!.matchAll(/"([^"]+)"/g)].map((m) => m[1]!);
}

describe("AC9 -- the TypeScript mirror equals the server enum", () => {
  it("the tuple, in the server's order, with nothing added and nothing dropped", () => {
    expect([...RESPONSIVE_PROFILES]).toEqual(serverProfiles());
  });

  it("there is no `compact` on either side -- compaction is a behaviour, not a name", () => {
    expect(serverProfiles()).not.toContain("compact");
    expect(RESPONSIVE_PROFILES as readonly string[]).not.toContain("compact");
  });

  it("`mcp-pip` is in the enum on BOTH sides, and is substituted by neither", () => {
    // The former named divergence, closed 2026-08-24. The ratified Surface parity
    // table lists picture-in-picture; the decision was to build it rather than
    // withdraw it from the target. So it is a value of the grammar on the server,
    // a value of this mirror, and NOT an entry of the substitution map -- a
    // profile that is both drawn and substituted would be two answers to one
    // question, and the substitution is the one a host would see.
    expect(serverProfiles()).toContain("mcp-pip");
    expect(RESPONSIVE_PROFILES as readonly string[]).toContain("mcp-pip");
    expect(Object.keys(KNOWN_UNSUPPORTED_PROFILES)).not.toContain("mcp-pip");
  });

  it("no profile is declared BOTH implemented and substituted", () => {
    // The class the line above is one instance of. The map exists for a profile
    // the target grows before this build draws it; the moment a value appears in
    // both places, `resolveProfile` returns it unsubstituted and the map's entry
    // is a dead statement nobody can observe.
    for (const substituted of Object.keys(KNOWN_UNSUPPORTED_PROFILES)) {
      expect(RESPONSIVE_PROFILES as readonly string[]).not.toContain(substituted);
    }
  });
});
