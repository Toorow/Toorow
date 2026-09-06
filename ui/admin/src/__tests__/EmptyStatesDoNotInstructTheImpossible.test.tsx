/**
 * An empty state may say what is absent. It may not instruct a path that does
 * not exist — and, once the path exists, it may not keep saying it does not.
 *
 * `analyze/reports` told a person to "Save a question from Explore, or create
 * one from a connector seed below". `analyze/notebooks` told them to "Compose
 * one from saved Reports or Query Spec versions". Neither path existed anywhere
 * in `ui/admin/src`: `createReport`, `createReportVersion`, `createNotebook`,
 * `createNotebookVersion` and `setNotebookSchedule` were all built AND mounted
 * server-side, with ZERO call sites in the console.
 *
 * Telling someone to press a button that was never built is worse than an empty
 * screen: they look for it, fail, and conclude they misread the product.
 *
 * WHAT THIS FILE GOT WRONG, and why it is now written the other way round
 * (2026-08-05, AI-212). It pinned the *literal copy* of the unreachable era —
 * "not yet reachable from this console" on BOTH screens — and carried a tripwire
 * whose whole job was to fail the day a real creator landed, so the copy would be
 * rewritten with it. AI-198 landed that creator for Reports on 2026-08-04 and
 * correctly rewrote the copy. The tripwire did not fire: it matched
 * `createReport\s*\(` and the function shipped as `createReportFromResult(`. So
 * the guard stayed green while the four assertions beside it went stale and red,
 * and the red was read as background noise for a day.
 *
 * So the assertions below no longer name an era. Each screen's copy is checked
 * AGAINST WHETHER ITS CREATOR IS CALLED, which is the property that actually has
 * to hold. Reports has a caller, so it must instruct; Notebooks has none, so it
 * must say so. When Notebooks gains one, its case fails and its copy gets
 * rewritten — this time whatever the function is named.
 *
 * The guard is written over the SOURCE, because what has to hold is a property
 * of the copy, not the behaviour of one render.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";

/** Source with comments stripped.
 *
 *  Both files EXPLAIN their old copy in a comment, quoting it verbatim so the
 *  next reader knows why it changed. A matcher that reads prose as code reports
 *  that explanation as the defect -- the same trap the `/sample` route guard hit,
 *  and stripping comments first was the fix both times. */
function code(relative: string): string {
  const raw = readFileSync(resolve(__dirname, relative), "utf-8");
  // `.` does not cross a line in JS, so a line comment needs no explicit
  // newline class — and the class is what kept breaking through the shells.
  return raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*/g, "");
}

const REPORTS = code("../analyze-artifacts/Reports.tsx");
const NOTEBOOKS = code("../analyze-artifacts/Notebooks.tsx");
const CONSOLE_SRC = resolve(__dirname, "..");

/** The unreachable-era sentence. Present iff the screen has no create path. */
const UNREACHABLE = "not yet reachable from this console";

/**
 * Every console file that CALLS a creator, whatever suffix it was given.
 *
 * `[A-Za-z]*` before the parenthesis is the whole repair: the previous version
 * anchored on the bare name and `createReportFromResult(` walked straight past
 * it. A guard that can be defeated by renaming the thing it guards is not a
 * guard.
 *
 * A DECLARATION is not a call. `saveAsReport.ts` writes
 * `export async function createReportFromResult(` and offers nothing by itself;
 * counting it would make the module its own caller and the guard would report a
 * path that exists on disk but is reachable from no screen — the exact confusion
 * this file was written to prevent.
 */
function callersOf(name: "createReport" | "createNotebook"): string[] {
  const call = new RegExp(String.raw`\b${name}[A-Za-z]*\s*\(`);
  const declaration = new RegExp(String.raw`\bfunction\s+${name}[A-Za-z]*\s*\(`, "g");
  const found: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = resolve(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry !== "__tests__" && entry !== "node_modules") walk(full);
        continue;
      }
      // `client.ts` DECLARES the transport; declaring is not offering.
      if (!/\.tsx?$/.test(entry) || entry === "client.ts") continue;
      const body = readFileSync(full, "utf-8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/\/\/.*/g, "")
        .replace(declaration, "");
      if (call.test(body)) found.push(entry);
    }
  };
  walk(CONSOLE_SRC);
  return found.sort();
}

const REPORT_CALLERS = callersOf("createReport");
const NOTEBOOK_CALLERS = callersOf("createNotebook");

it("no longer tells a person to save a question from Explore in the old words", () => {
  // The wording that named a button nobody built. The path is real now, but it
  // is reached from a Result, not from Explore itself.
  expect(REPORTS).not.toContain("Save a question from Explore");
});

it("no longer tells a person to compose a Notebook from saved Reports", () => {
  expect(NOTEBOOKS).not.toContain("Compose one from saved Reports");
});

it("Reports has a create path, so its empty state instructs it", () => {
  // Proven, not assumed: `analyze/saveAsReport.ts` is called by ResultWorkbench.
  expect(REPORT_CALLERS).toContain("ResultWorkbench.tsx");
  expect(REPORTS).toContain("Ask a question in Explore, then save it from its Result");
  // And it must NOT still claim the console cannot do it — an empty state that
  // under-promises is the same defect in the other direction.
  expect(REPORTS).not.toContain(UNREACHABLE);
});

it("Notebooks has a create path, so its empty state offers the real composer", () => {
  expect(NOTEBOOK_CALLERS).toContain("Notebooks.tsx");
  expect(NOTEBOOKS).not.toContain(UNREACHABLE);
  expect(NOTEBOOKS).toContain("Compose a Notebook");
  expect(NOTEBOOKS).toContain("current immutable versions of Reports");
});

it("keeps the seed sentence, because seeds are real and are not a create path", () => {
  // Enabling a connector seed creates nothing. The sentence stays beside the
  // true instruction so the two are not confused.
  expect(REPORTS).toContain("A connector seed on its own is availability, not a Report.");
});

// ---------------------------------------------------------------------------
// 76-4 — the same rule, widened from two named screens to the whole console.
//
// The assertions above hold ONE property of TWO files: does the copy match
// whether a creator exists. That property is real and it stays. What it could
// never see is the general shape `console-presentation.md` §5 ratifies:
//
//   « An empty state that names a gesture ("create a first datastream") WITHOUT
//     the control that performs it is incomplete. »
//   « The same error is never shown twice on one screen … one formulation. »
//
// Both are read over every file under `ui/admin/src`, as SOURCE greps, for the
// reason the assertions above are: what has to hold is a property of the COPY,
// and a render test only ever proves the one state it was set up to reach.
// ---------------------------------------------------------------------------

/** Every non-test source file of the console, comment-stripped. */
function consoleSources(): Array<{ path: string; body: string }> {
  const out: Array<{ path: string; body: string }> = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const full = resolve(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry !== "__tests__" && entry !== "node_modules") walk(full);
        continue;
      }
      if (!/\.tsx?$/.test(entry) || /\.test\./.test(entry)) continue;
      out.push({
        path: full.slice(CONSOLE_SRC.length + 1).replace(/\\/g, "/"),
        // Comments are stripped for the reason `code()` above gives, and it
        // matters more here: several of these files QUOTE their retired copy in
        // a header, gesture verb and all.
        body: readFileSync(full, "utf-8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*/g, ""),
      });
    }
  };
  walk(CONSOLE_SRC);
  return out;
}

const SOURCES = consoleSources();

/**
 * Reads one JSX element by NAME and returns each occurrence's attribute text.
 *
 * A regex cannot do this: a `description` may be a template literal carrying
 * braces, and an `action` routinely carries three levels of them
 * (`action={<Button onClick={() => f()}>x</Button>}`). So this counts braces
 * and stops at the `>` that is at depth zero — the end of the opening tag, and
 * nothing else.
 */
export function openingTags(body: string, tag: string): string[] {
  const found: string[] = [];
  const opener = new RegExp(String.raw`<${tag}(?=[\s/>])`, "g");
  for (const match of body.matchAll(opener)) {
    const from = (match.index ?? 0) + match[0].length;
    let depth = 0;
    let quote: string | null = null;
    let i = from;
    for (; i < body.length; i += 1) {
      const c = body[i];
      if (quote) {
        if (c === quote) quote = null;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") { quote = c; continue; }
      if (c === "{") { depth += 1; continue; }
      if (c === "}") { depth -= 1; continue; }
      if (depth === 0 && c === ">") break;
    }
    found.push(body.slice(from, i));
  }
  return found;
}

// ---------------------------------------------------------------------------
// Rule 1 — an empty state that names a gesture must leave the reader able to
// perform it.
// ---------------------------------------------------------------------------

/**
 * The gesture verbs §5's own example is about — the words that name a WRITE
 * this console performs.
 *
 * WHY A LIST AND NOT A DICTIONARY OF VERBS. The rule is not "no verb in an
 * empty state": `See what it feeds` is a verb and it is correct English about a
 * link that exists — the exact correction 76-3 made to a guard that had grown
 * seventy-seven exemptions. These nine are the writes the story measured.
 *
 * `choose` and `pick` are deliberately ABSENT, and it is a decision rather than
 * an oversight: their control is the shell's project switcher, permanently on
 * screen, and `NoScope` says where it is. `compose` is absent for the mirror
 * reason — the Dossiers empty state says the gesture is performed from the MCP
 * host, which is true and is the whole point of that sentence.
 */
const GESTURE_VERBS = [
  "create", "add", "connect", "declare", "run", "import", "publish", "map", "bind",
];

/**
 * A gesture is a verb in IMPERATIVE POSITION — bare form, opening a sentence.
 *
 * MEASURED, AND THE FIRST VERSION WAS WRONG. Matching the verb anywhere, in any
 * conjugation, accused 30 empty states, and 17 of them were correct English
 * about something other than a gesture: `No MCP host is connected` (a state),
 * `Every pin family this run declares` (the object acting, not the reader),
 * `A run belongs to a named profile` (the noun). Only the bare form at the head
 * of a sentence is the console telling a person to do something — which is the
 * only shape §5 is about — and the sharpened rule leaves 13.
 */
const GESTURE = new RegExp(
  String.raw`(?:^|[.!?;:]\s+)(${GESTURE_VERBS.join("|")})\b`,
  "i",
);

/**
 * « … without the control that performs it ». A control the reader can reach is
 * either MOUNTED here (`action`) or NAMED here — and naming it is a shape, not
 * a list of pardoned files: a positional word, or a preposition followed by the
 * proper noun of a screen. `Add one in Explore`, `Create one in Invitations
 * below`, `from its card above` all leave the reader able to act; `Bind one to
 * a geographic dimension` and `Add a Datastream` do not.
 *
 * This is the same instrument shape 76-3 landed for the retired spellings: test
 * the word that comes BEFORE, rather than carry an exemption per site.
 */
const NAMES_ITS_PLACE = /\b(?:above|below|here)\b|\b(?:in|from|on|under|through)\s+(?:the\s+)?[A-Z][A-Za-z]/;

/** The two slots a person reads. `icon` and `data-testid` are not copy. */
const EMPTY_STATE_COPY = /\b(?:title|description)=\{?(?:`([^`]*)`|"([^"]*)")/g;

it("no empty state names a gesture the reader cannot reach", () => {
  const offenders: string[] = [];
  for (const { path, body } of SOURCES) {
    for (const attrs of openingTags(body, "EmptyState")) {
      // An `action` IS the control. `EmptyState` has exactly one slot for it,
      // so its presence is decidable from the tag and needs no render.
      if (/\baction=/.test(attrs)) continue;
      for (const match of attrs.matchAll(EMPTY_STATE_COPY)) {
        const copy = (match[1] ?? match[2] ?? "").replace(/\s+/g, " ");
        const named = GESTURE.exec(copy);
        if (!named) continue;
        if (NAMES_ITS_PLACE.test(copy)) continue;
        offenders.push(`${path}: "${named[1]}" in "${copy.slice(0, 100)}"`);
      }
    }
  }
  expect(offenders).toEqual([]);
});

// ---------------------------------------------------------------------------
// Rule 2 — one error, one formulation.
// ---------------------------------------------------------------------------

/**
 * The bodies of a file's error BANNERS, keeping only the ones this console
 * WROTE.
 *
 * MEASURED, AND THE FIRST VERSION WAS WRONG HERE TOO. Comparing every body
 * accused four files and three were correct: `DatastreamWorkbenchRoute` renders
 * `{visibleState.message}` under `Workbench access unavailable` and under
 * `Workbench evidence unavailable` — two different facts of one state machine,
 * never both true; `CanonicalFields` renders `{retireError}` in two different
 * confirmations, never both open; `SemanticModelTabs` renders one refusal word
 * per matrix cell and per table row, which is a mark, not a banner.
 *
 * What they share is that the body is a VALUE — a state variable, a server
 * sentence, a call — and the console chose no words there. §5 is about a
 * formulation: a sentence this repository authored. So a body counts only when
 * it carries plain prose of its own, or names a `*_COPY` constant, which is how
 * this console stores an authored sentence. That is exactly what the wizard's
 * `{EXTERNAL_BQ_COPY.previewNeedsEstimate}` was, twice, under two titles.
 */
const AUTHORED_COPY = /\b[A-Z][A-Z0-9_]*_COPY\.\w+/;

export function authoredErrorBodies(source: string): string[] {
  const bodies: string[] = [];
  for (const match of source.matchAll(/<Status(?=\s)/g)) {
    const from = match.index ?? 0;
    const attrs = openingTags(source.slice(from), "Status")[0] ?? "";
    if (!/tone="error"/.test(attrs)) continue;
    // A banner, not an inline mark: `as="inline"` is the default and is a dot
    // in a cell, which repeats per row by design.
    if (!/as="block"/.test(attrs)) continue;
    // A self-closing tag has no body to compare.
    if (attrs.trimEnd().endsWith("/")) continue;
    const tagEnd = from + "<Status".length + attrs.length;
    const close = source.indexOf("</Status>", tagEnd);
    if (close < 0) continue;
    const inner = source.slice(tagEnd + 1, close).replace(/\s+/g, " ").trim();
    // Prose the file wrote itself: text outside every interpolation.
    const prose = inner.replace(/\{[^{}]*\}/g, " ").replace(/<[^<>]*>/g, " ").trim();
    if (prose.length < 12 && !AUTHORED_COPY.test(inner)) continue;
    bodies.push(inner);
  }
  return bodies;
}

it("no screen spells one authored error two ways", () => {
  const offenders: string[] = [];
  for (const { path, body } of SOURCES) {
    const seen = new Map<string, number>();
    for (const inner of authoredErrorBodies(body)) seen.set(inner, (seen.get(inner) ?? 0) + 1);
    for (const [inner, count] of seen) {
      if (count > 1) offenders.push(`${path}: x${count} - ${inner.slice(0, 80)}`);
    }
  }
  expect(offenders).toEqual([]);
});

it("reads a real corpus, and not zero files", () => {
  // The guard on the guard. A broken walk returns zero files, zero offenders
  // and green for ever — the failure this repository has paid for four times.
  expect(SOURCES.length).toBeGreaterThan(300);
  expect(SOURCES.some((entry) => entry.path === "ui/Data.tsx")).toBe(true);
  // And the corpus really does hold empty states and error banners to read.
  expect(SOURCES.filter((entry) => openingTags(entry.body, "EmptyState").length > 0).length)
    .toBeGreaterThan(30);
});

it("both rules ACCUSE what they exist to refuse", () => {
  // A guard that cannot be shown failing is a guard nobody is holding. Proven
  // on synthetic source as well as on the tree, so it keeps proving it once the
  // tree is clean.
  const guilty = '<EmptyState title="No Datastream" description="Create the first one." />';
  expect(openingTags(guilty, "EmptyState")).toHaveLength(1);
  expect(GESTURE.test("Create the first one.")).toBe(true);
  expect(NAMES_ITS_PLACE.test("Create the first one.")).toBe(false);

  // The three ways out, each proven.
  const mounted = '<EmptyState title="x" description="Create one." action={<Button/>} />';
  expect(/\baction=/.test(openingTags(mounted, "EmptyState")[0])).toBe(true);
  expect(NAMES_ITS_PLACE.test("Create one in Invitations below.")).toBe(true);
  expect(NAMES_ITS_PLACE.test("Add one in Explore to make it bindable.")).toBe(true);

  // And what it must NOT accuse: the verb as a noun, and the object acting.
  expect(GESTURE.test("A run belongs to a named profile.")).toBe(false);
  expect(GESTURE.test("No MCP host is connected to this organization")).toBe(false);

  // The brace counter is the half a regex cannot do: an `action` holding a
  // handler with braces of its own must not end the tag early.
  const nested = "<EmptyState title=\"x\" action={<Button onClick={() => go({ a: 1 })}>Go</Button>} />";
  expect(openingTags(nested, "EmptyState")[0]).toContain("action=");

  // Rule 2, in the shape the wizard actually had: one authored constant, two
  // banners, two titles.
  const doubled = [
    '<Status as="block" tone="error" title="No scan estimate">{BQ_COPY.needsEstimate}</Status>',
    '<Status as="block" tone="error" title="A read cannot be launched">{BQ_COPY.needsEstimate}</Status>',
  ].join("\n");
  expect(authoredErrorBodies(doubled)).toHaveLength(2);
  // And what it must NOT accuse: one state variable under two different facts.
  const machine = [
    '<Status as="block" tone="error" title="Access unavailable">{state.message}</Status>',
    '<Status as="block" tone="error" title="Evidence unavailable">{state.message}</Status>',
  ].join("\n");
  expect(authoredErrorBodies(machine)).toEqual([]);
});

// ---------------------------------------------------------------------------
// Rule 3 — §5's own sentence, restored as the criterion: « an error block
// offers no action ».
//
// THE FIRST VERSION OF THIS STORY NARROWED THE RULE TO ITS OWN REPAIR. It made
// `ui/AsyncStates.tsx#Failure` take a required `action`, closed those 19 sites,
// and then wrote the amendment as though `<Failure>` were the class. It is not.
// `Failure` is one of two ways this console draws an error; the other is a
// `Status as="block" tone="error"` written at the call site, and there are 200
// of them. 157 carry no action. §5 does not say "a Failure offers no action".
//
// WHAT IS REFUSED, AND WHAT IS NOT. A block that reports A FAILED READ — the
// fetch did not answer, the object could not be loaded, the service could not be
// reached — is a dead end without an action: the person did nothing to cause it
// and there is nothing on the screen to correct. A block that reports A REFUSAL
// — they typed something the server would not accept, a write was rejected — is
// not a dead end when the control that repairs it is the form they are looking
// at; the repair is to change the value and press the button again.
//
// So the shape below is the READ-FAILURE shape, and only it. Refusals are
// classified in the story record rather than held by a grep, for the reason
// `glossary.md` gives about the word *Concept*: a guard that cries wolf gets
// switched off, and "is the repairing control on this screen" is not decidable
// from source.
// ---------------------------------------------------------------------------

/**
 * The wording a failed read takes in this console, measured over all 200 blocks.
 * `could not (be )?load` matches `could not be loaded` by prefix, which is why
 * the participles are not spelled out.
 */
const READ_FAILURE = /unavailable|could not (be )?(load|read|fetch|open)|failed to (load|read|fetch)|not (be )?reached/i;

/** Every `Status as="block" tone="error"` in a file, with its title and body. */
export function errorBlocks(source: string): Array<{ attrs: string; title: string; body: string }> {
  const out: Array<{ attrs: string; title: string; body: string }> = [];
  for (const match of source.matchAll(/<Status(?=\s)/g)) {
    const from = match.index ?? 0;
    const attrs = openingTags(source.slice(from), "Status")[0] ?? "";
    if (!/as="block"/.test(attrs) || !/tone="error"/.test(attrs)) continue;
    const titleMatch = attrs.match(/\btitle=\{?(?:`([^`]*)`|"([^"]*)"|([^\s}]+))/);
    const title = titleMatch ? (titleMatch[1] ?? titleMatch[2] ?? titleMatch[3] ?? "") : "";
    let body = "";
    if (!attrs.trimEnd().endsWith("/")) {
      const tagEnd = from + "<Status".length + attrs.length;
      const close = source.indexOf("</Status>", tagEnd);
      if (close > 0) body = source.slice(tagEnd + 1, close).replace(/\s+/g, " ").trim();
    }
    out.push({ attrs, title, body });
  }
  return out;
}

it("no block that reports a failed read is a dead end", () => {
  const offenders: string[] = [];
  for (const { path, body } of SOURCES) {
    for (const block of errorBlocks(body)) {
      if (/\baction=/.test(block.attrs)) continue;
      if (!READ_FAILURE.test(`${block.title} ${block.body}`)) continue;
      offenders.push(`${path}: ${block.title || "(no title)"}`);
    }
  }
  expect(offenders).toEqual([]);
});

it("an error block that reports a failed read says WHAT could not be read", () => {
  // Measured 2026-09-06: 17 of the 157 actionless blocks carried no `title` at
  // all -- a red rectangle with a server sentence in it. A person cannot act on
  // an error whose subject is unnamed, so a title is half of the action.
  const offenders: string[] = [];
  for (const { path, body } of SOURCES) {
    for (const block of errorBlocks(body)) {
      if (!READ_FAILURE.test(`${block.title} ${block.body}`)) continue;
      if (!block.title) offenders.push(`${path}: untitled read failure`);
    }
  }
  expect(offenders).toEqual([]);
});

// ---------------------------------------------------------------------------
// Rule 4 — an absent COLLECTION is an `EmptyState`, never a bare sentence.
//
// §5 opens with « One `EmptyState` ». Measured 2026-09-06 over comment-stripped
// source: 53 absence SENTENCES were rendered outside it, and 29 of them answered
// for an empty collection -- the primitive library's own `ui/EntityMatrix.tsx`
// among them, twice. The other 24 are a fact about ONE object, a disclosure, or
// the result of a filter, and those stay sentences.
//
// The rule tests the shape, not a list: a text run that OPENS with `No`,
// `Nothing` or `None` and is a SENTENCE -- five words or more, or a full stop --
// is an answer, and an answer for a region belongs in the primitive. A short
// noun phrase (`No filter`, `No time grain`, `No owner link`) is the dash a
// definition list owes its reader and is not touched.
// ---------------------------------------------------------------------------

const ABSENCE_OPENS = /^(?:No|Nothing|None)\b/;

/** Absence SENTENCES a file renders outside `EmptyState` and outside `Status`. */
export function bareAbsenceSentences(source: string): string[] {
  let masked = source;
  const blank = (start: number, end: number) =>
    masked.slice(0, start) + " ".repeat(end - start + 1) + masked.slice(end + 1);
  // `EmptyState` is the answer; `Status` is a statement about a condition and has
  // its own rules above. Mask both -- and mask a `Status` WHOLE, body included:
  // its children are part of the statement it makes, not a bare sentence beside
  // it. Masking only the opening tag counted six `Status` bodies as offenders,
  // which is how a guard starts asking for an `EmptyState` inside an alert.
  for (const t of openingTagSpans(masked, "EmptyState")) masked = blank(t.start, t.end);
  for (const t of openingTagSpans(masked, "Status")) {
    const close = masked.indexOf("</Status>", t.end);
    masked = blank(t.start, close < 0 ? t.end : close + "</Status>".length - 1);
  }
  const found: string[] = [];
  for (const m of masked.matchAll(/>([^<>{}]{6,}?)</g)) {
    const text = m[1].replace(/\s+/g, " ").trim();
    if (!ABSENCE_OPENS.test(text)) continue;
    if (text.split(/\s+/).length < 5 && !/[.!?]$/.test(text)) continue;
    found.push(text);
  }
  return found;
}

/** `openingTags` with the span, so a tag can be blanked out of the source. */
function openingTagSpans(body: string, tag: string): Array<{ start: number; end: number }> {
  const spans: Array<{ start: number; end: number }> = [];
  const opener = new RegExp(String.raw`<${tag}(?=[\s/>])`, "g");
  for (const match of body.matchAll(opener)) {
    const from = (match.index ?? 0) + match[0].length;
    let depth = 0;
    let quote: string | null = null;
    let i = from;
    for (; i < body.length; i += 1) {
      const c = body[i];
      if (quote) { if (c === quote) quote = null; continue; }
      if (c === '"' || c === "'" || c === "`") { quote = c; continue; }
      if (c === "{") { depth += 1; continue; }
      if (c === "}") { depth -= 1; continue; }
      if (depth === 0 && c === ">") break;
    }
    spans.push({ start: match.index ?? 0, end: i });
  }
  return spans;
}

/**
 * The absences that are NOT a region's answer, each named by the file it lives
 * in.
 *
 * THE CUT IS THE CONTAINER, and it is what the migration measured rather than
 * what it assumed. `EmptyState` answers for a REGION — a panel's body, a tab, a
 * page — and it is 12rem of centred vertical space, which is exactly right
 * there and wrong everywhere else. The entries below answer inside a ROW, a
 * CARD, a FIELD or a 210px navigation rail, or they are not an absence at all:
 * a fact about one object, a disclosure of what a screen deliberately does not
 * do, a consequence read inside a confirmation, or a FILTER that matched
 * nothing while the collection behind it is full.
 *
 * `shell/DataTree.tsx` is the clearest of them and its file says so in a
 * comment: an `EmptyState` in a 210px tree rail reads as a broken layout, not
 * as an answer. The three `No published Concept` hints sit where a
 * `<NativeSelect>` goes, inside a form, beside a `loading` twin that is already
 * a plain sentence.
 *
 * 29 absences WERE regions and are now `EmptyState`, including two in the
 * primitive library itself (`ui/EntityMatrix.tsx`). This list is what remains.
 *
 * THIS LIST IS THE CLASSIFICATION, not an amnesty. Every entry answers about ONE
 * object (`No freshness verdict was recorded for this Result`), DISCLOSES what a
 * screen deliberately does not do (`No path token is created here`), explains a
 * CONSEQUENCE inside a confirmation (`Nothing is deleted: its versions stay`),
 * or reports a FILTER that matched nothing where the collection is not empty.
 * None of them is a region with nothing in it, which is what `EmptyState`
 * answers for. A new entry has to carry the same kind of reason.
 */
const NOT_A_COLLECTION = new Set([
  // analyze/builder/VisualizationBuilder.tsx
  "No version has been saved yet.",
  // analyze/explorer/AnalyticsExplorer.tsx
  "No active Skills.",
  // analyze/ResultWorkbench.tsx
  "No freshness verdict was recorded for this Result, so this screen cannot tell you whether the data behind it is current. Re-run the query to get one.",
  // analyze-artifacts/Dossiers.tsx
  "None was recorded for this Result",
  // connaissances/ContextHubLayout.tsx
  "No matching business layer.",
  // ContextObjectPage.tsx
  "No interaction has taken this Skill yet. A walk appears here the first time an agent asks for this procedure and calls one of the tools its steps name.",
  // datastreams/workbench/DatastreamReloadPanel.tsx
  "None of them is a control. Day-by-day coverage above performs the retired ones.",
  // datastreams/workbench/DatastreamSample.tsx
  "No eligible row on this day.",
  // datastreams/workbench/mapping/DeclareConceptPanel.tsx
  "No published Concept to reference yet. A formula pins a Concept AND its exact version, and this project has published no Concept version, so there is nothing to point at. A Concept is published in Governance.",
  // datastreams/workbench/pages/WorkbenchCostPage.tsx
  "No rule reached this phase",
  // datastreams/workbench/pages/WorkbenchPlacementsPage.tsx
  "No placement is attached to this campaign on this line.",
  // governance/CanonicalFields.tsx
  "Nothing is deleted: its versions stay, and any Semantic View relationship that pins one keeps working. What changes is that no new relationship can be built on it. A key a relationship still pins is refused, and the refusal names which ones to retire first.",
  // governance/CanonicalFields.tsx
  "Nothing is deleted: a mapping already published against this field keeps working. What changes is that it leaves this list and no new mapping can be built on it — and the name",
  // governance/CountryWorkspace.tsx
  "No country is assigned to this Market.",
  // governance/FormulaTreeEditor.tsx
  "No published Concept to reference yet. A formula pins a Concept AND its exact version, and this Project has published no Concept version, so there is nothing to point at.",
  // governance/NewSemanticViewDialog.tsx
  "No pair currently implements the same published MDM common key.",
  // governance/NewSemanticViewDialog.tsx
  "No published Concept to publish yet. A Semantic View publishes at least one metric, pinned to its exact version, and this Project has none: a Concept is authored from New Concept in Governance, and published before it can be pinned.",
  // governance/TaxFeeLadderTabs.tsx
  "No jurisdiction — applies on contract, not on geography",
  // governance/UnresolvedRepairDrawer.tsx
  "Nothing is written until you have seen what this repair would do.",
  // KnowledgeBasePage.tsx
  "No fallback knowledge has been substituted.",
  // KnowledgeGraphPage.tsx
  "Nothing of this project&rsquo;s own yet",
  // KnowledgeGraphPage.tsx
  "No node matches these filters",
  // KnowledgeGraphPage.tsx
  "Nothing feeds this field yet — no datastream in this project maps a source column onto it.",
  // KnowledgeGraphPage.tsx
  "Nothing links to this node.",
  // shell/DataTree.tsx
  "No Datastream yet — one is added from Data.",
  // shell/pages/FeedbackReviewWorkbench.tsx
  "No navigation was supplied to this workbench",
  // shell/pages/JoinOrg.tsx
  "No changes were made. You can retry the acceptance.",
  // shell/pages/PlatformClocks.tsx
  "No longer in the sequence",
  // shell/pages/ProjectMapping.tsx
  "None identified by the server.",
  // shell/pages/WidgetFeedback.tsx
  "No additional version filter.",
  // shell/pages/WidgetFeedback.tsx
  "No compatible automated verdict.",
]);

it("an absent collection is an EmptyState, never a bare sentence", () => {
  const offenders: string[] = [];
  for (const { path, body } of SOURCES) {
    for (const text of bareAbsenceSentences(body)) {
      if (NOT_A_COLLECTION.has(text)) continue;
      offenders.push(`${path}: ${text.slice(0, 90)}`);
    }
  }
  expect(offenders).toEqual([]);
});

it("the classification list stays a classification, and does not rot", () => {
  // Every entry must still be rendered somewhere, or it is an exemption for a
  // sentence nobody writes any more -- which is how a list outgrows its rule.
  const all = new Set(SOURCES.flatMap((entry) => bareAbsenceSentences(entry.body)));
  const stale = [...NOT_A_COLLECTION].filter((text) => !all.has(text));
  expect(stale).toEqual([]);
});

it("rules 3 and 4 ACCUSE what they exist to refuse", () => {
  const deadEnd = '<Status as="block" tone="error" title="The rows could not be read">{e}</Status>';
  expect(errorBlocks(deadEnd)).toHaveLength(1);
  expect(READ_FAILURE.test(errorBlocks(deadEnd)[0].title)).toBe(true);
  expect(/\baction=/.test(errorBlocks(deadEnd)[0].attrs)).toBe(false);

  const repaired = '<Status as="block" tone="error" title="The rows could not be read" action={<Retry/>}>{e}</Status>';
  expect(/\baction=/.test(errorBlocks(repaired)[0].attrs)).toBe(true);

  // A refusal is NOT accused: the person typed something and the form is here.
  const refusal = '<Status as="block" tone="error" title="The run was refused">{e}</Status>';
  expect(READ_FAILURE.test(refusal)).toBe(false);

  // Rule 4: a sentence is an answer, a noun phrase is a cell value.
  expect(bareAbsenceSentences("<p>No datastream is bound to this project yet.</p>")).toHaveLength(1);
  expect(bareAbsenceSentences("<dd>No time grain</dd>")).toHaveLength(0);
  // And an EmptyState's own copy is never counted as a bare sentence.
  expect(bareAbsenceSentences('<EmptyState title="No datastream is bound to this yet." />')).toHaveLength(0);
});
