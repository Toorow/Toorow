/**
 * Story 76-8 — `src/format.ts` is a DOOR, never a second formatter.
 *
 * `console-presentation.md` §2: a presentation decision is written once and
 * consumed from one module; a second module answering the same question is the
 * defect, whatever it answers. This widget cannot name `@toorow/card-shell` as a
 * dependency without a `pnpm-lock.yaml` write that `--frozen-lockfile` would
 * refuse at deploy time (the reasoning and the command are in `format.ts`), so
 * it reaches the pinned module by path. That is only acceptable while the file
 * stays a pure re-export — which is what this test measures, rather than trusting
 * a comment to hold.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { FORMATTER_LOCALE, formatPercent, formatValue, verdictTone } from "../format";

const SOURCE = readFileSync(join(__dirname, "..", "format.ts"), "utf8");

/** The file with its licence-style header comment removed. */
const CODE = SOURCE.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

describe("the widget's format door", () => {
  it("declares nothing of its own — only `export … from`", () => {
    const statements = CODE.split(";")
      .map((s) => s.trim())
      .filter(Boolean);
    expect(statements.length).toBeGreaterThan(0);
    for (const statement of statements) {
      // `export { … } from` and `export type { … } from`, and nothing else.
      expect(/^export (type )?\{/.test(statement), statement.slice(0, 60)).toBe(true);
      expect(statement).toContain("} from ");
    }
    // No rule, no locale, no branch may live here.
    expect(CODE).not.toMatch(/toLocale|Intl\.|function |const |if \(/);
  });

  it("points at the ONE pinned module, and at nothing else", () => {
    const targets = [...CODE.matchAll(/from "([^"]+)"/g)].map((m) => m[1]);
    expect(targets.length).toBeGreaterThan(0);
    for (const target of targets) {
      expect(target.startsWith("../../../cards/shell/src/")).toBe(true);
    }
    expect(targets).toContain("../../../cards/shell/src/viz/theme/formatters");
  });

  it("serves the same locale and the same output as the cards", () => {
    expect(FORMATTER_LOCALE).toBe("en-US");
    expect(formatValue(40467)).toBe("40,467");
    expect(formatPercent(-8.6, { signed: true })).toBe("-8.6%");
  });

  it("serves the SAME verdict decision as the cards, not a copy of it", () => {
    // A widget that judged with a rule of its own would be the second authority
    // `console-presentation.md` §2 forbids — and its tiles painted three deltas
    // red with nothing saying what red meant until story 76-8 round 2.
    expect(verdictTone(-8.6, "up_good")).toBe("unfavourable");
    expect(verdictTone(-8.6, "down_good")).toBe("favourable");
  });
});
