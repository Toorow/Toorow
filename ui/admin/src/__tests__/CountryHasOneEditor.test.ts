/**
 * Country has ONE editor, and it is the governed Registry.
 *
 * Story 48.2 AC1: "The orphaned `CountrySplit.tsx`, its private stylesheet and
 * all Connectors/Data-module placement are removed." The story deliberately did
 * NOT remove them at the time — its own Task 6 says the screen "is the trace of
 * a screen that must be replaced, and deleting it before its replacement exists
 * erases the inventory of the work remaining". The replacement now exists
 * (`governance/CountryWorkspace.tsx`, mounted by `GovernanceObjectWorkbench` on
 * object type `registry`, tab `hierarchy`, backed by
 * `/api/projects/{id}/governance/master-data/country`), so the trace is removed
 * and this guard takes its place.
 *
 * What made the old screen worse than dead code: it read
 * `project_preferences.geographic_mode` and `local_market_country_codes` — the
 * read-layer authority THIS SAME STORY retired (Task 2) — and it was mounted on
 * a `country-market` lens that `shell/navigation.ts` never declared, so no lens
 * tab could reach it. A second authority, unreachable, still compiling.
 *
 * Textual on purpose, like `applicationCssLayering.test.ts`: a render assertion
 * cannot fail on a file that merely still exists, and "still exists" is exactly
 * the defect.
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Walk up from the working directory until the path is found. Same reason as
 *  `applicationCssLayering.test.ts`: two test files in this repo returned two
 *  different verdicts for the same tree because they resolved against `cwd`. */
function locateRoot(): string {
  let dir = resolve(process.cwd());
  for (let i = 0; i < 6; i += 1) {
    if (existsSync(join(dir, "src", "shell", "navigation.ts"))) return join(dir, "src");
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`cannot locate ui/admin/src from ${process.cwd()}`);
}

const SRC = locateRoot();

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

const FILES = walk(SRC);
const rel = (p: string) => p.slice(SRC.length + 1).replace(/\\/g, "/");

describe("Country has one editor", () => {
  it("carries no CountrySplit screen and no country-split stylesheet", () => {
    const offenders = FILES.filter((p) => /country-split\.css$|CountrySplit\.tsx$/.test(p)).map(rel);
    expect(offenders).toEqual([]);
  });

  it("has no module importing or mounting CountrySplit", () => {
    const offenders = FILES.filter((p) => !p.endsWith("CountryHasOneEditor.test.ts"))
      .filter((p) => /import\s+.*CountrySplit|<CountrySplit|from\s+["'].*CountrySplit/.test(readFileSync(p, "utf8")))
      .map(rel);
    expect(offenders).toEqual([]);
  });

  it("keeps the governed Country editor mounted on the registry hierarchy tab", () => {
    // The removal above is only legitimate because this mount exists. If the
    // workbench branch goes, this guard fails rather than leaving Country with
    // no editor at all — the outcome Task 6 refused to risk.
    const workbench = readFileSync(join(SRC, "governance", "GovernanceObjectWorkbench.tsx"), "utf8");
    expect(workbench).toMatch(/CountryWorkspace/);
    expect(workbench).toMatch(/objectType === "registry"/);
    const workspace = readFileSync(join(SRC, "governance", "CountryWorkspace.tsx"), "utf8");
    expect(workspace).toMatch(/governance\/master-data\/country/);
  });

  it("re-derives geography from no retired project_preferences field", () => {
    // The pre-48.2 store. Measured at the removal: zero readers left in `src`,
    // so this guard starts at zero and any re-introduction is a regression, not
    // a debt to negotiate.
    const offenders = FILES.filter((p) => !rel(p).startsWith("__tests__/"))
      .filter((p) => /local_market_country_codes|geographic_mode/.test(readFileSync(p, "utf8")))
      .map(rel);
    expect(offenders).toEqual([]);
  });
});
