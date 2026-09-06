/**
 * Two ratchets on the CAUSE of the visual debt, not on its symptom.
 *
 * WHY THIS EXISTS, measured on 2026-08-04. The ratified target has been
 * Tailwind 4.3 + shadcn since 2026-07-29, and the tree looked like this:
 *
 *     319 .tsx        24 primitives available
 *      54 use the primitives            (17%)
 *      24 actually import @mui/material (a naive string match says 45)
 *         -> 19 after this commit's Tooltip migration
 *      30 hard-coded #rrggbb -> 16: comments excluded, the status palette
 *         deduplicated, then pointed at the semantic ramp that existed
 *         all along (origin.semantic.*, emitted into styles/theme.css)
 *      29 stylesheets / 5151 CSS lines  -- EXACTLY the recorded ratchet
 *
 * That last number is the tell. `known-debt.json.css_lines` was armed on
 * 2026-08-03 and the count sits *exactly* on it: people stopped ADDING CSS, and
 * nobody removed any. What is measured moves; what is not, drifts.
 *
 * And nothing measured the cause. A search of `scripts/`,
 * `server/tests/conformance/` and this directory found NO guard on MUI and none
 * on primitive adoption — the only hits were test files importing MUI
 * themselves. So the CSS ratchet only forbids the last step of a bad move: you
 * can add a MUI import and a component that reinvents every primitive without
 * writing one line of CSS, and every gate stays green.
 *
 * These two budgets close that. They do not migrate anything — they guarantee
 * the numbers cannot go back up, which is the part that was missing.
 *
 * HOW TO CHANGE THEM. Down, by migrating a file, and then lower the constant in
 * the same commit. Never up. Raising one to make a build pass re-opens the exact
 * hole this file was written to close, and the CSS ratchet has already been
 * silently disarmed once for a whole month (`known-debt.json._WHY_css_lines`).
 *
 * Textual and browser-free on purpose, like `applicationCssLayering.test.ts`
 * beside it: this counts what the SOURCE says, and no rendering assertion could.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Walk up until `src/` is found — the same reason the sibling guard does it:
 *  two test files in this repo once returned two different verdicts for one
 *  tree because they resolved against `cwd`. */
function locateSrc(): string {
  let dir = resolve(process.cwd());
  for (let i = 0; i < 6; i += 1) {
    const candidate = join(dir, "src");
    try {
      if (statSync(candidate).isDirectory() && statSync(join(candidate, "components")).isDirectory()) {
        return candidate;
      }
    } catch {
      /* keep walking */
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error("could not locate ui/admin/src from " + process.cwd());
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "node_modules" || entry === "dist") continue;
      walk(full, out);
    } else if (entry.endsWith(".tsx") || entry.endsWith(".ts")) {
      out.push(full);
    }
  }
  return out;
}

const SRC = locateSrc();
const FILES = walk(SRC).map((path) => ({ path, source: readFileSync(path, "utf8") }));

// ---------------------------------------------------------------------------
// Ratchet 1 — files importing MUI.
//
// Counted per FILE, not per named import: a file is migrated or it is not, and
// counting the 242 named imports would let someone "improve" the number by
// merging two imports without removing anything.
// ---------------------------------------------------------------------------

/** Measured 2026-08-05 (AI-208): ZERO. LOWER THIS as files migrate. Never raise it.
 *
 *  The last non-test importer was `src/theme.ts` — `createTheme` over the token
 *  bundle — and it was deleted with the ten test suites that were its only
 *  consumers. Its four `@fontsource-variable` side-effect imports went with it and
 *  nothing changed on screen: the faces reach the browser through the `@font-face`
 *  block at `shell/application.css:1-4`, over the four .woff2 files checked into
 *  `public/imports/`, three of them preloaded by index.html. Those four packages
 *  were left declared and unimported — a dead dependency. REMOVED on 2026-08-05
 *  (AI-212), and the removal is proved rather than asserted: `vite build` before
 *  and after is byte-identical across all 160 artefact groups in `dist/`.
 *
 *  At zero this budget stops being a ratchet and becomes a wall: any new
 *  `from "@mui/material"` in a non-test file fails here.
 *
 *  AND IT ONLY EVER WALKED `ui/admin/src`, which is the defect worth reading
 *  before trusting any guard in this repo. Reaching zero here on 2026-08-05 read
 *  as "the MUI debt is closed"; it was not. 34 source files and 14 manifests in
 *  `ui/shell`, `ui/cards/*` and `ui/widgets/*` still carried it, invisible
 *  because no guard looked there. The repo-wide wall that closes that is
 *  `ui/shell/src/__tests__/muiIsGone.test.ts` (source + manifests + lockfile).
 *  This one stays: it also guards the hex budget and the primitive library below.
 */
const MUI_FILE_BUDGET = 0;

// ---------------------------------------------------------------------------
// Ratchet 2 — hard-coded colours.
//
// Test fixtures are EXCLUDED and that is not a loophole: an org branding colour
// asserted in `OrgSettingsIdentity.test.tsx` is test DATA, not styling. Counting
// it would make the budget unfixable — you cannot token-ise a value whose whole
// point is to be an arbitrary customer input.
// ---------------------------------------------------------------------------

/** Measured 2026-08-04 over .ts AND .tsx, excluding test fixtures. LOWER THIS. */
const HEX_BUDGET = 14;

const isTest = (path: string) =>
  path.includes("__tests__") || /\.test\.(ts|tsx)$/.test(path);

describe("the visual migration only goes one way", () => {
  it(`no more than ${MUI_FILE_BUDGET} files import @mui/material`, () => {
    // An IMPORT, not the bare string. `analyze-artifacts/RenderSharing.tsx`
    // states "No `@mui/material`" in its own header and was counted as an
    // offender for saying so — a guard that punishes a file for documenting its
    // own compliance teaches people to delete the sentence. Measured: the naive
    // match said 45 files, the honest one says 24. A budget of 45 would have
    // allowed twenty-one regressions while reading as armed.
    const offenders = FILES.filter(
      (f) => !isTest(f.path) && /from\s*"@mui\/material/.test(f.source),
    ).map((f) => f.path.slice(SRC.length + 1).replace(/\\/g, "/"));

    expect(
      offenders.length,
      `MUI is in ${offenders.length} files, budget ${MUI_FILE_BUDGET}.\n` +
        `If this GREW, a screen was written against MUI instead of the 24 primitives ` +
        `in src/components/ui. If it SHRANK, lower MUI_FILE_BUDGET in this file, in ` +
        `the same commit as the migration.\n${offenders.join("\n")}`,
    ).toBeLessThanOrEqual(MUI_FILE_BUDGET);
  });

  it(`no more than ${HEX_BUDGET} hard-coded colours outside test fixtures`, () => {
    const found: string[] = [];
    for (const file of FILES) {
      if (isTest(file.path)) continue;
      for (const line of file.source.split("\n")) {
        // Comments do not style anything. `theme.ts` documents "rose accent
        // #FF99C8" and `themeMode.ts` documents "--page #141416" — counting
        // those is the same defect as counting the MUI guard's own compliance
        // note above, and it teaches people to stop writing the value down.
        if (/^\s*(\/\/|\/\*|\*)/.test(line)) continue;
        for (const match of line.matchAll(/#[0-9A-Fa-f]{6}\b/g)) {
          found.push(`${file.path.slice(SRC.length + 1).replace(/\\/g, "/")}  ${match[0]}`);
        }
      }
    }

    expect(
      found.length,
      `${found.length} hard-coded colours, budget ${HEX_BUDGET}. A colour belongs ` +
        `in ui/tokens/tokens.json and reaches the screen as a theme variable; a ` +
        `literal here cannot follow the light/dark flip and cannot carry an org's ` +
        `branding.\n${found.join("\n")}`,
    ).toBeLessThanOrEqual(HEX_BUDGET);
  });

  it("the primitives are still there to migrate TO", () => {
    // The budgets above can also be satisfied by deleting the library, which
    // would read as progress in both numbers at once.
    const primitives = readdirSync(join(SRC, "components", "ui")).filter((f) =>
      f.endsWith(".tsx"),
    );
    expect(primitives.length).toBeGreaterThanOrEqual(24);
  });
});
