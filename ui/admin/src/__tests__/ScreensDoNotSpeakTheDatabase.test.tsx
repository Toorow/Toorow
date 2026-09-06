/**
 * THE SCREEN DOES NOT SPEAK THE DATABASE, AND IT DOES NOT SPEAK THE TRACKER.
 *
 * Three defects of one class, found on six different screens by the audit of
 * 2026-08-17 (reports 04, 05, 08 and 12). Fixing them one screen at a time is
 * what let them spread in the first place, so the guard is written over the
 * SOURCE of every console file rather than over one render:
 *
 *  1. A story number or an amendment date rendered to a person. The wizard's
 *     Mode step read "One question per screen (57.12, amendment of 2026-08-11)";
 *     two workbenches deferred a judgement to "Story 51.3" and "Story 51.1".
 *     An operator has no tracker and no way to look any of those up.
 *
 *  2. A stored word printed raw inside a user sentence. `EventStreamPanel` said
 *     "{name} is {lifecycle_state}" and "Version 3 is superseded" — column
 *     values, straight through. `SchedulePanel`'s `RUN_STATE` had already shown
 *     the shape of the repair: a closed map from the stored word to a sentence.
 *
 *  3. An empty list described in the words of the storage. "No immutable
 *     managed-feed ledger row is available for this Project", "Membership
 *     evidence is empty", "No governed mapping evidence has been persisted" —
 *     none of them names what would fill the list.
 *
 * WHY OVER THE SOURCE AND NOT OVER A RENDER. Reaching every empty state of the
 * console through a render means driving each screen into the one state where
 * its list is empty, which is more fixtures than there are screens. What has to
 * hold is a property of the COPY, and the copy is readable directly.
 *
 * WHAT THIS GUARD CANNOT SEE, said here rather than discovered later. A rendered
 * word that is spelled like a token escapes it: `ContextObjectPage` read
 * `{kind === "topic" ? "knowledge" : "procedures"}`, and `"procedures"` is
 * lowercase with no punctuation, so `isAddress` below classifies it as a route
 * segment. That instance was found by reading and is fixed, but a regression of
 * the same SHAPE would pass. Tightening the rule to catch it would report every
 * `data-testid` and class name in the console, which is the trade that gets a
 * guard suppressed rather than obeyed — so the hole is recorded instead of
 * closed, and a single-word rendered literal stays something to read for.
 *
 * WHAT IS DELIBERATELY NOT MATCHED. Comments are stripped first: every one of
 * these files EXPLAINS its old copy in a comment, quoting the defect verbatim so
 * the next reader knows what changed and why — the same trap the empty-state
 * guard beside this one hit twice, and stripping comments was the fix both
 * times. Identifiers are not matched either: `lifecycle_state` as a field READ
 * (`armed.lifecycle_state === "active"`) is a predicate over the wire, not a
 * word on a screen, and the rename that would remove it is a migration with a
 * backfill (`alignment-register.md` item 4), not a copy change.
 *
 * WHAT IT READS, AND WHY THAT IS NOT A LIST. This guard was anchored on
 * `resolve(__dirname, "..")` — `ui/admin/src` alone — for as long as it existed,
 * and it declared the class closed while fourteen other front trees carried it:
 * `ui/shell`, the ten `ui/cards/*` and the three `ui/widgets/*`. That is the
 * guard's own defect and not a missing entry, because a screen is drawn by
 * whichever package holds the component — `ui/cards/shell/src/viz/renderers/`
 * draws the AI Path onto two console pages, and every word it renders escaped a
 * guard mounted inside the console. So the scope is DERIVED: every `package.json`
 * under `ui/` names one front package, and its `src/` is read. A hard-coded list
 * of trees would have to be edited by whoever adds the sixteenth package, which
 * is exactly the maintenance that let the fifteenth go unread.
 *
 * WHAT A `pending` ENTRY DOES. A violation that cannot be repaired here — a file
 * a parallel session is holding, or a rename that needs an arbitration the
 * ratified documents do not carry — is written into
 * `docs/product-architecture/known-debt.json` under `pending`, with its reason,
 * its owner and its date, and it stops blocking. No counter is raised and no
 * pattern is loosened: the finding stays named, stays loud in the audit report,
 * and has to disappear when its owner lands. Widening a regex or shrinking the
 * scope to make this file green would put the class back where it was.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";

/** `ui/` — this file sits at `ui/admin/src/__tests__/`. */
const UI = resolve(__dirname, "..", "..", "..");
const REPO = resolve(UI, "..");

/** Every front package's `src/`, derived from where a `package.json` sits.
 *
 *  The workspace root `ui/package.json` has no `src/` and drops out on its own,
 *  which is why the existence check is the filter and no name is spelled here. */
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
    if (extname(entry) === ".ts" || extname(entry) === ".tsx") out.push(full);
  }
  return out;
}

/** Source with block and line comments removed.
 *
 *  `.` does not cross a line in JS, so a line comment needs no explicit newline
 *  class — and that class is what kept breaking through the shells. */
function code(file: string): string {
  return readFileSync(file, "utf-8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*/g, "");
}

/** Only the contents of string and template literals: what a person can read.
 *
 *  A bare identifier is not copy. Matching the whole file would report
 *  `node_type === "procedure"` and `interface EventStreamPlan` as defects, and a
 *  guard that cries on a predicate gets suppressed rather than obeyed. */
function literals(source: string): string[] {
  return [
    ...source.matchAll(/"([^"\\\n]|\\.)*"/g),
    ...source.matchAll(/'([^'\\\n]|\\.)*'/g),
    ...source.matchAll(/`([^`\\]|\\.)*`/g),
  ].map((match) => match[0]);
}

/** A literal that is an address, an identifier or a key, not a sentence.
 *
 *  Route segments, API paths, `data-testid`s and CSS class names all legitimately
 *  carry the delivered tokens, and all of them are excluded by shape rather than
 *  by a list of allowed files — a list would need an entry per screen, which is
 *  the maintenance the class-wide guard exists to avoid. */
function isAddress(literal: string): boolean {
  const inner = literal.slice(1, -1);
  // A template's interpolations are holes, not words: `procedure-review-${id}`
  // is one `data-testid`, and what makes it an address is everything around the
  // hole. Blanking them first is what lets the shape test below see it.
  const shape = inner.replace(/\$\{[^}]*\}/g, "");
  return (
    inner.includes("/")
    || inner.includes("_")
    // A class list and a `data-testid` are lowercase words, hyphens and spaces
    // with no punctuation. A sentence a person reads has a capital or a full
    // stop — "No members" and "panel procedure-empty" part company here, which
    // is the whole reason the test is shape-based and carries no file list.
    || /^[a-z0-9- ]*$/.test(shape)
    || shape.trim() === ""
  );
}

const FILES = frontRoots(UI).sort().flatMap((root) => sources(root));

/** Findings another session owns, dated and reasoned, that stop blocking here.
 *
 *  Read from `known-debt.json.pending`, never from a constant in this file: the
 *  entry has to carry an owner and a date, and it has to be visible to the audit
 *  that reports it. Matching is EXACT on the finding string, so an entry can
 *  exempt the literal it names and nothing else. Paths are compared with forward
 *  slashes because the file is read on every platform. */
function pendingFindings(): Set<string> {
  const raw = readFileSync(join(REPO, "docs/product-architecture/known-debt.json"), "utf-8");
  const entries = (JSON.parse(raw) as { pending?: { finding?: string }[] }).pending ?? [];
  return new Set(entries.map((entry) => entry.finding ?? "").filter(Boolean));
}
const PENDING = pendingFindings();

/** Literals matching `pattern`, across the whole console.
 *
 *  `where` chooses what the pattern reads. `"prose"` blanks every `${…}` first,
 *  because what is inside a hole is an expression and not a word: after the
 *  rename, `` `Skill ${procedure.id}` `` is correct copy that happens to
 *  interpolate a variable still called `procedure` — and a guard that reported it
 *  would be demanding a variable rename the console cannot do alone (the token
 *  is the wire's, `alignment-register.md` item 4). `"raw"` keeps the holes,
 *  which is the only way to catch a stored column being interpolated INTO a
 *  sentence — there the hole is exactly the defect. */
function offenders(pattern: RegExp, where: "prose" | "raw" = "prose"): string[] {
  const found: string[] = [];
  for (const file of FILES) {
    for (const literal of literals(code(file))) {
      if (isAddress(literal)) continue;
      const subject = where === "prose" ? literal.replace(/\$\{[^}]*\}/g, "…") : literal;
      if (pattern.test(subject)) {
        found.push(finding(file, literal));
      }
    }
  }
  return found.filter((hit) => !PENDING.has(hit));
}

/** How one hit is named, here and in `known-debt.json`. One spelling, so an
 *  entry written from a failure message matches the next run byte for byte. */
function finding(file: string, literal: string): string {
  return `ui/${relative(UI, file).replace(/\\/g, "/")}: ${literal}`;
}

describe("a console string is read by a person, not by a developer", () => {
  it("names no story and no amendment date", () => {
    // "Story 51.3", "(57.12, amendment of 2026-08-11)". Two shapes, because the
    // wizard carried the bare number in parentheses and the workbenches carried
    // the word — and pinning only the word left the wizard green.
    expect(offenders(/\bstor(y|ies)\s+\d+\.\d+|\(\d{2}\.\d+,\s*amendment/i)).toEqual([]);
  });

  it("prints no stored state word raw inside a sentence", () => {
    // The interpolation itself is the defect: `{x.lifecycle_state}` inside a
    // template a person reads. A closed label map (`RUN_STATE`,
    // `CONFIGURATION_STATE`) is how a stored word becomes a sentence.
    expect(offenders(/\$\{[^}]*(lifecycle_state|review_state|node_type|target_type)[^}]*\}/, "raw")).toEqual([]);
    const inlineJsx = FILES
      .filter((file) => /\{\s*\w+(\?)?\.(lifecycle_state|review_state)\s*\}/.test(code(file)))
      .map((file) => `ui/${relative(UI, file).replace(/\\/g, "/")}`)
      .filter((hit) => !PENDING.has(hit));
    expect(inlineJsx).toEqual([]);
  });

  it("describes an empty list in the reader's words", () => {
    // The exact phrases the audit found, and the shapes that produced them.
    // `evidence`, on its own, is a product word (`data.md`) and is not matched.
    expect(offenders(/ledger row|evidence is empty|evidence has been persisted|has been persisted[.,]|No owned \w+ evidence/i)).toEqual([]);
  });

  it("calls a Skill a Skill", () => {
    // `glossary.md`: Procedure *(not a product noun)* — A Skill. The stored
    // token `context_procedure` keeps its name until the rename of
    // `alignment-register.md` item 4 lands; the word on the screen does not
    // wait for that migration.
    //
    // The exception this list used to carry -- `ui/admin/src/connaissances/
    // archive/`, four superseded components kept unreachable -- is gone with the
    // files (story 49-6, 2026-08-30): `context-hub.md:20-22` ratified them as
    // "100% remplacés ... sans aucune perte fonctionnelle", so an exclusion that
    // outlived them would be a hole waiting for the next unreachable directory.
    expect(offenders(/\bprocedures?\b/i)).toEqual([]);
  });

  it("shows a payload as fields, never as a `<pre>` of JSON", () => {
    // `console-presentation.md` §4: *"A JSON payload is shown through
    // `EvidenceRows`, never through `<pre>{JSON.stringify}`"*, and the same
    // document's `Incomplete if` names the shape.
    //
    // MEASURED 2026-09-05 over `ui/` and `web/`: ZERO live sites. The class was
    // closed one screen at a time — the four Data workbenches, then the
    // Datastream confirmation reviews, then Project Settings' capability block —
    // and `ui/Evidence.tsx` was extracted out of the last of them. What it never
    // got was an instrument, so nothing stopped the fifth. This is it, and it
    // starts at zero rather than at a count.
    //
    // The four `<pre>` that remain are not payloads and do not match: a markdown
    // body, a version's frontmatter fallback, `RawMarkdown`, and the copy-paste
    // MCP recipe of Project Settings — text a person copies verbatim, which is
    // the one reading `EvidenceRows` would destroy. §4 allows a genuinely raw
    // payload inside a `Collapsible` whose trigger says `Raw payload`; none
    // exists today, so that shape is described here and not yet exempted.
    const found = FILES
      .filter((file) => /<pre[^>]*>\s*\{?\s*JSON\.stringify/.test(code(file)))
      .map((file) => `ui/${relative(UI, file).replace(/\\/g, "/")}`)
      .filter((hit) => !PENDING.has(hit));
    expect(found).toEqual([]);
  });
  it("never renders a stored word raw where a person reads it", () => {
    // `console-presentation.md` §4: *"vocabulary of the user, not the base"*.
    // MEASURED 2026-09-05: 42 screen positions printed a stored token straight
    // into JSX child position — `{selectedNode.status}`,
    // `{comparison.comparison_kind}`, `{project.status}`. Eleven were STATES and
    // now go through `stateLabel` (the declared vocabulary of 76-2, with its
    // tones and its `Unknown`); the rest go through a DECLARED label map beside
    // the enum's TS union with the server file quoted, or through `wireWord`,
    // which takes the base's punctuation off a token and changes nothing else.
    //
    // WHY CHILD POSITION AND WHY THESE SUFFIXES. A token reaching a person is a
    // token BETWEEN TAGS; the same field read as a predicate
    // (`x.status === "ready"`) is not copy and is not matched. The suffix list is
    // the one the census was taken with — WIDENED 2026-09-06, and the widening
    // is the point. The first list named nine suffixes and five stored words
    // walked past it under names it had not thought of: `{row.provider}` (a
    // connector slug), `{step.action}`, and three `{…scope}` on three different
    // screens. A list of field names is a guess about what a wire is called, so
    // it grows by what it MISSED, never by what merely looked plausible.
    const STORED_WORD =
      />\s*\{\s*([A-Za-z_$][\w$]*(?:\??\.[\w$]+)*?\??\.[\w$]*(?:kind|state|status|type|reason|role|severity|outcome|provider|action|scope|connector|kind_of|mode))\s*\}\s*</g;

    // Accessors whose VALUE is not a stored word — the guard keys on the field's
    // NAME, so what the value IS has to be written down, with the line that
    // proves it. Each entry is one accessor, never a file.
    const NOT_A_STORED_WORD: Readonly<Record<string, string>> = {
      // A warehouse column type (`STRING`, `INT64`) on the Result's schema tab.
      // The technical type IS the truthful word; there is no product noun for it.
      "field.type": "a warehouse column type",
      // A composed SENTENCE, not a token: `server/core/analyze_workbench.py:240`
      // writes "no active `{facet}` classification is approved in this
      // organization, so this facet cannot be chosen".
      "entry.reason": "a sentence composed by the server",
      // `server/core/analyze_artifacts.py:371` — "a sentence naming the gesture".
      "entry.unavailable_reason": "a sentence naming the gesture",
      // A person's own words: `server/core/daily_insights.py:497` stores
      // `reason_clean` exactly as it was typed.
      "share.reason": "a person's typed reason",
      // `server/core/evaluation_runs.py:1973` stores `reason[:2000]` — free text
      // written by whoever decided the gate.
      "decision.decision_reason": "free text written by the decider",
      // `server/core/entity_bindings.py:562-563` keeps `exception_reason_code`
      // AND `exception_reason`: the code is the enum, this is its prose.
      "focusedCell.exception_reason": "the prose beside an exception_reason_code",
      // The BODY of a `Status` whose title is the verdict: a refusal or a
      // degradation explained in words. `server/core/analyze_workbench.py:1218-1219`
      // passes the manifest's own sentence straight through.
      "body.refused_reason": "the server's refusal sentence",
      "body.degraded_reason": "the server's degradation sentence",
      "comparison.unavailable_reason": "the server's sentence naming the gesture",
      // `server/core/capability_proposals.py:431,450,503,510` — every one of them
      // a written sentence ("No mapping version pins a field universe.").
      "action.reason": "a written sentence from the capability compiler",
      "exception.reason": "a written sentence from the capability compiler",
      // The change-set preparation's own explanation of what it is based on.
      "base.reason": "a written sentence from the change-set preparation",
      // `server/core/feedback_review.py:3413` types it `str | None` and writes it
      // beside the attribution, never as an enum.
      "bucket.unattributed_reason": "a written sentence beside the attribution",
      // ADDED 2026-09-06 with the widened field list. Each is a field the new
      // names catch whose VALUE is not a stored word.
      //
      // An OAuth scope URL (`https://www.googleapis.com/auth/adwords`), printed
      // in monospace UNDER `entry.label` — the double display §4 asks for, and
      // the URL is the truth a person checks against the provider. It is not the
      // governance `scope` the three screens above render.
      "entry.scope": "an OAuth scope URL, shown under its own label",
      // A composed sentence: `server/core/datastream_matches.py:614,664` writes
      // "Map {named} to a canonical field on both sources, then declare a
      // common key".
      "match.next_action": "a sentence composed by the matcher",
      // `ActivityLog`s own prop, typed `ReactNode` (`ui/ActivityLog.tsx:37`):
      // "the trailing affordance", a control the caller passes. Never a token.
      "entry.action": "the ActivityLog trailing affordance, a ReactNode",
    };

    // CONSOLE ONLY, for the same reason `ScreensDoNotPrintIdentifiersAsProse`
    // holds `ObjectId` to this tree alone: `stateLabel` and `wireWord` live in
    // `ui/admin/src/ui/`, and a card cannot import them. `ui/cards/shell/src/viz/
    // renderers/aiPathBranches.tsx` prints `{branch.kind}` and is epic 76-8's,
    // which is the story that gives the card surface its own vocabulary.
    const CONSOLE = join(UI, "admin", "src");
    const offending: string[] = [];
    for (const file of FILES) {
      if (!file.startsWith(CONSOLE)) continue;
      const source = code(file);
      for (const match of source.matchAll(new RegExp(STORED_WORD.source, STORED_WORD.flags))) {
        const accessor = match[1];
        const tail = accessor.split(/\??\./).slice(-2).join(".");
        if (NOT_A_STORED_WORD[accessor] || NOT_A_STORED_WORD[tail]) continue;
        const hit = `ui/${relative(UI, file).replace(/\\/g, "/")}: {${accessor}} printed raw`;
        if (!PENDING.has(hit)) offending.push(hit);
      }
    }
    expect([...new Set(offending)].sort()).toEqual([]);
  });

  it("never falls back to the stored token when its label map misses", () => {
    // A MAP WITH A RAW FALLBACK IS THE SAME DEFECT WEARING A MAP.
    // `{ROLE_LABELS[invitation.role] ?? invitation.role}`
    // (`shell/pages/OrgSettings.tsx`) reads as a repair and is not one: the four
    // roles are declared, and the fifth the server ever adds prints its wire
    // value into a table cell. The test above cannot see it — the accessor is
    // inside a subscript, not in child position — so the shape needs its own
    // reading, and the `??` with the SAME expression on both sides is what makes
    // it decidable: the fallback is literally the key.
    //
    // MEASURED 2026-09-06: 54 `MAP[x] ?? x` across `ui/`, of which 16 in JSX
    // CHILD position — the only ones a person reads. That is the shape held at
    // zero here. The other 38 are computed keys, sort orders and props, and a
    // rule that reported them would be the rule that gets switched off.
    //
    // THE TWO REPAIRS, and which one applies is a product answer, not a shape:
    // `wireWord()` for a stored token whose word IS the token with the base's
    // punctuation taken off (13 sites), and the unknown form
    // `console-presentation.md` §4 ratifies — the words, then the token through
    // `ObjectId` — where a prettified token would CLAIM a meaning the console
    // has not been told (`roleWord`, `OrgSettings.tsx`).
    //
    // CONSOLE ONLY, for the reason the test above gives: `wireWord` lives in
    // `ui/admin/src/ui/glossary.ts` and a card or a widget cannot import it.
    // `ui/widgets/google-analytics/src/DayDetail.tsx` carries the sixteenth site
    // and belongs to the story that gives those surfaces their own vocabulary.
    const RAW_FALLBACK =
      />\s*\{\s*([A-Za-z_$][\w$]*)\s*\[\s*([A-Za-z_$][\w$?.]*)\s*\]\s*\?\?\s*\2\s*\}\s*</g;
    const CONSOLE = join(UI, "admin", "src");
    const offending: string[] = [];
    for (const file of FILES) {
      if (!file.startsWith(CONSOLE)) continue;
      for (const match of code(file).matchAll(new RegExp(RAW_FALLBACK.source, RAW_FALLBACK.flags))) {
        const hit = `ui/${relative(UI, file).replace(/\\/g, "/")}: ${match[1]}[${match[2]}] ?? ${match[2]}`;
        if (!PENDING.has(hit)) offending.push(hit);
      }
    }
    expect([...new Set(offending)].sort()).toEqual([]);
  });
});