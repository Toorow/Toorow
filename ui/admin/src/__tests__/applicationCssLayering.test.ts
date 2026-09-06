/**
 * The palette must never go back inside `@layer legacy`.
 *
 * It did, and dark mode was inert across every legacy surface for as long as it
 * lasted. The mechanism is not obvious, which is why it needs a guard rather
 * than a comment: `shell/application.css` defined the light `:root` defaults
 * UNLAYERED and the `[data-color-scheme="dark"]` overrides INSIDE the layer. An
 * unlayered rule beats a layered one whatever the specificity or source order,
 * so eleven custom properties never flipped — while the chrome, which reads
 * `theme.css`, went dark around them. Grey-on-white action rows, and a canvas
 * rendering its stock light palette on a black page.
 *
 * It read as a regression of Story 44.4's React Flow theming fix. That fix was
 * intact. Someone could have spent a day re-fixing what was not broken.
 *
 * Measured after the repair, in a real browser on the running sandbox:
 * 11 of 11 palette variables flip between `data-color-scheme` light and dark.
 *
 * This test is deliberately textual and browser-free: jsdom does not implement
 * cascade layers, so a rendering assertion here would pass on a broken file.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/** Walk up from the working directory until the sheet is found.
 *
 *  Not `import.meta.url` (this config does not give it a `file:` scheme) and
 *  not `?raw` (vitest stubs CSS imports to an empty string, so every assertion
 *  would pass against nothing). Not a plain cwd-relative path either: two test
 *  files in this repo returned two different verdicts for the same tree because
 *  they resolved against `cwd`, and this guard exists precisely to survive. */
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

const CSS = readFileSync(locate(join("src", "shell", "application.css")), "utf8");

/** The variables every one of the ~60 legacy sheets reads. */
const PALETTE = [
  "--ink", "--muted", "--surface", "--surface-2", "--page",
  "--line", "--track", "--info", "--success", "--warning", "--error",
];

/** Index of the `@layer legacy {` opening, or -1. */
const layerStart = CSS.indexOf("@layer legacy {");

/** The order statement as a RULE: at the start of a line, never ` * `-prefixed
 *  inside the doc block that also quotes it. Not anchored at end-of-line: the
 *  statement carries a trailing comment, because this sheet is under a line
 *  ratchet and the explanation lives here rather than costing it four lines. */
const ORDER = /^@layer theme, base, legacy, components, utilities;/m;
const orderStatement = ORDER.exec(CSS)?.index ?? -1;

describe("application.css cascade layering", () => {
  it("declares the legacy layer exactly once", () => {
    expect(CSS.match(/@layer legacy \{/g)).toHaveLength(1);
  });

  it("keeps the scheme overrides OUTSIDE the layer, with the light defaults", () => {
    // If the dark block sits inside the layer while `:root` sits outside it,
    // the unlayered light default wins and dark mode silently stops working.
    const darkBlock = CSS.indexOf('[data-color-scheme="dark"]');
    expect(darkBlock).toBeGreaterThan(-1);
    expect(layerStart).toBeGreaterThan(-1);
    expect(darkBlock).toBeLessThan(layerStart);
  });

  it("declares every palette variable in both schemes, outside the layer", () => {
    const beforeLayer = CSS.slice(0, layerStart);
    const dark = beforeLayer.slice(beforeLayer.indexOf('[data-color-scheme="dark"]'));
    for (const name of PALETTE) {
      expect(beforeLayer, `${name} must have a light default outside the layer`)
        .toContain(`${name}:`);
      expect(dark, `${name} must be overridden for the dark scheme`)
        .toContain(`${name}:`);
    }
  });

  it("has teeth: the same check fails on the shape that broke dark mode", () => {
    // The mutation is applied to a COPY, never to the sheet on disk: proving a
    // guard by editing the file it guards is how a repo ends up with a red tree
    // and a session that has to remember to undo something.
    const broken = CSS.replace("@layer legacy {", "")
      .replace('[data-color-scheme="dark"]', '@layer legacy {\n[data-color-scheme="dark"]');
    const brokenLayer = broken.indexOf("@layer legacy {");
    const brokenDark = broken.indexOf('[data-color-scheme="dark"]');
    expect(brokenDark).toBeGreaterThan(brokenLayer); // the defect, reproduced
    expect(brokenDark).not.toBeLessThan(brokenLayer); // so the real assertion would fail
  });

  it("declares the layer ORDER before it opens the layer", () => {
    // A layer ranks where its name is FIRST seen, and this sheet is emitted
    // first: `App.tsx` reaches it through `pages/CreateProject` (l. 41) before
    // it imports `styles/theme.css` (l. 49). So `@layer legacy {` alone
    // registered legacy as layer #1 and Tailwind's `base` landed AFTER it.
    // Measured in the built bundle, `dist/assets/App-*.css`, before the fix:
    // legacy@1658, properties@76197, theme@78292, base@82396, components@86044,
    // utilities@86062. After: theme@0, base@4104, legacy@7752, components@34704,
    // utilities@34722.
    //
    // Preflight therefore beat every rule in the sheet that targets a form
    // element -- `*{border:0 solid;padding:0}` took the border and the padding,
    // `button{background-color:#0000;border-radius:0}` took the rose fill and
    // the pill. `.primary-button` rendered as bare text on the Create-project
    // door, and with it .secondary-button, .quiet-button, .icon-button and
    // .selector on every screen still reading this sheet.
    //
    // Restating the statement in `theme.css` alone cannot fix it: whichever
    // sheet the bundler emits first is the one that sets the order, so BOTH
    // must carry it.
    expect(orderStatement, "the order must be restated in this sheet").toBeGreaterThan(-1);
    expect(orderStatement).toBeLessThan(layerStart);
  });

  it("has teeth: commented out, the order statement stops counting", () => {
    // The mutation goes to a COPY, as above. It also proves the matcher reads a
    // RULE and not the same text quoted in the doc block further down — which is
    // the shape that made the first version of this guard pass on a broken file.
    const broken = CSS.replace(ORDER, (found) => ` * ${found}`);
    expect(ORDER.exec(broken)?.index ?? -1).toBe(-1);
  });

  it("still layers the RULES, so utilities keep winning over legacy CSS", () => {
    // The layer earns its keep on `button, input { font: inherit }`, which was
    // overriding the type of every component in the library.
    const inLayer = CSS.slice(layerStart);
    expect(inLayer).toMatch(/button[^{]*\{[^}]*font:\s*inherit/);
  });
});
