/**
 * `prefers-reduced-motion: reduce` reaches every console route — measured on the
 * IMPORT GRAPH, never on a file that exists.
 *
 * THE CLAUSE. `visualization-and-rendering.md`: "No transition outstays the
 * gesture, and `prefers-reduced-motion: reduce` removes all of it while every
 * action stays available", and, in the list under it, *"motion continues under
 * `prefers-reduced-motion: reduce`"*.
 *
 * WHAT WAS MEASURED, 2026-08-31. The kill-switch existed — in
 * `shell/application.css`, a sheet `App.tsx` does not import. `App.tsx` imports
 * `styles/theme.css` and `styles/console.css`, and nothing else; the built app
 * chunk carried exactly one `prefers-reduced-motion` rule,
 * `.motion-reduce:animate-none`, which only fires on elements that opted into
 * it, while the `*{transition-duration:.001ms!important}` block sat in a chunk
 * the Analyze route never loads. Meanwhile `components/ui/dialog.tsx`
 * (`animate-in`, `zoom-in-95`) and `components/ui/progress.tsx`
 * (`transition-transform`) animate with nothing above them.
 *
 * WHY THIS TEST IS WRITTEN AGAINST THE GRAPH. A test asserting that
 * `application.css` contains the block would have been green for the whole life
 * of the defect: the rule was there, correct, and unloaded. "A guard must not
 * measure a file nobody loads" is the exact shape of this bug, so the set of
 * sheets is COMPUTED — from `App.tsx`'s own side-effect imports, followed
 * through each sheet's `@import` — and the assertion is made against that union
 * and nothing else. Remove the `import "./styles/console.css"` line and this
 * file goes red.
 *
 * Textual and browser-free on purpose, and for the reason its neighbour
 * `applicationCssLayering.test.ts` gives: jsdom implements neither cascade
 * layers nor `@media (prefers-reduced-motion)`, so a rendering assertion here
 * would pass against a broken sheet.
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Walk up from the working directory until the path is found.
 *  Same reason as `applicationCssLayering.test.ts`: two test files in this repo
 *  once returned two verdicts for the same tree by resolving against `cwd`. */
function locate(relative: string): string {
  let dir = resolve(process.cwd());
  for (let i = 0; i < 6; i += 1) {
    const candidate = join(dir, relative);
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`cannot locate ${relative} from ${process.cwd()}`);
}

const SRC = dirname(locate(join("src", "App.tsx")));

/** Strip `//` and block comments so a COMMENTED-OUT import is not "loaded".
 *
 *  Not a nicety: the first draft of this test read the raw text, and commenting
 *  the `import "./styles/console.css"` line out — the exact mutation that must
 *  turn it red — left it green. A guard that counts a disabled import as a
 *  loaded sheet is measuring the file again, one level up.
 *
 *  `[^\n\r]*` and not `.*$`: JavaScript's `.` does not match `\r`, so on a file
 *  checked out with CRLF endings the anchored form matched nothing at all and
 *  the strip was a no-op — green, on Windows, for the one mutation this file
 *  exists to catch. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .map((line) => line.replace(/(^|[^:])\/\/[^\n\r]*/, "$1"))
    .join("\n");
}

/** Side-effect stylesheet imports of one module: `import "./x.css";` */
function stylesheetImports(source: string): string[] {
  return [...stripComments(source).matchAll(/import\s+"([^"]+\.css)"\s*;?/g)].map(
    (match) => match[1],
  );
}

/** `@import "./x.css"` inside a sheet, local files only.
 *  A bare specifier (`tailwindcss`, `tw-animate-css`) is a package, not a sheet
 *  of ours; following it would make this test depend on a vendored build. */
function cssImports(source: string): string[] {
  return [...source.matchAll(/@import\s+"([^"]+)"/g)]
    .map((match) => match[1])
    .filter((specifier) => specifier.startsWith("."));
}

/**
 * Every stylesheet a console route actually loads, as a map path -> text.
 *
 * The root is `App.tsx` because that is what every console route is mounted
 * inside (`main.tsx` renders it for every path but the two `import.meta.env.DEV`
 * debug routes, which mount their own component precisely so the legacy sheets
 * are NOT loaded).
 */
function loadedStylesheets(): Map<string, string> {
  const sheets = new Map<string, string>();
  const queue: string[] = stylesheetImports(readFileSync(join(SRC, "App.tsx"), "utf8")).map(
    (specifier) => resolve(SRC, specifier),
  );
  while (queue.length) {
    const path = queue.shift()!;
    if (sheets.has(path) || !existsSync(path)) continue;
    const text = readFileSync(path, "utf8");
    sheets.set(path, text);
    for (const specifier of cssImports(text)) queue.push(resolve(dirname(path), specifier));
  }
  return sheets;
}

/** The `@media (prefers-reduced-motion: reduce) { ... }` bodies of one sheet. */
function reducedMotionBlocks(css: string): string[] {
  const blocks: string[] = [];
  const opener = /@media[^{]*prefers-reduced-motion:\s*reduce[^{]*\{/g;
  let match: RegExpExecArray | null;
  while ((match = opener.exec(css)) !== null) {
    let depth = 1;
    let index = match.index + match[0].length;
    const start = index;
    while (index < css.length && depth > 0) {
      if (css[index] === "{") depth += 1;
      else if (css[index] === "}") depth -= 1;
      index += 1;
    }
    blocks.push(css.slice(start, index - 1));
  }
  return blocks;
}

describe("the reduced-motion kill-switch is in a sheet every route loads", () => {
  it("reads a non-empty stylesheet set from App.tsx's own imports", () => {
    // The instrument before the measurement. A resolver that found nothing
    // would make every assertion below vacuous, which is how the original
    // defect survived.
    const sheets = loadedStylesheets();
    expect(sheets.size).toBeGreaterThan(0);
    const names = [...sheets.keys()].map((path) => path.replace(/\\/g, "/"));
    expect(names.some((name) => name.endsWith("/styles/theme.css"))).toBe(true);
  });

  it("neutralises motion for EVERY element, not only those that opted in", () => {
    const sheets = loadedStylesheets();
    const blocks = [...sheets.values()].flatMap(reducedMotionBlocks);
    expect(
      blocks.length,
      "no `@media (prefers-reduced-motion: reduce)` rule is reachable from App.tsx — " +
        "the console loads " +
        [...sheets.keys()].map((path) => path.replace(/\\/g, "/")).join(", "),
    ).toBeGreaterThan(0);

    // A universal selector is what makes this a kill-switch rather than a
    // per-component opt-in. `.motion-reduce:animate-none` is the opt-in, and it
    // is what the built chunk carried while the switch was unloaded.
    const universal = blocks.filter((body) => /(^|[\s,{])\*(\s|,|:|\{)/.test(body));
    expect(
      universal.length,
      "the reachable reduced-motion rules all target specific selectors; a " +
        "component added tomorrow would animate with nothing above it",
    ).toBeGreaterThan(0);

    const body = universal.join("\n");
    expect(body).toMatch(/animation-duration:\s*[^;]*!important/);
    expect(body).toMatch(/transition-duration:\s*[^;]*!important/);
    expect(body).toMatch(/animation-iteration-count:\s*1\s*!important/);
  });

  it("shortens the motion instead of removing it, so no end state is lost", () => {
    // "removes all of it WHILE EVERY ACTION STAYS AVAILABLE". `animation: none`
    // on an `animate-in` element would strip the animation that carries it to
    // its visible end state -- a dialog that never appears is not reduced
    // motion, it is a broken dialog.
    const body = [...loadedStylesheets().values()].flatMap(reducedMotionBlocks).join("\n");
    expect(body).not.toMatch(/animation:\s*none/);
    expect(body).not.toMatch(/display:\s*none/);
    expect(body).not.toMatch(/visibility:\s*hidden/);
  });

  it("covers the library primitives, which do not opt in one by one", () => {
    // The measured instances: `dialog.tsx` animates in and zooms, `progress.tsx`
    // transitions its transform, and neither carries a `motion-reduce:` variant.
    // The census is read from the directory, so a primitive that starts
    // animating tomorrow is inside this claim without anyone editing it.
    const primitives = join(SRC, "components", "ui");
    const animated = readdirSync(primitives)
      .filter((name) => name.endsWith(".tsx"))
      .filter((name) =>
        /\banimate-(in|out|pulse|spin|bounce)\b|\btransition-|\bduration-\d/.test(
          readFileSync(join(primitives, name), "utf8"),
        ),
      );
    expect(
      animated.length,
      "no primitive under components/ui declares motion any more -- this guard " +
        "has lost its subject; check that the census still matches the library",
    ).toBeGreaterThan(0);
    expect(animated).toContain("dialog.tsx");
    expect(animated).toContain("progress.tsx");

    // None of them opts in, and none of them has to: the switch above is
    // universal, which is the whole reason it is written that way.
    const blocks = [...loadedStylesheets().values()].flatMap(reducedMotionBlocks);
    expect(blocks.some((body) => /(^|[\s,{])\*(\s|,|:|\{)/.test(body))).toBe(true);
  });
});
