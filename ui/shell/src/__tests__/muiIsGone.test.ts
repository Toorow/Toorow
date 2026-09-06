/**
 * The wall. MUI and emotion are not in this repository, and cannot come back.
 *
 * WHY IT IS HERE AND NOT ONLY IN ui/admin. A ratchet already stood at
 * `ui/admin/src/__tests__/migrationRatchet.test.ts`, and it was armed at zero —
 * so the console was clean and the debt read as closed. It was not: 34 source
 * files in `ui/shell`, `ui/cards/*` and `ui/widgets/*` still imported MUI, and
 * fourteen manifests still declared it, because that ratchet only ever walked
 * `ui/admin/src`. A guard that covers one package makes the packages it does not
 * cover invisible, which is worse than no guard at all.
 *
 * This one walks the WHOLE workspace, and it checks three surfaces, because
 * removing any one alone leaves the door open:
 *   1. source imports   — the thing itself;
 *   2. manifests        — a declared dependency is an invitation to import it;
 *   3. the lockfile     — the packages are still installable while it resolves them.
 *
 * There is no budget constant to lower. Zero is the only passing value.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Walk up to the `ui/` workspace root, identified by its pnpm-workspace.yaml. */
function locateWorkspaceRoot(): string {
  let dir = resolve(process.cwd());
  for (let i = 0; i < 6; i += 1) {
    try {
      statSync(join(dir, "pnpm-workspace.yaml"));
      return dir;
    } catch {
      /* keep walking */
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`could not locate the ui/ workspace root from ${process.cwd()}`);
}

const SKIP_DIRS = new Set(["node_modules", "dist", ".git", ".turbo", "coverage"]);

function walk(dir: string, keep: (name: string) => boolean, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, keep, out);
    else if (keep(entry)) out.push(full);
  }
  return out;
}

const ROOT = locateWorkspaceRoot();
const rel = (path: string) => path.slice(ROOT.length + 1).replace(/\\/g, "/");

/** An IMPORT, not the bare string: a file may say why MUI is gone. */
const BANNED_IMPORT = /(?:from|import)\s*\(?\s*["']@(?:mui|emotion)\//;
const BANNED_DEPS = /^@(?:mui|emotion)\//;

/**
 * Comments are stripped before the match. The sibling ratchet's own header
 * quotes `from "@mui/material"` to explain what it forbids, and flagging it for
 * that is the defect its comment warns about two lines further down: a guard
 * that punishes a file for documenting its own compliance teaches people to
 * delete the sentence.
 */
function withoutComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

describe("MUI and emotion are gone from the widget platform (AD-35)", () => {
  it("no source file imports @mui/* or @emotion/*", () => {
    const sources = walk(ROOT, (name) => /\.(ts|tsx|js|jsx|mjs)$/.test(name));
    expect(sources.length).toBeGreaterThan(100); // the walk actually saw the tree

    const offenders = sources
      .map((path) => ({ path, source: withoutComments(readFileSync(path, "utf8")) }))
      .filter(({ source }) => BANNED_IMPORT.test(source))
      .map(({ path }) => rel(path));

    expect(
      offenders,
      `${offenders.length} file(s) import MUI or emotion. The replacements all live in ` +
        `@toorow/shell: useTheme/useColorScheme/ThemeProvider, the colour helpers ` +
        `(alpha, darken, lighten, getContrastRatio, decomposeColor) and the DOM ` +
        `primitives (Box, Typography, Button, Chip, Alert, ToggleButton, Table, Dialog).`,
    ).toEqual([]);
  });

  it("no package.json declares @mui/* or @emotion/*", () => {
    const manifests = walk(ROOT, (name) => name === "package.json");
    expect(manifests.length).toBeGreaterThan(10);

    const offenders: string[] = [];
    for (const path of manifests) {
      const data = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
      for (const section of ["dependencies", "devDependencies", "peerDependencies"]) {
        const block = data[section] as Record<string, string> | undefined;
        for (const dep of Object.keys(block ?? {})) {
          if (BANNED_DEPS.test(dep)) offenders.push(`${rel(path)} -> ${section}.${dep}`);
        }
      }
    }

    expect(
      offenders,
      "A declared dependency is an invitation to import it. Remove these and re-run " +
        "`pnpm install` so the lockfile follows.",
    ).toEqual([]);
  });

  it("the lockfile does not resolve @mui/* or @emotion/*", () => {
    // The manifests can be clean while the lockfile still installs the packages —
    // and then the next `pnpm install` puts them back in node_modules, where an
    // editor's auto-import will happily suggest `Box` again.
    const lock = readFileSync(join(ROOT, "pnpm-lock.yaml"), "utf8");
    const hits = lock.split("\n").filter((line) => /['"]?@(mui|emotion)\//.test(line));
    expect(
      hits.length,
      `pnpm-lock.yaml still resolves ${hits.length} MUI/emotion entries. Run ` +
        "`pnpm install` in ui/ after clearing the manifests.",
    ).toBe(0);
  });
});
