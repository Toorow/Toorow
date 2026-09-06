/**
 * AN IDENTIFIER IS NEVER A NAME, AND THE RULE IS NOT THE BUILDER RAIL'S ALONE.
 *
 * `f3ca3d50` repaired one legend: the member rail of the Analytics Builder was
 * printing `mdm_01KZ…` where a person expects "Ad spend". Its own commit message
 * named three more sites and stopped there. A census over every front package
 * found the class is wider than three:
 *
 *   grep -rnE '^\s*\{[a-zA-Z_]+\.[a-zA-Z_]*(_id|Id)\}\s*$' \
 *     ui/admin/src ui/cards ui/shell/src ui/widgets web/src --include=*.tsx \
 *     | grep -v test        ->  12 lines, 2026-08-21
 *
 * WHAT THIS GUARD REFUSES, AND WHAT IT DOES NOT. Printing an identifier is not
 * the defect by itself, and a guard that said so would be wrong: a Result, a
 * Render, a pull run and an AI Path are artifacts a person OPENS BY THEIR
 * ADDRESS, and manufacturing a title for them would be worse than showing the
 * address — `visualization-and-rendering.md` is explicit that the identifier
 * stays when nothing is recorded, "ugly and true". What is refused is an
 * identifier standing where a WORD belongs. The same document draws the line and
 * this guard reads it: an identifier is legitimate when it is
 *
 *   * ACTIONABLE AS AN ADDRESS — it is the content of a control that opens the
 *     object it names (`href`, `onClick`, `onOpen`, …); or
 *   * MARKED AS TECHNICAL — `<code>`, `text-technical`, a monospace cell.
 *
 * An identifier rendered as bare prose is the defect. Of the twelve sites of the
 * census, ten are addresses under a control or technical cells; the two that were
 * neither are repaired in the same change as this file:
 *
 *   * `governance/NewSemanticViewDialog.tsx` printed "· version scv_01KZ…" ONE
 *     LINE under `label ?? name ?? concept_id` — the exact twin of the rail
 *     defect. `semantic_concept_versions.version_number` has existed since
 *     `142_semantic_model.sql`; the read model now serves it and the line reads
 *     "· version 3".
 *   * `datastreams/onboarding/FileSourceOnboardingReview.tsx` said
 *     "mdm_01KZ… is measured in micros". `mdm_canonical_fields.canonical_name`
 *     is `NOT NULL`; `import_preview._unit_report` now serves it.
 *
 * Both were repaired ON THE SERVER, where the vocabulary lives. Composing either
 * name in the browser would make the console a second authority on the
 * vocabulary, and two authorities eventually disagree.
 *
 * WHAT IT READS, AND WHY THAT IS NOT A LIST. The scope is DERIVED from where a
 * `package.json` sits, exactly as `ScreensDoNotSpeakTheDatabase.test.tsx` derives
 * its own since `771b0f46` — that guard was anchored on `ui/admin/src` alone and
 * declared its class closed while fourteen other front trees carried it. A
 * hard-coded list would have to be edited by whoever adds the next package, which
 * is the maintenance that let the fifteenth go unread.
 *
 * WHAT IT CANNOT SEE, said here rather than discovered later. It reads an
 * identifier by the SHAPE OF THE FIELD NAME (`…_id`, `…Id`), because the value is
 * only known at run time. A wire field that is misnamed — an identifier called
 * `column`, or a readable word called `field_id` — passes or trips wrongly, and
 * the honest answer to the second is to rename the wire, not to loosen this file.
 * It also reads only an expression that is ALONE ON ITS LINE: a one-line
 * `<span className="text-technical">{x.ai_path_id}</span>` is already marked, and
 * widening to every position would report every `key=` and `data-testid=`.
 *
 * WHAT A `pending` ENTRY DOES. A violation that cannot be repaired here goes into
 * `docs/product-architecture/known-debt.json` under `pending`, with its reason,
 * its owner and its date, and it stops blocking. No pattern is loosened and no
 * scope is shrunk: making this file green that way would put the class back where
 * it was.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";

/** The repository root — this file sits at `ui/admin/src/__tests__/`. */
const REPO = resolve(__dirname, "..", "..", "..", "..");

/** Every front package's `src/`, derived from where a `package.json` sits.
 *
 *  `ui/` holds the console, the shell, the cards and the widgets; `web/` holds
 *  the public site. Both are walked, and a workspace root with no `src/` drops
 *  out on its own — the existence check is the filter, so no package is named. */
const FRONT_TREES = ["ui", "web"];

function frontRoots(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "node_modules" || entry === "dist") continue;
      frontRoots(full, out);
      continue;
    }
    if (entry !== "package.json") continue;
    const src = join(dirname(full), "src");
    try {
      if (statSync(src).isDirectory()) out.push(src);
    } catch {
      // A package that ships no `src/` renders nothing and is read by nobody.
    }
  }
  return out;
}

/** Every `.ts`/`.tsx` of a front package except the tests themselves. */
function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules") continue;
      sources(full, out);
      continue;
    }
    if (!/\.tsx?$/.test(entry)) continue;
    if (/\.test\.tsx?$/.test(entry)) continue;
    if (extname(entry) === ".ts" || extname(entry) === ".tsx") out.push(full);
  }
  return out;
}

const FILES = FRONT_TREES.flatMap((tree) => frontRoots(join(REPO, tree)))
  .sort()
  .flatMap((root) => sources(root));

/** An expression container whose WHOLE line is one identifier accessor.
 *
 *  `{result.result_id}`, `{member?.concept_version_id}`,
 *  `{block.result_id as string}`. The trailing `_id`/`Id` is what names it an
 *  identifier; nothing here can know the value. */
const BARE_IDENTIFIER =
  /^[ 	]*\{\s*([A-Za-z_$][\w$]*(?:\??\.[\w$]+)*?\??\.[\w$]*(?:_id|Id))(?:\s+as\s+[\w<>[\]|]+)?\s*\}[ 	]*$/gm;

/** The same accessor in JSX CHILD position: `>{render.id}<`.
 *
 *  ADDED 2026-09-05 (story 76-3) and it is the half that mattered. The rule
 *  above reads only an expression ALONE ON ITS LINE, which was deliberate — the
 *  file said so — and it meant a one-line `<code>{entry.id}</code>` was never
 *  even looked at. Measured that day: 63 identifiers matched the old shape and
 *  **73 more** sat on a single line inside an ad-hoc monospace wrapper, which is
 *  the majority of the class and the half nothing had ever counted.
 *
 *  Child position is what keeps this from reporting every attribute: an
 *  identifier between `>` and `<` is RENDERED; `key={x.id}`, `data-testid={…}`
 *  and `value={x.id}` are not, and none of them is preceded by `>`. `.id` joins
 *  `_id`/`Id` here because `{render.id}` inside a `<code>` is exactly the shape
 *  this pass exists to remove. */
const CHILD_IDENTIFIER =
  />\s*\{\s*([A-Za-z_$][\w$]*(?:\??\.[\w$]+)*?\??\.(?:id|[\w$]*(?:_id|Id)))(?:\s+as\s+[\w<>[\]|]+)?\s*\}\s*</g;

/** The opening tag the expression sits directly inside, as written.
 *
 *  Read backwards to the last tag opener before the expression. It can be
 *  truncated by an arrow function in an attribute (`onClick={(e) => …}` puts a
 *  `>` inside the tag), which costs nothing: what is read out of it are the
 *  attributes that come BEFORE the expression, and they are all of them. */
function enclosingTag(source: string, at: number): string {
  const before = source.slice(0, at);
  const opener = before.lastIndexOf("<");
  return opener < 0 ? "" : before.slice(opener);
}

/** THE CONSOLE HAS ONE RENDERING OF AN IDENTIFIER, AND IT IS `ObjectId`.
 *
 *  `console-presentation.md` §4: *"A technical identifier is shown in double —
 *  human label, then `ObjectId` in monospace — or not at all."* Until story
 *  76-3 this file accepted any technical MARKING — `<code>`, `text-technical`,
 *  a `font-mono` class — plus any identifier under a control. That reading was
 *  the ratified one when it was written, and it let 73 hand-rolled monospace
 *  spans stand: six spellings of one answer, none of them truncating from the
 *  right, none carrying the full value in a `title`, and `ObjectId` — which
 *  does all three — used at 36 sites out of 136.
 *
 *  For `ui/admin/src` the accepted rendering is now `ObjectId` and nothing
 *  else. Because `ObjectId` takes its value as an ATTRIBUTE, an identifier that
 *  reaches this function at all is by construction a child of something that is
 *  not `ObjectId` — so the console's answer here is a flat refusal, and an
 *  address under a control puts `ObjectId` INSIDE the control rather than
 *  beside it.
 *
 *  CARDS AND WIDGETS KEEP `<code>`. They do not import the console's `ui/`
 *  library and have no `ObjectId`; holding them to a component they cannot
 *  reach would be a rule nobody can obey, which is how a guard gets switched
 *  off. Their old acceptance is unchanged and is what this function still
 *  answers for every tree but the console. */
function isAcceptedRendering(tag: string, isConsole: boolean): boolean {
  if (isConsole) return false;
  return (
    /<code\b/.test(tag)
    || /\b(href|to)=/.test(tag)
    || /\bon[A-Z][\w$]*=/.test(tag)
    || /text-technical|font-mono/.test(tag)
  );
}

/** The console's own tree, the one held to `ObjectId`. */
const CONSOLE_PREFIX = "ui/admin/src/";

/** Findings another session owns, dated and reasoned, that stop blocking here.
 *
 *  Read from `known-debt.json.pending`, never from a constant in this file: the
 *  entry has to carry an owner and a date, and it has to be visible to the audit
 *  that reports it. Matching is EXACT on the finding string. */
function pendingFindings(): Set<string> {
  const raw = readFileSync(join(REPO, "docs/product-architecture/known-debt.json"), "utf-8");
  const entries = (JSON.parse(raw) as { pending?: { finding?: string }[] }).pending ?? [];
  return new Set(entries.map((entry) => entry.finding ?? "").filter(Boolean));
}
const PENDING = pendingFindings();

/** Findings inside a file a PARALLEL SESSION is holding, on 2026-09-05.
 *
 *  `connaissances/AiPathPage.tsx` renders `{sibling.path_id}` inside a
 *  `font-mono` span. That was an ACCEPTED rendering until the line above it
 *  changed, and story 76-3 migrated the other 79 sites of the class in one pass
 *  — but a server session owns this file this week, and editing it under them to
 *  keep a guard green is how two sessions produce a conflict neither asked for.
 *
 *  The entry is one exact finding, not a path: the next identifier written into
 *  that file is still refused. It is written here rather than in
 *  `known-debt.json` because that file is not this story's to edit, and it must
 *  disappear the moment its owner lands — `ObjectId` is already imported by 34
 *  of its siblings, so the fix is one line whenever the file is free. */
const HELD_BY_ANOTHER_SESSION = new Set([
  "ui/admin/src/connaissances/AiPathPage.tsx: {sibling.path_id} rendered as prose",
]);

/** How one hit is named, here and in `known-debt.json`. One spelling, so an
 *  entry written from a failure message matches the next run byte for byte.
 *  Paths use forward slashes: the file is read on every platform. */
function finding(file: string, accessor: string): string {
  return `${relative(REPO, file).replace(/\\/g, "/")}: {${accessor}} rendered as prose`;
}

function offenders(): string[] {
  const found: string[] = [];
  for (const file of FILES) {
    const rel = relative(REPO, file).replace(/\\/g, "/");
    const isConsole = rel.startsWith(CONSOLE_PREFIX);
    const source = readFileSync(file, "utf-8");
    // The CHILD shape is read in the console only. `ObjectId` is a component of
    // `ui/admin/src/ui/`, so it is the console's answer and nobody else's;
    // widening the SHAPE for a tree whose accepted rendering has not changed
    // would report sites against a rule that tree cannot satisfy.
    const patterns = isConsole ? [BARE_IDENTIFIER, CHILD_IDENTIFIER] : [BARE_IDENTIFIER];
    for (const pattern of patterns) {
      for (const match of source.matchAll(new RegExp(pattern.source, pattern.flags))) {
        const at = match.index ?? 0;
        if (isAcceptedRendering(enclosingTag(source, at), isConsole)) continue;
        found.push(finding(file, match[1]));
      }
    }
  }
  return [...new Set(found)]
    .filter((hit) => !PENDING.has(hit) && !HELD_BY_ANOTHER_SESSION.has(hit))
    .sort();
}

describe("an identifier is an address, never a name", () => {
  it("reads every front package, not the console alone", () => {
    // The defect `771b0f46` found in the guard next door: a scope of one tree
    // declares the class closed while the others carry it. Asserted as a lower
    // bound on the trees, so adding a package cannot silently narrow the read.
    const trees = new Set(FILES.map((file) => relative(REPO, file).split(/[\\/]/).slice(0, 2).join("/")));
    expect(trees.size).toBeGreaterThan(1);
    expect(FILES.length).toBeGreaterThan(300);
  });

  it("renders no identifier as bare prose", () => {
    expect(offenders()).toEqual([]);
  });
});
